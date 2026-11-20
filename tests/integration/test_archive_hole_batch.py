"""Real market-role persistence for bounded archive hole batches."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from tests.integration.test_listing_history_bounds import INSTRUMENT, seed_instrument


def test_archive_holes_batch_idempotence_scope_and_rest_rows_remain_intact(migrated_database):
    seed_instrument(migrated_database)
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"), max_size=1)
    pool.open()
    try:
        repo = PostgresMarketIngestRepository(pool)
        minute = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "龙虾_USDT", "USDT", "1m")
        hourly = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "龙虾_USDT", "USDT", "1h")
        now = datetime(2026, 9, 12, 12, 0, 30, tzinfo=timezone.utc)
        start = int(now.timestamp()) // 60 * 60 - 2 * 86400
        original = (start + 898 * 60, 200, 202, 199, 201, 6)
        assert repo.upsert_final_bars(minute, [original], observed_at=now) == 1
        archive_rows = [(start + index * 60, 100, 102, 99, 101, 10) for index in range(0, 900, 2)]
        assert repo.upsert_final_bars(minute, archive_rows, observed_at=now,
                                     archive_source={"source": "GATE_MONTHLY_ARCHIVE"}) == 449
        # Cleanup is scoped to this subscription; a separate hourly deferral
        # retains its exact range, reason and original retry timestamp.
        repo.defer_history(minute, start - 60, start - 60, now - timedelta(hours=7),
                           reason="ARCHIVE_RANGE_EMPTY")
        repo.defer_history(hourly, start, start + 3600, now, reason="ARCHIVE_NOT_PUBLISHED")
        holes = [(start + index * 60, start + index * 60) for index in range(1, 900, 2)]
        repo.defer_history_ranges(minute, holes, now, reason="ARCHIVE_RANGE_EMPTY")
        later = now + timedelta(seconds=60)
        repo.defer_history_ranges(minute, holes, later, reason="ARCHIVE_RANGE_EMPTY")
        with pool.public_transaction() as connection:
            persisted = connection.execute("""SELECT from_time,to_time,retry_at,reason
                FROM market_history_deferred WHERE instrument_id=%s AND timeframe='1m'
                ORDER BY from_time""", (INSTRUMENT,)).fetchall()
            assert len(persisted) == 450
            assert [(row[0], row[1]) for row in persisted] == holes
            assert all(row[2:] == (later + timedelta(hours=6), "ARCHIVE_RANGE_EMPTY") for row in persisted)
            assert connection.execute("""SELECT from_time,to_time,retry_at,reason
                FROM market_history_deferred WHERE instrument_id=%s AND timeframe='1h'""",
                (INSTRUMENT,)).fetchone() == (start, start + 3600, now + timedelta(hours=6), "ARCHIVE_NOT_PUBLISHED")
            assert connection.execute("""SELECT count(*) FROM bars
                WHERE instrument_id=%s AND timeframe='1m'""", (INSTRUMENT,)).fetchone()[0] == 450
            assert connection.execute("""SELECT close,revision FROM bars
                WHERE instrument_id=%s AND timeframe='1m' AND open_time=to_timestamp(%s)""",
                (INSTRUMENT, original[0])).fetchone() == (Decimal("201"), 1)
    finally:
        pool.close()
