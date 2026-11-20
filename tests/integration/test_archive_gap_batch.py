"""Bounded archive-hole persistence against the real least-privilege role."""
from datetime import timedelta

from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from tests.integration.test_listing_history_bounds import INSTRUMENT, NOW, revision, seed_instrument


def test_sparse_archive_page_defers_all_proven_holes_without_hiding_real_bars(migrated_database):
    seed_instrument(migrated_database)
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"), max_size=1)
    pool.open()
    try:
        repo = PostgresMarketIngestRepository(pool)
        sub = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "龙虾_USDT", "USDT", "1m")
        end = int(NOW.timestamp()) // 60 * 60 - 60
        start = end - 899 * 60
        with pool.public_transaction() as conn:
            revision(conn, start + 1)
        repo.sync_states([sub])
        present = [(start + i * 60, 100, 102, 99, 101, 10) for i in range(0, 900, 2)]
        repo.upsert_final_bars(sub, present, observed_at=NOW)
        holes = [(start + i * 60, start + i * 60) for i in range(1, 900, 2)]
        repo.defer_history_ranges(sub, holes, NOW, reason="ARCHIVE_RANGE_EMPTY")
        # Retrying the same proof does not duplicate a row, insert a fake bar,
        # or extend the interval over the alternating genuinely present bars.
        repo.defer_history_ranges(sub, holes, NOW, reason="ARCHIVE_RANGE_EMPTY")
        with pool.public_transaction() as conn:
            stored = conn.execute("""SELECT from_time,to_time,retry_at FROM market_history_deferred
                WHERE instrument_id=%s AND timeframe='1m' ORDER BY from_time""", (INSTRUMENT,)).fetchall()
            assert [(row[0], row[1]) for row in stored] == holes
            assert all(row[2] == NOW + timedelta(hours=6) for row in stored)
            assert conn.execute("SELECT count(*) FROM bars WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 450
        plan = repo.history_plan(sub, NOW)
        assert plan["target_bars"] == 900 and plan["cached_bars"] == 450
        assert plan["missing_bars"] == 450 and plan["gap"] is None
    finally:
        pool.close()
