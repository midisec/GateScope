from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

import pytest

from gatescope.adapters.gate_credentials import GateCredentialProbe, GateRateLimited, SafeGateProbeError
from gatescope.credential_crypto import CredentialMaterial


@dataclass
class FakeResponse:
    status_code: int
    payload: Any
    headers: dict[str, str]

    def json(self) -> Any:
        return self.payload


def test_probe_signs_only_two_allowlisted_testnet_get_requests() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    responses = [
        FakeResponse(200, {"ip_whitelist": ["203.0.113.2"], "currency_pairs": [], "key": {"mode": 1}}, {}),
        FakeResponse(200, {"total": "1000", "available": "1000"}, {}),
    ]

    def transport(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        return responses.pop(0)

    material = CredentialMaterial("test-key-123456789", "test-secret-123456789")
    result = GateCredentialProbe(transport=transport, clock=lambda: 1_700_000_000).probe("TESTNET", material)
    assert [call[0] for call in calls] == [
        "https://fx-api-testnet.gateio.ws/api/v4/account/detail",
        "https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/accounts",
    ]
    for url, options in calls:
        assert options["allow_redirects"] is False
        assert options["timeout"] == (3.05, 10)
        endpoint = url.removeprefix("https://fx-api-testnet.gateio.ws")
        payload_hash = hashlib.sha512(b"").hexdigest()
        signed = f"GET\n{endpoint}\n\n{payload_hash}\n1700000000".encode()
        assert options["headers"]["SIGN"] == hmac.new(
            material.api_secret.encode(), signed, hashlib.sha512,
        ).hexdigest()
    assert result.capabilities["futures_usdt_read"] is True
    assert result.capabilities["trading_ready"] is False
    assert result.capabilities["trade_permission"] == "UNVERIFIED"
    assert result.warning_codes == ("API_PERMISSION_SCOPE_UNVERIFIED",)


def test_probe_honors_bounded_retry_after_without_retrying_in_transport() -> None:
    calls = 0

    def transport(url: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(429, {}, {"Retry-After": "900"})

    with pytest.raises(GateRateLimited) as caught:
        GateCredentialProbe(transport=transport).probe(
            "TESTNET", CredentialMaterial("test-key-123456789", "test-secret-123456789"),
        )
    assert caught.value.retry_after_seconds == 300
    assert calls == 1


def test_probe_rejects_non_allowlisted_environment_before_network() -> None:
    def transport(url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("network must not be called")

    with pytest.raises(SafeGateProbeError, match="GATE_ENVIRONMENT_NOT_ALLOWED"):
        GateCredentialProbe(transport=transport).probe(
            "LIVE", CredentialMaterial("test-key-123456789", "test-secret-123456789"),
        )
