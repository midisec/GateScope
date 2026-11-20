from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from gatescope.adapters.coinglass import (
    CoinglassClient, CoinglassRateLimited, CoinglassRequestPacer,
    CoinglassUnavailable, _retry_after,
)
from tests.unit.test_coinglass_client import API_KEY, FakeResponse, FakeSession


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def test_account_catalog_and_history_share_one_budget_across_clients():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    first_session = FakeSession(FakeResponse(200, {"code": "0", "data": {}}))
    second_session = FakeSession(*[FakeResponse(200, {"code": "0", "data": []}) for _ in range(2)])
    first = CoinglassClient(API_KEY, session=first_session, pacer=pacer, monotonic=clock)
    second = CoinglassClient(API_KEY, session=second_session, pacer=pacer, monotonic=clock)
    first.get_account()
    second._get("/api/futures/supported-exchange-pairs", {})
    second._get("/api/futures/open-interest/history", {}, deadline_seconds=3, max_attempts=1)
    assert clock.waits == [6.0, 6.0]
    assert len(first_session.calls) + len(second_session.calls) == 3
    assert API_KEY not in repr(pacer.states)


def test_different_credentials_and_lsr_have_independent_pacing_slots():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    pacer.acquire("credential-one", "coinglass")
    pacer.acquire("credential-two", "coinglass")
    pacer.acquire("credential-one", "lsr")
    assert not clock.waits


def test_account_rate_metadata_paces_first_history_after_catalog():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    session = FakeSession(*[FakeResponse(200, {"code": "0", "data": []}) for _ in range(2)])
    client = CoinglassClient(API_KEY, session=session, pacer=pacer, monotonic=clock)
    client._get("/api/futures/supported-exchange-pairs", {})
    client.configure_rate_limits({"rate_limits": {"coinglass": {"limit": 11, "window_seconds": 60}}})
    client._get("/api/futures/open-interest/history", {}, deadline_seconds=3, max_attempts=1)
    assert clock.waits == pytest.approx([60 / 11 * 1.15])


def test_429_blocks_other_client_and_other_endpoint_without_sleeping_or_http():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    session = FakeSession(FakeResponse(429, {}, headers={"Retry-After": "60"}))
    blocked_session = FakeSession()
    first = CoinglassClient(API_KEY, session=session, pacer=pacer, monotonic=clock)
    second = CoinglassClient(API_KEY, session=blocked_session, pacer=pacer, monotonic=clock)
    with pytest.raises(CoinglassRateLimited):
        first._get("/api/futures/open-interest/history", {}, max_attempts=1)
    with pytest.raises(CoinglassRateLimited) as limited:
        second.get_lsr_signal("BTC")
    assert limited.value.retry_after_seconds == 60
    assert not blocked_session.calls and not clock.waits


def test_waiter_observes_429_received_during_its_pacing_sleep():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    pacer.acquire("same-key", "coinglass")

    def rate_limited_while_waiting(seconds):
        pacer.defer("same-key", 60)
        clock.sleep(seconds)

    pacer.sleep = rate_limited_while_waiting
    with pytest.raises(CoinglassRateLimited) as limited:
        pacer.acquire("same-key", "coinglass")
    assert limited.value.retry_after_seconds == 54


def test_pacing_wait_is_bounded_instead_of_sleeping_for_an_hour():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    pacer.configure("key", "coinglass", limit=1, window=3600)
    pacer.acquire("key", "coinglass")
    with pytest.raises(CoinglassRateLimited) as limited:
        pacer.acquire("key", "coinglass")
    assert limited.value.retry_after_seconds >= 3600
    assert not clock.waits


def test_capacity_never_evicts_an_active_cooldown():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    pacer.MAX_KEYS = 2
    pacer.defer("first", 60)
    with pytest.raises(CoinglassUnavailable):
        pacer.acquire("second", "coinglass")
    assert len(pacer.states) == 2
    clock.now += 61
    pacer.acquire("second", "coinglass")
    assert len(pacer.states) == 2


def test_persistent_gate_is_rechecked_after_wait_and_before_http():
    clock = Clock()
    pacer = CoinglassRequestPacer(monotonic=clock, sleep=clock.sleep)
    session = FakeSession(FakeResponse(200, {"code": "0", "data": []}))
    client = CoinglassClient(API_KEY, session=session, pacer=pacer, monotonic=clock)
    client.get_coins_markets()

    def persistent_gate(_path):
        if clock() > 100:
            raise CoinglassRateLimited(30)

    client.set_request_hooks(before_request=persistent_gate)
    with pytest.raises(CoinglassRateLimited):
        client.get_coins_markets()
    assert len(session.calls) == 1


def test_http_200_business_429_defers_and_calls_persistent_pause_hook():
    seen = []
    session = FakeSession(FakeResponse(200, {"code": "429", "msg": API_KEY}, headers={"Retry-After": "120"}))
    client = CoinglassClient(API_KEY, session=session, on_rate_limit=lambda path, retry: seen.append((path, retry)))
    with pytest.raises(CoinglassRateLimited) as error:
        client.get_coins_markets()
    assert seen == [("/api/futures/coins-markets", 120)]
    assert len(session.calls) == 1 and API_KEY not in str(error.value)


@pytest.mark.parametrize("value, expected", [("3601", 3601), ("86400", 86400), ("0", 1), (None, 1), ("invalid", 1)])
def test_retry_after_is_not_shortened_to_one_hour(value, expected):
    assert _retry_after(value) == expected


def test_retry_after_http_date_beyond_one_hour_is_preserved():
    at = datetime.now(timezone.utc) + timedelta(hours=2)
    assert 7198 <= _retry_after(format_datetime(at, usegmt=True)) <= 7200
