"""Strict Gate USDT futures adapter limited to the official TESTNET host.

Mutation methods never retry internally.  A transport failure after dispatch is
reported as ambiguous so the durable coordinator must reconcile exchange facts
before taking another action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, cast
from urllib.parse import quote, urlencode

import requests
from requests.adapters import TimeoutSauce

from gatescope.credential_crypto import CredentialMaterial

GATE_TESTNET_BASE_URL = "https://fx-api-testnet.gateio.ws/api/v4"
CLIENT_TEXT_RE = re.compile(r"^t-[A-Za-z0-9_.-]{1,28}$")
CONTRACT_RE = re.compile(r"^[A-Z0-9]{2,20}_USDT$")
SAFE_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class GateAdapterError(RuntimeError):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


class GateQueryFailure(GateAdapterError):
    pass


class GateDeadlineExceeded(GateQueryFailure):
    """The shared execution budget expired before another request was sent."""


class GateMutationRejected(GateAdapterError):
    pass


class GateMutationAmbiguous(GateAdapterError):
    pass


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ProtectionKind(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


Transport = Callable[..., ResponseLike]


@dataclass(frozen=True)
class GateContractSpec:
    name: str
    quanto_multiplier: Decimal
    order_price_round: Decimal
    order_size_min: int
    order_size_max: int
    order_price_deviate: Decimal
    mark_price: Decimal
    last_price: Decimal
    status: str


@dataclass(frozen=True)
class NormalizedEntry:
    contract: str
    position_side: PositionSide
    contracts: int
    signed_size: int
    limit_price: Decimal
    client_text: str
    tif: str = "ioc"


@dataclass(frozen=True)
class NormalizedProtection:
    contract: str
    position_side: PositionSide
    kind: ProtectionKind
    contracts: int
    signed_size: int
    trigger_price: Decimal
    trigger_rule: int
    price_type: int = 1


@dataclass(frozen=True)
class GateOrderSnapshot:
    order_id: str
    contract: str
    signed_size: int
    left: int
    status: str
    finish_as: str | None
    client_text: str | None
    reduce_only: bool
    fill_price: Decimal | None

    @property
    def filled_contracts(self) -> int:
        return max(0, abs(self.signed_size) - abs(self.left))


@dataclass(frozen=True)
class GatePriceOrderSnapshot:
    order_id: str
    contract: str
    signed_size: int
    trigger_price: Decimal
    trigger_rule: int
    status: str
    finish_as: str | None
    reduce_only: bool
    order_type: str | None
    price_type: int = 1


@dataclass(frozen=True)
class GatePositionSnapshot:
    contract: str
    signed_size: int
    mode: str
    entry_price: Decimal | None
    mark_price: Decimal | None


@dataclass(frozen=True)
class GateTradeFill:
    trade_id: str
    order_id: str
    contract: str
    signed_size: int
    price: Decimal
    client_text: str | None


def gate_client_text(intent_id: str, revision: int, leg: str) -> str:
    if revision < 1 or not re.fullmatch(r"[A-Z_]{2,32}", leg):
        raise ValueError("client order identity inputs are invalid")
    material = f"{intent_id}:{revision}:{leg}".encode("ascii", "strict")
    return "t-gs-" + hashlib.sha256(material).hexdigest()[:24]


def normalize_entry(
    spec: GateContractSpec,
    *,
    position_side: PositionSide,
    base_quantity: Decimal,
    entry_reference: Decimal,
    max_drift_bps: Decimal,
    client_text: str,
) -> NormalizedEntry:
    _positive_decimal(base_quantity, "base quantity")
    _positive_decimal(entry_reference, "entry reference")
    if max_drift_bps <= 0 or max_drift_bps > 100 or not max_drift_bps.is_finite():
        raise ValueError("entry drift must be in (0, 100] bps")
    if spec.status != "trading":
        raise ValueError("contract is not trading")
    if CLIENT_TEXT_RE.fullmatch(client_text) is None:
        raise ValueError("Gate client text is invalid")
    contracts = int((base_quantity / spec.quanto_multiplier).to_integral_value(rounding=ROUND_FLOOR))
    if contracts < spec.order_size_min or contracts > spec.order_size_max:
        raise ValueError("normalized contract size is outside exchange limits")
    drift = max_drift_bps / Decimal("10000")
    if position_side is PositionSide.LONG:
        raw_limit = entry_reference * (Decimal("1") + drift)
        limit_price = _quantize_tick(raw_limit, spec.order_price_round, ROUND_FLOOR)
        signed_size = contracts
    else:
        raw_limit = entry_reference * (Decimal("1") - drift)
        limit_price = _quantize_tick(raw_limit, spec.order_price_round, ROUND_CEILING)
        signed_size = -contracts
    _positive_decimal(limit_price, "entry limit price")
    deviation = abs(limit_price - spec.mark_price) / spec.mark_price
    if deviation > spec.order_price_deviate:
        raise ValueError("entry limit exceeds the exchange mark-price deviation")
    return NormalizedEntry(
        contract=spec.name,
        position_side=position_side,
        contracts=contracts,
        signed_size=signed_size,
        limit_price=limit_price,
        client_text=client_text,
    )


def normalize_protection(
    spec: GateContractSpec,
    *,
    position_side: PositionSide,
    kind: ProtectionKind,
    contracts: int,
    requested_trigger: Decimal,
    current_reference: Decimal,
) -> NormalizedProtection:
    if contracts < spec.order_size_min or contracts > spec.order_size_max:
        raise ValueError("protection contract size is outside exchange limits")
    _positive_decimal(requested_trigger, "protection trigger")
    _positive_decimal(current_reference, "current reference")
    if position_side is PositionSide.LONG:
        signed_size = -contracts
        if kind is ProtectionKind.STOP_LOSS:
            trigger = _quantize_tick(requested_trigger, spec.order_price_round, ROUND_CEILING)
            rule = 2
            if trigger >= current_reference:
                raise ValueError("long stop must be below the current reference")
        else:
            trigger = _quantize_tick(requested_trigger, spec.order_price_round, ROUND_FLOOR)
            rule = 1
            if trigger <= current_reference:
                raise ValueError("long take profit must be above the current reference")
    else:
        signed_size = contracts
        if kind is ProtectionKind.STOP_LOSS:
            trigger = _quantize_tick(requested_trigger, spec.order_price_round, ROUND_FLOOR)
            rule = 1
            if trigger <= current_reference:
                raise ValueError("short stop must be above the current reference")
        else:
            trigger = _quantize_tick(requested_trigger, spec.order_price_round, ROUND_CEILING)
            rule = 2
            if trigger >= current_reference:
                raise ValueError("short take profit must be below the current reference")
    _positive_decimal(trigger, "normalized protection trigger")
    return NormalizedProtection(
        contract=spec.name,
        position_side=position_side,
        kind=kind,
        contracts=contracts,
        signed_size=signed_size,
        trigger_price=trigger,
        trigger_rule=rule,
    )


class GateFuturesTestnetAdapter:
    def __init__(
        self,
        material: CredentialMaterial,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        session = requests.Session()
        self._material = material
        self._transport: Transport = transport or cast(Transport, session.request)
        self._clock = clock
        self._monotonic = monotonic
        self._deadline_monotonic: float | None = None

    def set_deadline(self, deadline_monotonic: float | None) -> None:
        """Share one budget across entry, reconciliation, and stop confirmation."""
        self._deadline_monotonic = deadline_monotonic

    def get_contract(self, contract: str) -> GateContractSpec:
        _contract(contract)
        payload = self._request_json("GET", f"/futures/usdt/contracts/{quote(contract, safe='')}")
        return _contract_spec(payload, contract)

    def get_position(self, contract: str) -> GatePositionSnapshot:
        _contract(contract)
        payload = self._request_json("GET", f"/futures/usdt/positions/{quote(contract, safe='')}")
        mode = _text(payload, "mode", allowed={"single", "dual_long", "dual_short"})
        return GatePositionSnapshot(
            contract=_matching_text(payload, "contract", contract),
            signed_size=_integer(payload, "size"),
            mode=mode,
            entry_price=_optional_decimal(payload.get("entry_price")),
            mark_price=_optional_decimal(payload.get("mark_price")),
        )

    def submit_entry(self, request: NormalizedEntry) -> GateOrderSnapshot:
        payload = {
            "contract": request.contract,
            "size": request.signed_size,
            "price": _decimal_text(request.limit_price),
            "tif": "ioc",
            "reduce_only": False,
            "close": False,
            "text": request.client_text,
        }
        response = self._request_json(
            "POST", "/futures/usdt/orders", body=payload, mutation=True, expected={201}, expiring=True,
        )
        try:
            snapshot = _order_snapshot(response)
        except GateQueryFailure as exc:
            raise GateMutationAmbiguous(exc.safe_code) from exc
        if snapshot.contract != request.contract or snapshot.client_text != request.client_text:
            raise GateMutationAmbiguous("GATE_ENTRY_ACK_IDENTITY_MISMATCH")
        return snapshot

    def submit_reduce_only_exit(
        self,
        *,
        contract: str,
        position_side: PositionSide,
        contracts: int,
        client_text: str,
    ) -> GateOrderSnapshot:
        _contract(contract)
        if contracts <= 0 or CLIENT_TEXT_RE.fullmatch(client_text) is None:
            raise ValueError("reduce-only exit identity or size is invalid")
        signed_size = -contracts if position_side is PositionSide.LONG else contracts
        response = self._request_json(
            "POST",
            "/futures/usdt/orders",
            body={
                "contract": contract,
                "size": signed_size,
                "price": "0",
                "tif": "ioc",
                "reduce_only": True,
                "close": False,
                "text": client_text,
            },
            mutation=True,
            expected={201},
            expiring=True,
        )
        try:
            snapshot = _order_snapshot(response)
        except GateQueryFailure as exc:
            raise GateMutationAmbiguous(exc.safe_code) from exc
        if (
            snapshot.contract != contract
            or snapshot.client_text != client_text
            or not snapshot.reduce_only
            or snapshot.signed_size != signed_size
        ):
            raise GateMutationAmbiguous("GATE_REDUCE_ACK_INVALID")
        return snapshot

    def get_order(self, order_id_or_text: str) -> GateOrderSnapshot | None:
        _order_identifier(order_id_or_text)
        try:
            payload = self._request_json(
                "GET", f"/futures/usdt/orders/{quote(order_id_or_text, safe='')}",
            )
        except GateQueryFailure as exc:
            if exc.safe_code in {"ORDER_NOT_FOUND", "GATE_HTTP_404"}:
                return None
            raise
        return _order_snapshot(payload)

    def cancel_order(self, order_id: str) -> GateOrderSnapshot:
        _numeric_order_id(order_id)
        payload = self._request_json(
            "DELETE",
            f"/futures/usdt/orders/{quote(order_id, safe='')}",
            mutation=True,
            expected={200},
            expiring=True,
        )
        try:
            return _order_snapshot(payload)
        except GateQueryFailure as exc:
            raise GateMutationAmbiguous(exc.safe_code) from exc

    def submit_protection(self, request: NormalizedProtection) -> str:
        order_type = (
            "plan-close-long-position"
            if request.position_side is PositionSide.LONG
            else "plan-close-short-position"
        )
        payload = {
            "initial": {
                "contract": request.contract,
                "size": request.signed_size,
                "price": "0",
                "close": False,
                "tif": "ioc",
                "text": "api",
                "reduce_only": True,
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": request.price_type,
                "price": _decimal_text(request.trigger_price),
                "rule": request.trigger_rule,
                "expiration": 86_400,
            },
            "order_type": order_type,
        }
        response = self._request_json(
            "POST", "/futures/usdt/price_orders", body=payload, mutation=True, expected={201},
        )
        try:
            return str(_positive_integer(response, "id"))
        except GateQueryFailure as exc:
            raise GateMutationAmbiguous(exc.safe_code) from exc

    def get_price_order(self, order_id: str) -> GatePriceOrderSnapshot | None:
        _numeric_order_id(order_id)
        try:
            payload = self._request_json(
                "GET", f"/futures/usdt/price_orders/{quote(order_id, safe='')}",
            )
        except GateQueryFailure as exc:
            if exc.safe_code in {"ORDER_NOT_FOUND", "GATE_HTTP_404"}:
                return None
            raise
        return _price_order_snapshot(payload)

    def list_open_price_orders(self, contract: str) -> tuple[GatePriceOrderSnapshot, ...]:
        _contract(contract)
        payload = self._request_json(
            "GET",
            "/futures/usdt/price_orders",
            query={"status": "open", "contract": contract, "limit": "100", "offset": "0"},
        )
        if not isinstance(payload, list):
            raise GateQueryFailure("GATE_RESPONSE_INVALID")
        return tuple(_price_order_snapshot(item) for item in payload if isinstance(item, dict))

    def list_my_trades(self, contract: str, *, limit: int = 100) -> tuple[GateTradeFill, ...]:
        """Return recent fills used to reconcile a lost IOC acknowledgement."""

        _contract(contract)
        if limit < 1 or limit > 1000:
            raise ValueError("Gate trade history limit must be in [1, 1000]")
        payload = self._request_json(
            "GET",
            "/futures/usdt/my_trades",
            query={"contract": contract, "limit": str(limit), "offset": "0"},
        )
        if not isinstance(payload, list):
            raise GateQueryFailure("GATE_RESPONSE_INVALID")
        return tuple(_trade_fill(item) for item in payload if isinstance(item, dict))

    def cancel_price_order(self, order_id: str) -> GatePriceOrderSnapshot:
        _numeric_order_id(order_id)
        payload = self._request_json(
            "DELETE",
            f"/futures/usdt/price_orders/{quote(order_id, safe='')}",
            mutation=True,
            expected={200},
        )
        try:
            return _price_order_snapshot(payload)
        except GateQueryFailure as exc:
            raise GateMutationAmbiguous(exc.safe_code) from exc

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        mutation: bool = False,
        expected: set[int] | None = None,
        expiring: bool = False,
    ) -> Any:
        if not endpoint.startswith("/futures/usdt/"):
            raise ValueError("private Gate endpoint is outside the USDT futures allowlist")
        query_text = urlencode(sorted((query or {}).items()))
        body_text = "" if body is None else json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        timestamp = f"{self._clock():.6f}".rstrip("0").rstrip(".")
        path = f"/api/v4{endpoint}"
        payload_hash = hashlib.sha512(body_text.encode("utf-8")).hexdigest()
        signed = f"{method}\n{path}\n{query_text}\n{payload_hash}\n{timestamp}".encode("utf-8")
        signature = hmac.new(
            self._material.api_secret.encode("utf-8"), signed, hashlib.sha512,
        ).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KEY": self._material.api_key,
            "Timestamp": timestamp,
            "SIGN": signature,
        }
        if expiring:
            headers["X-Gate-Exptime"] = str(int(self._clock() * 1000) + 1_500)
        url = f"{GATE_TESTNET_BASE_URL}{endpoint}" + (f"?{query_text}" if query_text else "")
        timeout: Any = (2.0, 5.0 if mutation else 8.0)
        if self._deadline_monotonic is not None:
            remaining = self._deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise GateDeadlineExceeded("PROTECTION_DEADLINE_EXCEEDED")
            timeout = TimeoutSauce(
                total=remaining,
                connect=min(2.0, remaining),
                read=min(5.0 if mutation else 8.0, remaining),
            )
        try:
            response = self._transport(
                method,
                url,
                headers=headers,
                data=body_text if body is not None else None,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            if mutation:
                raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS") from exc
            raise GateQueryFailure("GATE_NETWORK_UNAVAILABLE") from exc
        if 300 <= response.status_code < 400:
            if mutation:
                raise GateMutationAmbiguous("GATE_REDIRECT_REJECTED")
            raise GateQueryFailure("GATE_REDIRECT_REJECTED")
        accepted = expected or {200}
        if response.status_code not in accepted:
            safe_code = _safe_error_code(response)
            if mutation and (response.status_code in {408, 425, 429} or response.status_code >= 500):
                raise GateMutationAmbiguous(safe_code)
            if mutation:
                raise GateMutationRejected(safe_code)
            raise GateQueryFailure(safe_code)
        try:
            return response.json()
        except ValueError as exc:
            if mutation:
                raise GateMutationAmbiguous("GATE_RESPONSE_INVALID") from exc
            raise GateQueryFailure("GATE_RESPONSE_INVALID") from exc


def _contract_spec(payload: Any, expected_contract: str) -> GateContractSpec:
    if not isinstance(payload, dict):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    name = _matching_text(payload, "name", expected_contract)
    spec = GateContractSpec(
        name=name,
        quanto_multiplier=_decimal(payload, "quanto_multiplier", positive=True),
        order_price_round=_decimal(payload, "order_price_round", positive=True),
        order_size_min=_positive_integer(payload, "order_size_min"),
        order_size_max=_positive_integer(payload, "order_size_max"),
        order_price_deviate=_decimal(payload, "order_price_deviate", positive=True),
        mark_price=_decimal(payload, "mark_price", positive=True),
        last_price=_decimal(payload, "last_price", positive=True),
        status=_text(payload, "status", allowed={"trading", "delisting", "delisted", "circuit_breaker", "prelaunch"}),
    )
    if spec.order_size_max < spec.order_size_min or spec.order_price_deviate > Decimal("1"):
        raise GateQueryFailure("GATE_CONTRACT_RULES_INVALID")
    return spec


def _order_snapshot(payload: Any) -> GateOrderSnapshot:
    if not isinstance(payload, dict):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    order_id = str(_positive_integer(payload, "id"))
    contract = _text(payload, "contract")
    _contract(contract)
    size = _integer(payload, "size")
    left = _integer(payload, "left")
    if abs(left) > abs(size):
        raise GateQueryFailure("GATE_ORDER_QUANTITY_INVALID")
    text_value = payload.get("text")
    client_text = text_value if isinstance(text_value, str) and text_value else None
    if client_text is not None and client_text not in {"api", "web", "app"} and CLIENT_TEXT_RE.fullmatch(client_text) is None:
        raise GateQueryFailure("GATE_ORDER_IDENTITY_INVALID")
    finish = payload.get("finish_as")
    return GateOrderSnapshot(
        order_id=order_id,
        contract=contract,
        signed_size=size,
        left=left,
        status=_text(payload, "status", allowed={"open", "finished"}),
        finish_as=finish if isinstance(finish, str) and finish else None,
        client_text=client_text,
        reduce_only=bool(payload.get("is_reduce_only", payload.get("reduce_only", False))),
        fill_price=_optional_decimal(payload.get("fill_price")),
    )


def _price_order_snapshot(payload: Any) -> GatePriceOrderSnapshot:
    if not isinstance(payload, dict):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    initial = payload.get("initial")
    trigger = payload.get("trigger")
    if not isinstance(initial, dict) or not isinstance(trigger, dict):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    contract = _text(initial, "contract")
    _contract(contract)
    finish = payload.get("finish_as")
    order_type = payload.get("order_type")
    return GatePriceOrderSnapshot(
        order_id=str(_positive_integer(payload, "id")),
        contract=contract,
        signed_size=_integer(initial, "size"),
        trigger_price=_decimal(trigger, "price", positive=True),
        trigger_rule=_integer(trigger, "rule"),
        status=_text(payload, "status", allowed={"open", "finished", "inactive", "invalid"}),
        finish_as=finish if isinstance(finish, str) and finish else None,
        reduce_only=bool(initial.get("is_reduce_only", initial.get("reduce_only", False))),
        order_type=order_type if isinstance(order_type, str) and order_type else None,
        price_type=_integer(trigger, "price_type"),
    )


def _trade_fill(payload: Mapping[str, Any]) -> GateTradeFill:
    contract = _text(payload, "contract")
    _contract(contract)
    text_value = payload.get("text")
    client_text = text_value if isinstance(text_value, str) and text_value else None
    if client_text is not None and client_text not in {"api", "web", "app"}:
        if CLIENT_TEXT_RE.fullmatch(client_text) is None:
            raise GateQueryFailure("GATE_ORDER_IDENTITY_INVALID")
    return GateTradeFill(
        trade_id=str(_positive_integer(payload, "id")),
        order_id=str(_positive_integer(payload, "order_id")),
        contract=contract,
        signed_size=_integer(payload, "size"),
        price=_decimal(payload, "price", positive=True),
        client_text=client_text,
    )


def _safe_error_code(response: ResponseLike) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        label = payload.get("label")
        if isinstance(label, str) and SAFE_LABEL_RE.fullmatch(label):
            return label
    return f"GATE_HTTP_{response.status_code}"


def _decimal(payload: Mapping[str, Any], key: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(str(payload[key]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise GateQueryFailure("GATE_RESPONSE_INVALID") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise GateQueryFailure("GATE_RESPONSE_INVALID") from exc
    if not parsed.is_finite():
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    return parsed


def _integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise GateQueryFailure("GATE_RESPONSE_INVALID") from exc
    if str(parsed) != str(value):
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    return parsed


def _positive_integer(payload: Mapping[str, Any], key: str) -> int:
    value = _integer(payload, key)
    if value <= 0:
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    return value


def _text(payload: Mapping[str, Any], key: str, *, allowed: set[str] | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 128:
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    if allowed is not None and value not in allowed:
        raise GateQueryFailure("GATE_RESPONSE_INVALID")
    return value


def _matching_text(payload: Mapping[str, Any], key: str, expected: str) -> str:
    value = _text(payload, key)
    if value != expected:
        raise GateQueryFailure("GATE_RESPONSE_IDENTITY_MISMATCH")
    return value


def _contract(value: str) -> None:
    if CONTRACT_RE.fullmatch(value) is None:
        raise ValueError("Gate USDT futures contract is invalid")


def _order_identifier(value: str) -> None:
    if not value.isdigit() and CLIENT_TEXT_RE.fullmatch(value) is None:
        raise ValueError("Gate order identifier is invalid")


def _numeric_order_id(value: str) -> None:
    if not value.isdigit() or int(value) <= 0 or len(value) > 30:
        raise ValueError("Gate order ID is invalid")


def _positive_decimal(value: Decimal, label: str) -> None:
    if value <= 0 or not value.is_finite():
        raise ValueError(f"{label} must be finite and positive")


def _quantize_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    _positive_decimal(tick, "price tick")
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
