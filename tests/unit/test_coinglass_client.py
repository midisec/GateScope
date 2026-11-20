from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from gatescope.adapters.coinglass import (
    COINGLASS_ORIGIN,
    CoinglassBusinessError,
    CoinglassClient,
    CoinglassRateLimited,
    CoinglassSchemaError,
    CoinglassSecurityError,
    CoinglassUnavailable,
)

API_KEY = "cg_" + "A" * 40


class FakeResponse:
    def __init__(
        self,
        status: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
        invalid_json: bool = False,
    ) -> None:
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}
        self.invalid_json = invalid_json
        self.content = json.dumps(payload).encode()

    def json(self) -> Any:
        if self.invalid_json:
            raise requests.JSONDecodeError("invalid", "x", 0)
        return self.payload


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_fixed_origin_header_injection_and_business_code() -> None:
    session = FakeSession(FakeResponse(200, {"code": "0", "data": [{"symbol": "BTC"}]}))
    result = CoinglassClient(API_KEY, session=session).get_coins_markets(per_page=10)
    assert result.data == [{"symbol": "BTC"}]
    request = session.calls[0]
    assert request["url"] == f"{COINGLASS_ORIGIN}/api/futures/coins-markets?per_page=10"
    assert API_KEY not in request["url"]
    assert request["headers"]["X-API-Key"] == API_KEY
    assert request["allow_redirects"] is False


def test_lsr_unwrapped_shape_is_allowed_only_for_lsr() -> None:
    session = FakeSession(FakeResponse(200, {"symbol": "BTC", "calculated_at": 1_700_000_000}))
    result = CoinglassClient(API_KEY, session=session).get_lsr_signal("btc")
    assert result.data["symbol"] == "BTC"
    assert session.calls[0]["url"].endswith("/api/lsr/signal/BTC?mode=trader")

    malformed = FakeSession(FakeResponse(200, {"items": []}))
    with pytest.raises(CoinglassSchemaError):
        CoinglassClient(API_KEY, session=malformed).get_coins_markets()


def test_http_200_nonzero_business_code_is_failure_without_secret_leak() -> None:
    session = FakeSession(FakeResponse(200, {"code": 1007, "message": API_KEY}))
    with pytest.raises(CoinglassBusinessError) as raised:
        CoinglassClient(API_KEY, session=session).get_coins_markets()
    assert API_KEY not in str(raised.value)


def test_redirect_is_rejected_and_opens_circuit() -> None:
    session = FakeSession(FakeResponse(302, {}, headers={"Location": "https://example.com/steal"}))
    client = CoinglassClient(API_KEY, session=session)
    with pytest.raises(CoinglassSecurityError, match="redirect"):
        client.get_coins_markets()
    with pytest.raises(CoinglassUnavailable, match="circuit"):
        client.get_coins_markets()
    assert len(session.calls) == 1


def test_429_honors_retry_after_with_two_total_attempts() -> None:
    session = FakeSession(
        FakeResponse(429, {}, headers={"Retry-After": "2"}),
        FakeResponse(200, {"code": 0, "data": []}),
    )
    sleeps: list[float] = []
    result = CoinglassClient(API_KEY, session=session, sleep=sleeps.append).get_coins_markets()
    assert result.data == []
    assert sleeps == [2.0]
    assert len(session.calls) == 2

    deferred = FakeSession(FakeResponse(429, {}, headers={"Retry-After": "120"}))
    with pytest.raises(CoinglassRateLimited) as raised:
        CoinglassClient(API_KEY, session=deferred).get_coins_markets()
    assert raised.value.retry_after_seconds == 120
    assert len(deferred.calls) == 1


def test_account_capabilities_are_cached_for_sixty_seconds() -> None:
    session = FakeSession(FakeResponse(200, {"code": 0, "data": {"permissions": ["lsr"]}}))
    client = CoinglassClient(API_KEY, session=session)
    first = client.get_account()
    second = client.get_account()
    assert first is second
    assert len(session.calls) == 1
