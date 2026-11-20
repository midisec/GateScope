from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

import gatescope.application.mcp_tools as module
from tests.unit.test_mcp_market_export import IDENTIFIER, principal, service


NOW = 1_800_000_000


def queued(**changes):
    return {
        "instrument_id": IDENTIFIER, "timeframe": "15m", "status": "QUEUED",
        "refresh_state": "QUEUED", "queued_at": NOW - 10, "queue_age_seconds": 10,
        "collection_started_at": None, "next_eligible_at": NOW, "next_poll_after_seconds": 5,
        "match": {"status": "UNSUPPORTED"}, "scope": None, "series": [],
        "retry_at": 0, "expires_at": 0, "refresh_requested": True, **changes,
    }


def partial(**changes):
    return queued(
        status="PARTIAL", refresh_state="COLLECTING", collection_started_at=NOW,
        match={"status": "EXACT"}, scope={"exchange": "Gate", "symbol": "BTC_USDT"},
        fetched_at=NOW, expires_at=NOW + 600,
        series=[{
            "id": "oi", "status": "READY", "unit": "USD", "unit_status": "VERIFIED",
            "population": "OPEN_INTEREST_MARK_VALUE", "native_timeframe": "15m",
            "fetched_at": NOW, "expires_at": NOW + 600,
            "points": [{"time": NOW - 1800, "close_time": NOW - 900,
                        "available_at": NOW - 100, "value": 100}],
        }], **changes,
    )


class Cache:
    def __init__(self, *snapshots):
        self.snapshots = snapshots
        self.calls = []

    def get(self, tenant, user, instrument, timeframe, request=False):
        result = self.snapshots[min(len(self.calls), len(self.snapshots) - 1)]
        self.calls.append((tenant, user, instrument, timeframe, request))
        return deepcopy(result)


@pytest.fixture
def fake_clock(monkeypatch):
    state = SimpleNamespace(elapsed=0.0, sleeps=[])

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.elapsed += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(
        monotonic=lambda: state.elapsed, time=lambda: NOW + state.elapsed, sleep=sleep,
    ))
    return state


def call(cache, **arguments):
    instance, access = service(coinglass=cache)
    result = instance.coinglass_get_snapshot(principal("analysis:read"), instrument_id=IDENTIFIER, **arguments)
    return result["coinglass"], access


def test_wait_publishes_first_metric_without_waiting_for_whole_batch(fake_clock):
    cache = Cache(queued(), queued(refresh_state="COLLECTING"), partial())
    context, access = call(cache)
    assert fake_clock.elapsed == 2
    assert [row[-1] for row in cache.calls] == [True, False, False]
    assert all(row[2:4] == (UUID(IDENTIFIER), "15m") for row in cache.calls)
    assert context["refresh_state"] == "COLLECTING"
    assert context["availability"]["ready_metric_ids"] == ["oi"]
    assert context["availability"]["unavailable_metric_ids"] == ["funding", "lsr", "liquidation_long", "liquidation_short"]
    assert context["cache_wait"] == {
        "budget_seconds": 15, "waited_seconds": 2, "cache_reads": 3, "inline_provider_calls": 0,
    }
    assert len(access.audits) == 1


def test_queue_wait_has_hard_iteration_and_sleep_budget(fake_clock):
    cache = Cache(queued())
    context, _ = call(cache)
    assert fake_clock.elapsed == 15
    assert len(cache.calls) == 15 and sum(row[-1] for row in cache.calls) == 1
    assert context["status"] == "QUEUED"
    assert context["availability"]["available_count"] == 0
    assert context["queued_at"] == NOW - 10


@pytest.mark.parametrize("snapshot", [
    queued(refresh_state="COOLDOWN", retry_at=NOW + 60),
    queued(refresh_state="SCHEDULED", next_eligible_at=NOW + 60),
    queued(refresh_state="NOT_REQUESTED", status="NO_CONFIG"),
    queued(refresh_state="IDLE", status="ERROR"),
])
def test_no_pointless_wait_for_cooldown_disabled_or_future_schedule(fake_clock, snapshot):
    cache = Cache(snapshot)
    context, _ = call(cache)
    assert fake_clock.sleeps == [] and len(cache.calls) == 1
    assert context["availability"]["available_count"] == 0


@pytest.mark.parametrize("stale", [False, True])
def test_existing_data_returns_immediately_and_stale_is_reference_only(fake_clock, stale):
    snapshot = partial()
    if stale:
        snapshot["status"] = "STALE"
        snapshot["series"][0]["status"] = "STALE"
        snapshot["series"][0]["expires_at"] = NOW - 1
    context, _ = call(Cache(snapshot))
    assert fake_clock.sleeps == []
    assert context["availability"]["available_metric_ids"] == ["oi"]
    assert context["availability"]["ready_count"] == (0 if stale else 1)
    assert context["availability"]["metrics"][0]["reference_only"] is stale


@pytest.mark.parametrize("mutation", [
    {"unit": "PERCENT"}, {"unit_status": "UNKNOWN"}, {"population": "DIFFERENT"},
    {"native_timeframe": "1h"}, {"status": "MISMATCH"},
    {"points": [{"time": NOW - 1800, "close_time": NOW - 900, "available_at": NOW + 60, "value": 1}]},
    {"points": [{"time": NOW - 1800, "close_time": NOW - 1790, "available_at": NOW - 100, "value": 1}]},
])
def test_invalid_series_cannot_be_counted_as_available(fake_clock, mutation):
    snapshot = partial()
    snapshot["series"][0].update(mutation)
    context, _ = call(Cache(snapshot), wait_seconds=0)
    assert context["availability"]["available_count"] == 0
    assert fake_clock.sleeps == []


@pytest.mark.parametrize("wait", [-1, 16, True, 1.5, "5"])
def test_wait_argument_is_bounded_before_reading_cache(wait):
    cache = Cache(queued())
    with pytest.raises(ValueError, match="wait_seconds"):
        call(cache, wait_seconds=wait)
    assert cache.calls == []


def test_zero_wait_and_combined_market_preview_never_wait(fake_clock):
    cache = Cache(queued())
    context, _ = call(cache, wait_seconds=0)
    assert context["cache_wait"]["cache_reads"] == 1
    instance, _ = service(coinglass=cache)
    instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert fake_clock.sleeps == [] and len(cache.calls) == 2


def test_new_status_fields_exported_but_secrets_are_not(fake_clock):
    source = partial()
    source.update(cache_available=True, cached_points_count=1, last_success_at=NOW,
                  api_key="private-value", account={"remaining": 10})
    context, _ = call(Cache(source))
    for field in ("refresh_state", "queued_at", "collection_started_at", "next_eligible_at",
                  "next_poll_after_seconds", "cache_available", "cached_points_count", "last_success_at"):
        assert context[field] == source[field]
    assert "private-value" not in repr(context) and "remaining" not in repr(context)


@pytest.mark.parametrize("mutation", [
    {"scope": None}, {"scope": {}}, {"instrument_id": str(UUID(int=123))}, {"timeframe": "1h"},
])
def test_scope_or_top_level_identity_mismatch_is_not_available(fake_clock, mutation):
    snapshot = partial()
    snapshot.update(mutation)
    context, _ = call(Cache(snapshot), wait_seconds=0)
    assert context["availability"]["available_count"] == 0


def test_slow_initial_read_consumes_wait_budget_and_expiry_prevents_another_read(fake_clock):
    class SlowCache(Cache):
        def get(self, *args, **kwargs):
            fake_clock.elapsed += 14.5
            return super().get(*args, **kwargs)

    cache = SlowCache(queued())
    context, _ = call(cache)
    assert len(cache.calls) == 1
    assert fake_clock.elapsed == 15
    assert fake_clock.sleeps == [0.5]
    assert context["cache_wait"]["waited_seconds"] == 15
