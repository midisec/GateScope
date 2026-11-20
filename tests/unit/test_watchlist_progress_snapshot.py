"""Progress polling must never turn the web API into a full-history scanner."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.domain.history import listing_year_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS


AS_OF = datetime(2026, 9, 12, 10, 0, 30, tzinfo=timezone.utc)


class ProgressPool:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.identities = []

    @contextmanager
    def transaction(self, tenant, user):
        self.identities.append((tenant, user))
        yield self

    def execute(self, query, params):
        assert "WITH selected AS" in query
        assert "count(DISTINCT open_time)" not in query
        self.calls.append((query, params))
        return self

    def fetchall(self):
        return [self.row]


def progress_setup(monkeypatch, timeframe="1m", *, stored_listing=None, latest_listing=None, initialized=True):
    instrument, tenant, user = uuid4(), uuid4(), uuid4()
    stored = listing_year_bounds(AS_OF, timeframe, listing_time=stored_listing)
    step = TIMEFRAME_SECONDS[timeframe]
    start = datetime.fromtimestamp(stored["start"], timezone.utc)
    end = datetime.fromtimestamp(stored["end"]+step, timezone.utc)
    cached = min(1234, stored["target_bars"])
    row = (instrument, "龙虾_USDT", timeframe, cached, stored["target_bars"], stored["target_bars"]-cached,
           "BACKFILLING", None, "RUNNING", 100, cached, None,
           start if initialized else None, end if initialized else None, None,
           None, AS_OF, AS_OF if initialized else None, 0, 1)
    pool = ProgressPool(row)
    clock = [AS_OF]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(studies, "datetime", Clock)
    monkeypatch.setattr(studies, "history_bounds", lambda conn, pair, period, now: listing_year_bounds(
        now, period, listing_time=latest_listing))
    return studies.PatternStudyRepository(pool), pool, clock, tenant, user


@pytest.mark.parametrize("timeframe", ["1m", "1h", "1d"])
def test_time_advance_keeps_matching_persisted_numerator_denominator_without_bar_scan(monkeypatch, timeframe):
    repo, pool, clock, tenant, user = progress_setup(monkeypatch, timeframe)
    before = repo.watchlist_progress(tenant, user)["items"][0]
    for seconds in (60, 3600, 86400):
        clock[0] = AS_OF + timedelta(seconds=seconds)
        after = repo.watchlist_progress(tenant, user)["items"][0]
        assert after == before
    assert before["range_refresh_pending"] is False
    assert datetime.fromisoformat(before["coverage_asof"]) == AS_OF
    assert len(pool.calls) == 4 and pool.identities == [(tenant, user)] * 4


def test_new_listing_keeps_old_snapshot_until_background_worker_recomputes(monkeypatch):
    listing = int((AS_OF-timedelta(days=20)).timestamp())
    repo, pool, _, tenant, user = progress_setup(monkeypatch, latest_listing=listing)
    item = repo.watchlist_progress(tenant, user)["items"][0]
    assert item["listing_time"] == listing and item["range_refresh_pending"] is True
    assert item["range_policy"] == "ROLLING_YEAR"
    assert item["target_bars"] == 525600 and item["cached_bars"] == 1234
    assert item["coverage_ratio"] == 1234 / 525600
    assert datetime.fromisoformat(item["coverage_asof"]) == AS_OF
    assert len(pool.calls) == 1


def test_clipped_snapshot_is_not_recounted_as_target_grows_each_minute(monkeypatch):
    listing = int((AS_OF-timedelta(days=20)).timestamp())
    repo, pool, clock, tenant, user = progress_setup(monkeypatch, stored_listing=listing, latest_listing=listing)
    before = repo.watchlist_progress(tenant, user)["items"][0]
    clock[0] = AS_OF + timedelta(hours=3)
    after = repo.watchlist_progress(tenant, user)["items"][0]
    assert after == before and after["range_refresh_pending"] is False
    assert after["range_policy"] == "LISTING_CLIPPED" and after["target_bars"] < 525600
    assert len(pool.calls) == 2


def test_uninitialized_snapshot_has_no_fabricated_count_or_sync_scan(monkeypatch):
    repo, pool, _, tenant, user = progress_setup(monkeypatch, initialized=False)
    item = repo.watchlist_progress(tenant, user)["items"][0]
    assert item["coverage_asof"] is None and item["range_refresh_pending"] is True
    assert item["cached_bars"] == 0 and item["coverage_ratio"] == 0
    assert item["target_bars"] == item["missing_bars"] == 525600
    assert len(pool.calls) == 1
