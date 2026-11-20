from datetime import datetime, timezone
import json

import psycopg
from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository, MarketSubscription
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.quant.structure_lifecycle import observe
from tests.integration.test_postgres_rls import (
    seed_two_tenants,
    INSTRUMENT,
    TENANT_A,
    USER_A,
    TENANT_B,
    USER_B,
)


def test_replay_beyond_8000_is_bounded_auto_incremental_and_revision_starts_new_context(
    migrated_database, monkeypatch
):
    seed_two_tenants(migrated_database)
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"), max_size=1)
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"), max_size=1)
    market = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"), max_size=1)
    for pool in (api, worker, market):
        pool.open()
    try:
        now = datetime.now(timezone.utc)
        last = int(now.timestamp()) // 60 * 60 - 600
        s = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1m")
        ingest = PostgresMarketIngestRepository(market)
        ingest.sync_states([s])
        rows = [(last - (8104 - i) * 60, 100, 102, 99, 101, 10) for i in range(8105)]
        for start in range(0, len(rows), 900):
            ingest.upsert_final_bars(s, rows[start : start + 900], observed_at=now)
        plan = ingest.history_plan(s, now)
        assert plan["target_bars"] == 525600 and plan["cached_bars"] == 8105
        ingest.defer_history(s, *plan["gap"], now, reason="ARCHIVE_NOT_PUBLISHED")
        older = ingest.history_plan(s, now)
        assert older["gap"] != plan["gap"] and older["missing_bars"] == plan["missing_bars"]
        ingest.mark_history(s, plan, now, state="SOURCE_LIMIT", delay=900)
        repo = PatternStudyRepository(worker)
        repo.schedule()
        read = PatternStudyRepository(api)
        assert read.get(INSTRUMENT, "1m")["state"] == "QUEUED"
        # Real detector tested separately; stress the annual streaming/DB boundary cheaply.
        monkeypatch.setattr(
            studies,
            "observe",
            lambda bars, index, state, **kw: observe(bars, index, state, detector=lambda p: [], **kw),
        )
        assert repo.run_slice(max_steps=127)
        requested = read.get(INSTRUMENT, "1m")["requested_at"]
        restarted = PatternStudyRepository(worker)
        for _ in range(150):
            if not restarted.run_slice(seconds=5, max_steps=127):
                break
        result = read.get(INSTRUMENT, "1m")
        assert result["state"] == "READY" and result["cursor"] == 8105
        assert (
            result["report"]["bar_count"] == 8105 and result["report"]["metadata"]["missing_bars"] == 517495
        )
        with worker.public_transaction() as c:
            saved, checkpoint = c.execute(
                "SELECT input_json,checkpoint_json FROM market_pattern_studies"
            ).fetchone()
        assert len(saved["bars"]) == 480 and len(json.dumps([saved, checkpoint])) < 200000
        appended = [(last + i * 60, 100, 102, 99, 101, 10) for i in range(1, 6)]
        ingest.upsert_final_bars(s, appended, observed_at=datetime.now(timezone.utc))
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_pattern_studies SET updated_at=now()-interval '16 minutes'")
        restarted._scheduled_at = 0
        restarted.schedule()
        assert read.get(INSTRUMENT, "1m")["requested_at"] == requested
        assert restarted.run_slice(seconds=5, max_steps=127)
        result = read.get(INSTRUMENT, "1m")
        assert result["state"] == "READY" and result["cursor"] == 8110
        assert result["report"]["metadata"]["mode"] == "INCREMENTAL"
        with worker.public_transaction() as c:
            assert c.execute("SELECT count(*) FROM market_pattern_report_versions").fetchone()[0] == 2
        # Archive overlap never overrides a REST revision.
        altered = (rows[0][0], 100, 103, 99, 102, 10)
        assert (
            ingest.upsert_final_bars(
                s,
                [altered],
                observed_at=datetime.now(timezone.utc),
                archive_source={"source": "GATE_MONTHLY_ARCHIVE"},
            )
            == 0
        )
        assert ingest.upsert_final_bars(s, [altered], observed_at=datetime.now(timezone.utc)) == 1
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_pattern_studies SET updated_at=now()-interval '16 minutes'")
        restarted._scheduled_at = 0
        restarted.schedule()
        reset = read.get(INSTRUMENT, "1m")
        assert reset["requested_at"] > requested and reset["cursor"] == 0
        # User progress does not expose other users' watchlists, even within public OHLC storage.
        with psycopg.connect(migrated_database) as c:
            watch = c.execute(
                """INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at)
                VALUES(gen_random_uuid(),%s,%s,'annual',now(),now()) RETURNING id""",
                (TENANT_A, USER_A),
            ).fetchone()[0]
            c.execute(
                """INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at)
                VALUES(gen_random_uuid(),%s,%s,%s,0,'1m',now(),now())""",
                (TENANT_A, watch, INSTRUMENT),
            )
        progress = read.watchlist_progress(TENANT_A, USER_A, 0, 3)
        assert progress["total"] == 8 and len(progress["items"]) == 3
        assert read.watchlist_progress(TENANT_B, USER_B)["items"] == []
    finally:
        for pool in (api, worker, market):
            pool.close()
