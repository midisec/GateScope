import copy

import pytest

from gatescope.domain.coinglass_cache import (
    cached_evidence,
    merge_refresh,
    project_cache,
    project_refresh_state,
)


def good_bundle():
    return {
        "status": "READY",
        "instrument_id": "test-instrument",
        "timeframe": "1h",
        "scope": {"exchange": "Gate", "symbol": "BTC_USDT", "interval": "1h"},
        "match": {"status": "EXACT"},
        "fetched_at": 1000,
        "expires_at": 1900,
        "as_of": 900,
        "series": [
            {
                "id": "oi",
                "status": "READY",
                "unit": "USD",
                "unit_status": "VERIFIED",
                "native_timeframe": "1h",
                "points": [{"close_time": 900, "value": 12, "available_at": 1000}],
            }
        ],
    }


def merge(old, new):
    return merge_refresh(
        old, new, instrument_id="test-instrument", timeframe="1h", now=2000, ttl=900, retry_at=2060
    )


def test_transient_refresh_retains_original_evidence_time_and_does_not_mutate():
    old = good_bundle()
    before = copy.deepcopy(old)
    new = {"status": "RATE_LIMITED", "series": [], "retry_after_seconds": 60}
    result = merge(old, new)
    assert old == before and "instrument_id" not in new
    assert result["status"] == "STALE" and result["cache_retained"]
    assert result["fetched_at"] == 1000 and result["expires_at"] == 1900 and result["as_of"] == 900
    assert result["last_attempt_at"] == 2000 and result["refresh_status"] == "RATE_LIMITED"
    series = result["series"][0]
    assert series["status"] == "STALE" and series["points"] == old["series"][0]["points"]
    assert series["fetched_at"] == 1000 and series["expires_at"] == 1900


@pytest.mark.parametrize(
    "change",
    [
        {"status": "UNSUPPORTED"},
        {"match": {"status": "MISMATCH"}},
        {"scope": {"symbol": "ETH_USDT"}},
        {"instrument_id": "different"},
        {"timeframe": "4h"},
    ],
)
def test_explicit_identity_scope_or_support_failure_does_not_retain(change):
    result = merge(good_bundle(), {"status": "ERROR", "series": [], **change})
    assert not result["cache_retained"] and not result["series"]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "MISMATCH"},
        {"unit": "BASE"},
        {"unit_status": "UNKNOWN"},
        {"native_timeframe": "4h"},
        {"population": "other"},
    ],
)
def test_metric_mismatch_does_not_retain(change):
    result = merge(
        good_bundle(),
        {"status": "PARTIAL", "series": [{"id": "oi", "status": "ERROR", "points": [], **change}]},
    )
    assert not result["cache_retained"] and not result["series"][0]["points"]


def test_mixed_refresh_keeps_separate_series_evidence_times():
    new = {
        "status": "PARTIAL",
        "fetched_at": 2000,
        "series": [
            {"id": "oi", "status": "ERROR", "points": []},
            {
                "id": "lsr",
                "status": "READY",
                "points": [{"close_time": 1800, "available_at": 2000, "value": 1.2}],
            },
        ],
    }
    result = merge(good_bundle(), new)
    assert result["status"] == "PARTIAL" and result["fetched_at"] == 2000
    assert result["series"][0]["status"] == "STALE" and result["series"][0]["fetched_at"] == 1000
    assert result["series"][1]["status"] == "READY" and result["series"][1]["fetched_at"] == 2000


@pytest.mark.parametrize("requested,status", [(True, "QUEUED"), (False, "DEFERRED")])
def test_legacy_rate_limit_expires_from_old_attempt_not_read_time(requested, status):
    old = {
        "status": "RATE_LIMITED",
        "retry_after_seconds": 10,
        "fetched_at": 1000,
        "safe_errors": [{"group": "oi", "code": "PROVIDER_RATE_LIMITED"}],
        "series": [{"status": "RATE_LIMITED", "points": []}],
    }
    result = project_cache(old, now=26200, requested=requested, due_at=1060, last_updated=1000)
    assert result["status"] == status and result["refresh_status"] == status
    assert result["retry_at"] == 1010 and result["retry_after_seconds"] == 0
    assert result["last_attempt_at"] == 1000 and result["last_refresh_status"] == "RATE_LIMITED"
    assert result["safe_errors"] == [] and result["last_safe_errors"] == old["safe_errors"]
    assert result["series"][0]["status"] == "DEFERRED" and old["status"] == "RATE_LIMITED"


def test_active_cooldown_countdown_and_requested_due_are_not_shortened():
    old = {"status": "RATE_LIMITED", "retry_at": 2100, "retry_after_seconds": 100, "last_attempt_at": 2000}
    first = project_cache(old, now=2050, requested=True, due_at=2400)
    second = project_cache(old, now=2090, requested=True, due_at=2400)
    assert first["retry_after_seconds"] == 50 and second["retry_after_seconds"] == 10
    assert first["next_refresh_at"] == second["next_refresh_at"] == 2400
    assert second["refresh_status"] == "RATE_LIMITED"


def test_provider_cooldown_applies_across_cached_contexts():
    result = project_cache(good_bundle(), now=1500, requested=True, due_at=1400, provider_retry_at=2000)
    assert result["retry_after_seconds"] == 500 and result["next_refresh_at"] == 2000
    assert result["status"] == "READY"  # existing fresh evidence remains usable


def test_partial_cached_error_does_not_keep_expired_series_limit_current():
    payload = {
        "status": "PARTIAL",
        "expires_at": 2500,
        "retry_at": 2000,
        "last_attempt_at": 1940,
        "safe_errors": [{"group": "oi", "code": "PROVIDER_RATE_LIMITED"}],
        "series": [{"status": "RATE_LIMITED", "quality_flags": ["PROVIDER_RATE_LIMITED"], "points": []}],
    }
    result = project_cache(payload, now=2100, requested=True, due_at=2400)
    assert result["status"] == "PARTIAL" and result["refresh_status"] == "QUEUED"
    assert result["last_refresh_status"] == "PARTIAL" and result["safe_errors"] == []
    assert result["series"][0]["status"] == "DEFERRED" and result["series"][0]["quality_flags"] == []
    assert result["series"][0]["last_quality_flags"] == ["PROVIDER_RATE_LIMITED"]


@pytest.mark.parametrize(
    "exists,eligible,lease,due,retry,expected,poll",
    [
        (False, False, 0, 0, 0, "NOT_REQUESTED", 0),
        (True, False, 0, 0, 0, "IDLE", 0),
        (True, True, 0, 1900, 0, "QUEUED", 5),
        (True, True, 0, 2100, 0, "SCHEDULED", 100),
        (True, True, 0, 2600, 2300, "COOLDOWN", 300),
        (True, True, 2100, 1900, 0, "COLLECTING", 3),
    ],
)
def test_queue_state_comes_from_actual_scheduling_not_call_request(
    exists, eligible, lease, due, retry, expected, poll
):
    payload = {"status": "NOT_CACHED", "next_refresh_at": due, "retry_at": retry, "refresh_requested": False}
    result = project_refresh_state(
        payload,
        now=2000,
        exists=exists,
        eligible=eligible,
        pending_since=1800 if eligible else 0,
        lease_until=lease,
        collection_started_at=1950,
    )
    assert result["refresh_state"] == expected and result["next_poll_after_seconds"] == poll
    assert result["queue_age_seconds"] == (200 if eligible else 0)
    assert result["collection_started_at"] == (1950 if lease else None)
    assert result["status"] == "NOT_CACHED" and result["refresh_requested"] is False


def test_cache_availability_requires_real_same_dimension_points():
    from tests.unit.test_coinglass_context import NOW, bundle

    payload = bundle()
    count, latest = cached_evidence(payload, now=NOW)
    assert count == 2 and latest == NOW
    payload["series"][0]["status"] = "STALE"
    result = project_refresh_state(payload, now=NOW + 10, exists=True, eligible=True, pending_since=NOW)
    assert result["cache_available"] and result["cached_points_count"] == 2
    assert result["last_success_at"] == NOW
    for change in (
        {"unit_status": "UNKNOWN"},
        {"unit": "BASE"},
        {"population": "OTHER"},
        {"native_timeframe": "4h"},
        {"status": "ERROR"},
    ):
        modified = copy.deepcopy(payload)
        modified["series"][0].update(change)
        assert cached_evidence(modified, now=NOW) == (0, 0)
    assert cached_evidence(payload, now=NOW - 1) == (0, 0)
    for change in ({"scope": None}, {"scope": {}}, {"instrument_id": ""}, {"timeframe": None}):
        assert cached_evidence({**payload, **change}, now=NOW) == (0, 0)
