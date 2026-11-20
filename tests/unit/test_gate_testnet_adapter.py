from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
import requests

from gatescope.credential_crypto import CredentialMaterial
from gatescope_executor.gate_testnet import (
    GateContractSpec,
    GateDeadlineExceeded,
    GateFuturesTestnetAdapter,
    GateMutationAmbiguous,
    PositionSide,
    ProtectionKind,
    gate_client_text,
    normalize_entry,
    normalize_protection,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: Any
    headers: dict[str, str]

    def json(self) -> Any:
        return self.payload


def _spec() -> GateContractSpec:
    return GateContractSpec(
        name="BTC_USDT",
        quanto_multiplier=Decimal("0.0001"),
        order_price_round=Decimal("0.1"),
        order_size_min=1,
        order_size_max=1_000_000,
        order_price_deviate=Decimal("0.5"),
        mark_price=Decimal("100"),
        last_price=Decimal("100"),
        status="trading",
    )


def test_entry_normalization_uses_integer_contracts_and_adverse_safe_price_cap() -> None:
    text = gate_client_text("10000000-0000-0000-0000-000000000001", 1, "ENTRY")
    long = normalize_entry(
        _spec(),
        position_side=PositionSide.LONG,
        base_quantity=Decimal("0.00109"),
        entry_reference=Decimal("100"),
        max_drift_bps=Decimal("50"),
        client_text=text,
    )
    short = normalize_entry(
        _spec(),
        position_side=PositionSide.SHORT,
        base_quantity=Decimal("0.00109"),
        entry_reference=Decimal("100"),
        max_drift_bps=Decimal("50"),
        client_text=text,
    )
    assert long.contracts == 10
    assert long.signed_size == 10
    assert long.limit_price == Decimal("100.5")
    assert short.signed_size == -10
    assert short.limit_price == Decimal("99.5")
    assert text.startswith("t-") and len(text.removeprefix("t-")) <= 28


def test_protection_rounds_toward_earlier_trigger_and_is_reduce_direction() -> None:
    stop = normalize_protection(
        _spec(),
        position_side=PositionSide.LONG,
        kind=ProtectionKind.STOP_LOSS,
        contracts=10,
        requested_trigger=Decimal("95.04"),
        current_reference=Decimal("100"),
    )
    take_profit = normalize_protection(
        _spec(),
        position_side=PositionSide.SHORT,
        kind=ProtectionKind.TAKE_PROFIT,
        contracts=10,
        requested_trigger=Decimal("90.04"),
        current_reference=Decimal("100"),
    )
    assert stop.trigger_price == Decimal("95.1")
    assert stop.trigger_rule == 2
    assert stop.signed_size == -10
    assert take_profit.trigger_price == Decimal("90.1")
    assert take_profit.trigger_rule == 2
    assert take_profit.signed_size == 10


def test_submit_entry_signs_exact_body_and_does_not_hide_retry() -> None:
    calls: list[dict[str, Any]] = []
    material = CredentialMaterial("test-key-123456789", "test-secret-123456789")
    text = gate_client_text("10000000-0000-0000-0000-000000000001", 1, "ENTRY")
    normalized = normalize_entry(
        _spec(),
        position_side=PositionSide.LONG,
        base_quantity=Decimal("0.001"),
        entry_reference=Decimal("100"),
        max_drift_bps=Decimal("50"),
        client_text=text,
    )

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(201, {
            "id": 123,
            "contract": "BTC_USDT",
            "size": 10,
            "left": 0,
            "status": "finished",
            "finish_as": "filled",
            "text": text,
            "is_reduce_only": False,
            "fill_price": "100.2",
        }, {})

    order = GateFuturesTestnetAdapter(material, transport=transport, clock=lambda: 1_700_000_000).submit_entry(normalized)
    assert order.order_id == "123"
    assert order.filled_contracts == 10
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/orders"
    body = call["data"]
    assert json.loads(body) == {
        "close": False,
        "contract": "BTC_USDT",
        "price": "100.5",
        "reduce_only": False,
        "size": 10,
        "text": text,
        "tif": "ioc",
    }
    body_hash = hashlib.sha512(body.encode()).hexdigest()
    signed = f"POST\n/api/v4/futures/usdt/orders\n\n{body_hash}\n1700000000".encode()
    assert call["headers"]["SIGN"] == hmac.new(
        material.api_secret.encode(), signed, hashlib.sha512,
    ).hexdigest()
    assert call["headers"]["X-Gate-Exptime"] == "1700000001500"


def test_mutation_timeout_is_ambiguous_and_transport_is_called_once() -> None:
    calls = 0

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        raise requests.Timeout("unknown submission result")

    request = normalize_entry(
        _spec(),
        position_side=PositionSide.LONG,
        base_quantity=Decimal("0.001"),
        entry_reference=Decimal("100"),
        max_drift_bps=Decimal("50"),
        client_text=gate_client_text("10000000-0000-0000-0000-000000000001", 1, "ENTRY"),
    )
    with pytest.raises(GateMutationAmbiguous, match="GATE_NETWORK_AMBIGUOUS"):
        GateFuturesTestnetAdapter(
            CredentialMaterial("test-key-123456789", "test-secret-123456789"),
            transport=transport,
        ).submit_entry(request)
    assert calls == 1


def test_submit_protection_is_mark_price_triggered_reduce_only_close_direction() -> None:
    captured: dict[str, Any] = {}

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(201, {"id": 456}, {})

    protection = normalize_protection(
        _spec(),
        position_side=PositionSide.LONG,
        kind=ProtectionKind.STOP_LOSS,
        contracts=10,
        requested_trigger=Decimal("95"),
        current_reference=Decimal("100"),
    )
    order_id = GateFuturesTestnetAdapter(
        CredentialMaterial("test-key-123456789", "test-secret-123456789"),
        transport=transport,
    ).submit_protection(protection)
    body = json.loads(captured["data"])
    assert order_id == "456"
    assert body["initial"]["size"] == -10
    assert body["initial"]["reduce_only"] is True
    assert body["initial"]["price"] == "0"
    assert body["trigger"]["price_type"] == 1
    assert body["trigger"]["rule"] == 2
    assert body["order_type"] == "plan-close-long-position"


def test_credential_material_repr_is_always_redacted() -> None:
    rendered = repr(CredentialMaterial("visible-key-value", "visible-secret-value"))
    assert "visible-key" not in rendered
    assert "visible-secret" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_trade_history_can_reconcile_entry_by_client_text() -> None:
    captured: dict[str, Any] = {}
    text = gate_client_text("10000000-0000-0000-0000-000000000001", 1, "ENTRY")

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(200, [{
            "id": 77,
            "order_id": 123,
            "contract": "BTC_USDT",
            "size": 4,
            "price": "100.2",
            "text": text,
        }], {})

    fills = GateFuturesTestnetAdapter(
        CredentialMaterial("test-key-123456789", "test-secret-123456789"),
        transport=transport,
        clock=lambda: 1_700_000_000,
    ).list_my_trades("BTC_USDT")

    assert fills[0].client_text == text
    assert fills[0].signed_size == 4
    assert captured["method"] == "GET"
    assert captured["url"].endswith(
        "/futures/usdt/my_trades?contract=BTC_USDT&limit=100&offset=0"
    )


def test_sequential_requests_share_the_remaining_protection_budget() -> None:
    clock = [0.0]
    timeouts: list[Any] = []

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        timeouts.append(kwargs["timeout"])
        clock[0] += 1.2
        return FakeResponse(200, {"contract": "BTC_USDT", "mode": "single", "size": 10}, {})

    adapter = GateFuturesTestnetAdapter(
        CredentialMaterial("test-key-123456789", "test-secret-123456789"),
        transport=transport, monotonic=lambda: clock[0],
    )
    adapter.set_deadline(3.0)
    adapter.get_position("BTC_USDT")
    adapter.get_position("BTC_USDT")
    assert timeouts[0].total == 3.0
    assert timeouts[1].total == pytest.approx(1.8)
    assert timeouts[1].connect_timeout <= 1.8
    clock[0] = 3.0
    with pytest.raises(GateDeadlineExceeded):
        adapter.get_position("BTC_USDT")
    assert len(timeouts) == 2, "no request may start after the shared deadline"
    adapter.set_deadline(None)
    adapter.get_position("BTC_USDT")
    assert timeouts[-1] == (2.0, 8.0)


@pytest.mark.parametrize("leg", ["entry", "protection", "exit"])
def test_structurally_invalid_successful_mutation_ack_is_ambiguous(leg: str) -> None:
    calls = []

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url))
        return FakeResponse(201, {"id": "malformed"}, {})

    adapter = GateFuturesTestnetAdapter(
        CredentialMaterial("test-key-123456789", "test-secret-123456789"), transport=transport,
    )
    text = gate_client_text("10000000-0000-0000-0000-000000000001", 1, "ENTRY")
    with pytest.raises(GateMutationAmbiguous):
        if leg == "entry":
            adapter.submit_entry(normalize_entry(
                _spec(), position_side=PositionSide.LONG, base_quantity=Decimal("0.001"),
                entry_reference=Decimal("100"), max_drift_bps=Decimal("50"), client_text=text,
            ))
        elif leg == "protection":
            adapter.submit_protection(normalize_protection(
                _spec(), position_side=PositionSide.LONG, kind=ProtectionKind.STOP_LOSS,
                contracts=10, requested_trigger=Decimal("95"), current_reference=Decimal("100"),
            ))
        else:
            adapter.submit_reduce_only_exit(
                contract="BTC_USDT", position_side=PositionSide.LONG, contracts=10, client_text=text,
            )
    assert len(calls) == 1
