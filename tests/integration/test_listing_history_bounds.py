from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.market_history_bounds import history_bounds
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.domain.history import year_bounds


INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")
NOW = datetime(2026, 9, 12, 12, 0, 30, tzinfo=timezone.utc)
LISTED = 1_773_138_212


def seed_instrument(database_url):
    with psycopg.connect(database_url) as conn:
        conn.execute("""INSERT INTO instruments
            (id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
            VALUES(%s,'GATE','FUTURES_USDT','龙虾_USDT','龙虾','USDT','USDT','ACTIVE',%s,%s)""",
            (INSTRUMENT, NOW - timedelta(days=200), NOW))


def revision(conn, create_time, *, revision_number=1, available=NOW, effective=None):
    conn.execute("""INSERT INTO instrument_revisions
        (id,instrument_id,revision,effective_at,available_at,metadata_json,payload_hash,created_at)
        VALUES(gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s)""",
        (INSTRUMENT, revision_number, effective or available, available,
         Jsonb({"name": "龙虾_USDT", "create_time": create_time}), str(uuid4()), available))


def test_listing_clipped_ingest_progress_gaps_and_targets_survive_sync(migrated_database):
    seed_instrument(migrated_database)
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"), max_size=1)
    pool.open()
    try:
        repo = PostgresMarketIngestRepository(pool)
        subscription = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "龙虾_USDT", "USDT", "1h")
        repo.sync_states([subscription])
        assert repo.history_plan(subscription, NOW)["target_bars"] == 8760
        with pool.public_transaction() as conn:
            revision(conn, LISTED)
        start = LISTED // 3600 * 3600
        end = int(NOW.timestamp()) // 3600 * 3600 - 3600
        repo.upsert_final_bars(subscription, [(t, 100, 102, 99, 101, 10) for t in (start, end - 7200, end)], observed_at=NOW)
        initial = repo.history_plan(subscription, NOW)
        assert initial["target_from"].timestamp() == start
        assert initial["target_to"].timestamp() == end + 3600
        assert initial["target_bars"] == (end - start) // 3600 + 1
        assert initial["cached_bars"] == 3
        assert initial["missing_bars"] == initial["target_bars"] - 3
        assert initial["gap"] == (end - 3600, end - 3600)
        assert initial["listing_time"] == LISTED and initial["range_policy"] == "LISTING_CLIPPED"
        repo.defer_history(subscription, year_bounds(NOW, "1h")[0], start - 3600, NOW, reason="ARCHIVE_NOT_PUBLISHED")
        assert repo.history_plan(subscription, NOW) == initial
        repo.mark_history(subscription, initial, NOW, state="BACKFILLING", delay=5)
        repo.sync_states([subscription])
        repo.sync_states([])
        repo.sync_states([subscription])
        with pool.public_transaction() as conn:
            stored = conn.execute("""SELECT target_bars,cached_bars,missing_bars,target_from,target_to,active
                FROM market_sync_state WHERE instrument_id=%s AND timeframe='1h'""", (INSTRUMENT,)).fetchone()
        assert stored == (initial["target_bars"], 3, initial["missing_bars"], initial["target_from"], initial["target_to"], True)
        # Cached evidence earlier than metadata is retained; pre-listing deferred
        # intervals still cannot suppress a genuine post-listing hole.
        repo.upsert_final_bars(subscription, [(start - 7200, 100, 102, 99, 101, 10)], observed_at=NOW)
        wider = repo.history_plan(subscription, NOW)
        assert wider["target_from"].timestamp() == start - 7200
        assert wider["target_bars"] == initial["target_bars"] + 2
        assert wider["cached_bars"] == 4 and wider["gap"] == initial["gap"]
        for role in ("gatescope_api", "gatescope_strategy"):
            with psycopg.connect(make_conninfo(migrated_database, options=f"-c role={role}")) as conn:
                shared = history_bounds(conn, INSTRUMENT, "1h", NOW)
                assert shared["start"] == int(wider["target_from"].timestamp())
                assert shared["target_bars"] == wider["target_bars"]
    finally:
        pool.close()


def test_listing_metadata_is_asof_latest_and_ignores_unavailable_cache(migrated_database):
    seed_instrument(migrated_database)
    pool = PostgresPool(migrated_database, max_size=1)
    pool.open()
    try:
        repo = PostgresMarketIngestRepository(pool)
        subscription = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "龙虾_USDT", "USDT", "1h")
        with pool.public_transaction() as conn:
            revision(conn, LISTED)
            # A later revision that was not available at the evaluated time
            # cannot replace the known creation timestamp.
            revision(conn, LISTED + 86400, revision_number=2, available=NOW + timedelta(days=1))
            assert history_bounds(conn, INSTRUMENT, "1h", NOW)["listing_time"] == LISTED
        start = LISTED // 3600 * 3600
        repo.upsert_final_bars(subscription, [(start - 7200, 100, 102, 99, 101, 10)], observed_at=NOW + timedelta(seconds=1))
        repo.upsert_final_bars(subscription, [(start - 10800 + 1, 100, 102, 99, 101, 10)], observed_at=NOW)
        repo.upsert_final_bars(subscription, [(start - 14400, 100, 102, 99, 101, 10)], observed_at=NOW)
        with pool.public_transaction() as conn:
            conn.execute("UPDATE bars SET created_at=%s WHERE instrument_id=%s AND open_time=to_timestamp(%s)",
                (NOW + timedelta(seconds=1), INSTRUMENT, start - 14400))
            assert history_bounds(conn, INSTRUMENT, "1h", NOW)["start"] == start
            revision(conn, 0, revision_number=3)
            assert history_bounds(conn, INSTRUMENT, "1h", NOW)["target_bars"] == 8760
            revision(conn, LISTED, revision_number=4, effective=NOW + timedelta(days=1))
            assert history_bounds(conn, INSTRUMENT, "1h", NOW)["target_bars"] == 8760
            revision(conn, int(NOW.timestamp()) + 1, revision_number=5)
            assert history_bounds(conn, INSTRUMENT, "1h", NOW)["target_bars"] == 8760
    finally:
        pool.close()
