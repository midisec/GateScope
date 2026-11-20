"""Minimal, non-mutating Gate API v4 credential capability probe."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import requests

from gatescope.credential_crypto import CredentialMaterial

GATE_BASE_URLS = {
    "TESTNET": "https://fx-api-testnet.gateio.ws/api/v4",
}


class SafeGateProbeError(RuntimeError):
    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


class GateRateLimited(SafeGateProbeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, min(retry_after_seconds, 300))
        super().__init__("GATE_RATE_LIMITED")


class ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


@dataclass(frozen=True)
class GateProbeResult:
    capabilities: dict[str, Any]
    warning_codes: tuple[str, ...]


Transport = Callable[..., ResponseLike]


class GateCredentialProbe:
    def __init__(
        self,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        session = requests.Session()
        self._transport = transport or session.get
        self._clock = clock

    def probe(self, environment: str, material: CredentialMaterial) -> GateProbeResult:
        try:
            base_url = GATE_BASE_URLS[environment]
        except KeyError as exc:
            raise SafeGateProbeError("GATE_ENVIRONMENT_NOT_ALLOWED") from exc
        detail = self._signed_get(base_url, "/account/detail", material)
        self._signed_get(base_url, "/futures/usdt/accounts", material)

        ip_whitelist = detail.get("ip_whitelist")
        pair_whitelist = detail.get("currency_pairs")
        key_detail = detail.get("key")
        warnings: list[str] = ["API_PERMISSION_SCOPE_UNVERIFIED"]
        if not isinstance(ip_whitelist, list) or not ip_whitelist:
            warnings.append("IP_WHITELIST_MISSING")
        capabilities = {
            "schema_version": 1,
            "account_read": True,
            "futures_usdt_read": True,
            "testnet": True,
            "ip_whitelist_configured": isinstance(ip_whitelist, list) and bool(ip_whitelist),
            "currency_pair_restriction_count": len(pair_whitelist) if isinstance(pair_whitelist, list) else 0,
            "account_mode": key_detail.get("mode") if isinstance(key_detail, dict) else None,
            "trade_permission": "UNVERIFIED",
            "withdrawal_permission": "UNVERIFIED",
            "trading_ready": False,
        }
        return GateProbeResult(capabilities, tuple(warnings))

    def _signed_get(
        self,
        base_url: str,
        endpoint: str,
        material: CredentialMaterial,
    ) -> dict[str, Any]:
        timestamp = f"{self._clock():.6f}".rstrip("0").rstrip(".")
        path = f"/api/v4{endpoint}"
        payload_hash = hashlib.sha512(b"").hexdigest()
        signing_input = f"GET\n{path}\n\n{payload_hash}\n{timestamp}".encode("utf-8")
        signature = hmac.new(material.api_secret.encode("utf-8"), signing_input, hashlib.sha512).hexdigest()
        try:
            response = self._transport(
                f"{base_url}{endpoint}",
                headers={"Accept": "application/json", "KEY": material.api_key, "Timestamp": timestamp, "SIGN": signature},
                timeout=(3.05, 10),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise SafeGateProbeError("GATE_NETWORK_UNAVAILABLE") from exc
        if 300 <= response.status_code < 400:
            raise SafeGateProbeError("GATE_REDIRECT_REJECTED")
        if response.status_code == 429:
            raw_retry = response.headers.get("Retry-After", "1")
            try:
                retry_after = int(raw_retry)
            except ValueError:
                retry_after = 1
            raise GateRateLimited(retry_after)
        if response.status_code in {401, 403}:
            raise SafeGateProbeError("GATE_AUTHENTICATION_FAILED")
        if response.status_code >= 500:
            raise SafeGateProbeError("GATE_SERVICE_UNAVAILABLE")
        if response.status_code != 200:
            raise SafeGateProbeError("GATE_PROBE_REJECTED")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SafeGateProbeError("GATE_RESPONSE_INVALID") from exc
        if not isinstance(payload, dict):
            raise SafeGateProbeError("GATE_RESPONSE_INVALID")
        return payload
