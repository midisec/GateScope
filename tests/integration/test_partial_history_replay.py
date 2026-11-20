"""Partial public cache replay with real SQL roles and immutable archives.

The lifecycle engine is real; a deterministic synthetic detector keeps these
queue/snapshot tests fast. Detector accuracy is covered by its separate suite.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import (
    MarketSubscription,
    PostgresMarketIngestRepository,
)
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.quant.structure_lifecycle import observe
from tests.integration.test_postgres_rls import (
    INSTRUMENT,
    TENANT_A,
    TENANT_B,
    USER_A,
    USER_B,
    seed_two_tenants,
)
from tests.unit.test_structure_lifecycle import candidate


@contextmanager
def partial_cache(url, monkeypatch, history_status="RETRYING"):
    seed_two_tenants(url)
    pools = {
        role: PostgresPool(make_conninfo(url, options=f"-c role=gatescope_{role}"), max_size=1)
        for role in ("market", "strategy", "api")
    }
    for pool in pools.values():
        pool.open()
    try:
        now = datetime.now(timezone.utc)
        last = int(now.timestamp()) // 3600 * 3600 - 4 * 3600
        rows = [(last - (79 - i) * 3600, 100., 105., 95., 101., 10.) for i in range(80)]
        sub = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h")
        ingest = PostgresMarketIngestRepository(pools["market"])
        ingest.sync_states([sub])
        ingest.upsert_final_bars(sub, rows, observed_at=now)
        ingest.mark_history(sub, ingest.history_plan(sub, now), now, state=history_status,
                            delay=900, error="MARKET_PROVIDER_UNAVAILABLE")
        monkeypatch.setattr(studies, "observe", lambda bars, index, state, **kwargs: observe(
            bars, index, state,
            detector=lambda prefix: [candidate(prefix)] if len(prefix) >= 20 else [], **kwargs,
        ))
        yield PatternStudyRepository(pools["strategy"]), PatternStudyRepository(pools["api"]), ingest, sub, rows
    finally:
        for pool in pools.values():
            pool.close()


def reschedule(url, repo):
    with psycopg.connect(url) as conn:
        conn.execute("UPDATE market_pattern_studies SET updated_at=now()-interval '16 minutes'")
    repo._scheduled_at = 0
    repo.schedule()


def finish(repo):
    for _ in range(10):
        if not repo.run_slice(seconds=5, max_steps=64):
            return
    pytest.fail("bounded test replay did not finish")


def frozen_facts(url, context):
    with psycopg.connect(url) as conn:
        events = conn.execute("""SELECT candidate_id,sequence,event_json FROM market_pattern_events
            WHERE context_id=%s ORDER BY candidate_id,sequence""", (context,)).fetchall()
        reports = conn.execute("""SELECT snapshot_at,report_json FROM market_pattern_report_versions
            WHERE context_id=%s ORDER BY snapshot_at""", (context,)).fetchall()
    return events, reports


def add_listing(url, listing_time):
    now = datetime.now(timezone.utc)
    with psycopg.connect(url) as conn:
        conn.execute("""INSERT INTO instrument_revisions
            (id,instrument_id,revision,effective_at,available_at,metadata_json,payload_hash,created_at)
            VALUES(gen_random_uuid(),%s,1,%s,%s,%s,%s,%s)""",
            (INSTRUMENT, now, now, Jsonb({"name": "BTC_USDT", "create_time": listing_time}), str(uuid4()), now))


def add_watchlist(url):
    with psycopg.connect(url) as conn:
        watch = conn.execute("""INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at)
            VALUES(gen_random_uuid(),%s,%s,'partial',now(),now()) RETURNING id""", (TENANT_A, USER_A)).fetchone()[0]
        conn.execute("""INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,
            default_timeframe,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,%s,0,'1h',now(),now())""",
            (TENANT_A, watch, INSTRUMENT))


@pytest.mark.parametrize("history_status", ["RETRYING", "BACKFILLING", "PENDING"])
def test_partial_cache_starts_while_downloader_is_not_ready(migrated_database, monkeypatch, history_status):
    with partial_cache(migrated_database, monkeypatch, history_status) as (repo, read, _, _, _):
        repo.schedule()
        assert read.get(INSTRUMENT, "1h")["state"] == "QUEUED"
        finish(repo)
        result = read.get(INSTRUMENT, "1h")
        assert result["state"] == "READY" and result["cursor"] == 80
        assert result["report"]["metadata"]["cached_bars"] == 80
        assert result["report"]["metadata"]["missing_bars"] > 0
        assert result["report"]["metadata"]["coverage_ratio"] < 1
        assert result["report"]["partial"] is False  # complete replay of this partial cache snapshot


def test_inflight_snapshot_finishes_then_older_backfill_gets_new_context(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, rows):
        repo.schedule()
        assert repo.run_slice(max_steps=20)
        running = read.get(INSTRUMENT, "1h")
        assert running["state"] == "RUNNING" and running["total"] == 80
        earlier = [(rows[0][0] - (20-i)*3600, 100., 105., 95., 101., 10.) for i in range(20)]
        ingest.upsert_final_bars(sub, earlier, observed_at=datetime.now(timezone.utc))
        reschedule(migrated_database, repo)
        still_running = read.get(INSTRUMENT, "1h")
        assert (still_running["requested_at"], still_running["cursor"], still_running["total"]) == (
            running["requested_at"], running["cursor"], 80)
        finish(repo)
        completed = read.get(INSTRUMENT, "1h")
        old_context = completed["report"]["metadata"]["context_id"]
        before = frozen_facts(migrated_database, old_context)
        assert before[0] and before[1] and completed["cursor"] == 80
        reschedule(migrated_database, repo)
        reset = read.get(INSTRUMENT, "1h")
        assert reset["state"] == "QUEUED" and reset["cursor"] == 0
        assert reset["requested_at"] > running["requested_at"]
        finish(repo)
        completed = read.get(INSTRUMENT, "1h")
        assert completed["cursor"] == 100
        assert completed["report"]["metadata"]["context_id"] != old_context
        assert completed["report"]["from_time"] == earlier[0][0]
        assert frozen_facts(migrated_database, old_context) == before


def test_future_unavailable_revision_does_not_reset_completed_snapshot(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, rows):
        repo.schedule()
        finish(repo)
        before = read.get(INSTRUMENT, "1h")
        future = datetime.now(timezone.utc) + timedelta(days=1)
        revised = (rows[0][0], 100., 106., 95., 102., 10.)
        assert ingest.upsert_final_bars(sub, [revised], observed_at=future) == 1
        reschedule(migrated_database, repo)
        after = read.get(INSTRUMENT, "1h")
        assert after["state"] == "READY" and after["requested_at"] == before["requested_at"]
        assert after["report"] == before["report"]


def test_partial_progress_retains_error_and_user_watchlist_isolation(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, _):
        now = datetime.now(timezone.utc)
        ingest.mark_live(sub, now, error=None)
        add_watchlist(migrated_database)
        repo.schedule()
        result = read.watchlist_progress(TENANT_A, USER_A)
        item = next(row for row in result["items"] if row["timeframe"] == "1h")
        assert result["total"] == 8 and item["state"] == "QUEUED"
        assert item["cached_bars"] == 80 and item["target_bars"] == 8760
        assert item["history_status"] == "RETRYING" and item["history_error_code"] == "MARKET_PROVIDER_UNAVAILABLE"
        assert item["listing_time"] is None and item["range_policy"] == "ROLLING_YEAR"
        assert item["replay_error"] is None and item["replay_retry_exhausted"] is False
        assert item["live_error_code"] is None and datetime.fromisoformat(item["last_live_at"]) == now
        assert read.watchlist_progress(TENANT_B, USER_B)["items"] == []


def test_new_listing_metadata_starts_new_context_and_progress_waits_for_matching_snapshot(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, rows):
        repo.schedule()
        finish(repo)
        before = read.get(INSTRUMENT, "1h")
        old_context = before["report"]["metadata"]["context_id"]
        old_facts = frozen_facts(migrated_database, old_context)
        listed = rows[0][0] - 20 * 3600 + 123
        add_listing(migrated_database, listed)
        add_watchlist(migrated_database)
        # Keep the old denominator and count together until the background
        # worker publishes the newly clipped snapshot. No web-time full scan.
        item = next(row for row in read.watchlist_progress(TENANT_A, USER_A)["items"] if row["timeframe"] == "1h")
        assert item["listing_time"] == listed and item["range_policy"] == "ROLLING_YEAR"
        assert item["range_refresh_pending"] is True and item["coverage_asof"] is not None
        assert item["target_bars"] == 8760 and item["cached_bars"] == 80
        assert item["coverage_ratio"] == 80 / 8760
        now = datetime.now(timezone.utc)
        ingest.mark_history(sub, ingest.history_plan(sub, now), now, state="BACKFILLING", delay=5)
        item = next(row for row in read.watchlist_progress(TENANT_A, USER_A)["items"] if row["timeframe"] == "1h")
        expected_start = listed // 3600 * 3600
        assert item["listing_time"] == listed and item["range_policy"] == "LISTING_CLIPPED"
        assert item["range_refresh_pending"] is False
        assert item["range_start"] == item["target_from"].timestamp() == expected_start
        assert item["requested_start"] < expected_start
        assert item["target_bars"] == (item["range_end"] - expected_start) // 3600 + 1
        assert item["cached_bars"] == 80 and item["missing_bars"] == item["target_bars"] - 80
        assert item["coverage_ratio"] == 80 / item["target_bars"]
        reschedule(migrated_database, repo)
        reset = read.get(INSTRUMENT, "1h")
        assert reset["cursor"] == 0 and reset["requested_at"] > before["requested_at"]
        finish(repo)
        metadata = read.get(INSTRUMENT, "1h")["report"]["metadata"]
        assert metadata["context_id"] != old_context
        assert metadata["requested_from"] == expected_start
        assert metadata["range_policy"] == "LISTING_CLIPPED" and metadata["listing_time"] == listed
        assert metadata["target_bars"] == item["target_bars"]
        assert frozen_facts(migrated_database, old_context) == old_facts


def test_listing_clipped_increment_refreshes_target_metadata_without_reset(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, rows):
        listed = rows[0][0] - 20 * 3600 + 123
        add_listing(migrated_database, listed)
        repo.schedule()
        finish(repo)
        before = read.get(INSTRUMENT, "1h")
        old_metadata = before["report"]["metadata"]
        assert old_metadata["range_policy"] == "LISTING_CLIPPED"
        later = datetime.fromisoformat(old_metadata["snapshot_at"]) + timedelta(hours=2)

        class LaterDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return later.astimezone(tz) if tz else later.replace(tzinfo=None)

        monkeypatch.setattr(studies, "datetime", LaterDateTime)
        ingest.upsert_final_bars(sub, [(rows[-1][0]+3600, 100., 105., 95., 101., 10.)], observed_at=later)
        reschedule(migrated_database, repo)
        queued = read.get(INSTRUMENT, "1h")
        assert queued["requested_at"] == before["requested_at"]
        finish(repo)
        completed = read.get(INSTRUMENT, "1h")
        metadata = completed["report"]["metadata"]
        assert completed["cursor"] == 81 and metadata["context_id"] == old_metadata["context_id"]
        assert metadata["mode"] == "INCREMENTAL"
        assert metadata["target_bars"] == old_metadata["target_bars"] + 2
        assert metadata["cached_bars"] == 81 and metadata["missing_bars"] == metadata["target_bars"] - 81
        assert metadata["coverage_ratio"] == 81 / metadata["target_bars"]
        assert metadata["requested_start"] == old_metadata["requested_start"] + 7200
        assert metadata["range_start"] == old_metadata["range_start"]


def test_unaligned_cache_rows_are_not_counted_replayed_or_revision_triggers(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (repo, read, ingest, sub, rows):
        unaligned = (rows[0][0]+1, 100., 105., 95., 101., 10.)
        assert ingest.upsert_final_bars(sub, [unaligned], observed_at=datetime.now(timezone.utc)) == 1
        repo.schedule()
        finish(repo)
        before = read.get(INSTRUMENT, "1h")
        assert before["cursor"] == before["total"] == before["report"]["bar_count"] == 80
        assert before["report"]["metadata"]["cached_bars"] == 80
        new_unaligned = (rows[0][0]+2, 100., 105., 95., 101., 10.)
        assert ingest.upsert_final_bars(sub, [new_unaligned], observed_at=datetime.now(timezone.utc)) == 1
        reschedule(migrated_database, repo)
        after = read.get(INSTRUMENT, "1h")
        assert after["state"] == "READY" and after["requested_at"] == before["requested_at"]


def test_progress_clock_advance_uses_persisted_snapshot_without_count_query(migrated_database, monkeypatch):
    with partial_cache(migrated_database, monkeypatch) as (_, read, _, _, _):
        add_watchlist(migrated_database)
        before = next(row for row in read.watchlist_progress(TENANT_A, USER_A)["items"] if row["timeframe"] == "1h")
        later = datetime.now(timezone.utc) + timedelta(days=1)

        class LaterDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return later.astimezone(tz) if tz else later.replace(tzinfo=None)

        transaction = read.pool.transaction

        class GuardConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, query, params=None):
                assert "count(distinct open_time)" not in query.casefold()
                return self.connection.execute(query, params)

        @contextmanager
        def guarded_transaction(tenant, user):
            with transaction(tenant, user) as connection:
                yield GuardConnection(connection)

        monkeypatch.setattr(studies, "datetime", LaterDateTime)
        monkeypatch.setattr(read.pool, "transaction", guarded_transaction)
        for _ in range(3):
            after = next(row for row in read.watchlist_progress(TENANT_A, USER_A)["items"] if row["timeframe"] == "1h")
            for key in ("target_bars", "cached_bars", "missing_bars", "target_from", "target_to",
                        "coverage_asof", "coverage_ratio", "requested_start"):
                assert after[key] == before[key]
            assert after["range_refresh_pending"] is False
