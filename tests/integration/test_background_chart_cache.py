from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
import psycopg
from psycopg.conninfo import make_conninfo
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from apps.api.v2 import build_v2_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_chart_cache import PostgresChartCache
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository, MarketSubscription
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.annual_structure_study import reference
from tests.integration.test_postgres_rls import seed_two_tenants, INSTRUMENT


def test_durable_warm_cache_survives_reader_restart_and_repairs_revisions(migrated_database, monkeypatch):
    seed_two_tenants(migrated_database)
    pools = [
        PostgresPool(make_conninfo(migrated_database, options=f"-c role={role}"))
        for role in ("gatescope_market", "gatescope_api")
    ]
    for p in pools:
        p.open()
    market, api = pools
    try:
        s = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1m")
        ingest = PostgresMarketIngestRepository(market)
        warm = PostgresChartCache(market)
        now = datetime.now(timezone.utc)
        # Seed only candles already closed at the first observation. At :00 or
        # :01, using wall-clock now here incorrectly includes an unfinished bar
        # at observed_at=now-2s, which ingestion correctly excludes.
        last = int((now - timedelta(seconds=2)).timestamp()) // 60 * 60 - 60
        rows = [(last - (1499 - i) * 60, 100, 103, 99, 101, 10) for i in range(1500)]
        ingest.upsert_final_bars(s, rows, observed_at=now - timedelta(seconds=2))
        assert warm.warm(s) and not warm.warm(s)
        reader = PostgresMarketReadDatabase(api)
        cached = reader.latest_chart_snapshot(str(INSTRUMENT), "1m", 8)
        assert len(cached["items"]) == 8 and cached["bar_count"] == 1500
        assert (
            PostgresMarketReadDatabase(api).latest_chart_snapshot(str(INSTRUMENT), "1m", 1500)["items"][-8:]
            == cached["items"]
        )
        app = FastAPI()
        app.include_router(build_v2_router(reader, SimpleNamespace()))

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.trace_id = "fixture"
            return await call_next(request)

        with TestClient(app) as client:
            result = client.get(f"/api/v2/bars?instrument_id={INSTRUMENT}&timeframe=1m&latest=true&limit=8")
            assert result.status_code == 200 and result.json()["read_source"] == "BACKGROUND_SNAPSHOT"
            assert result.json()["items"] == cached["items"]
        ingest.upsert_final_bars(s, [(last, 100, 103, 99, 102, 10)], observed_at=now - timedelta(seconds=1))
        assert reader.latest_chart_snapshot(str(INSTRUMENT), "1m", 8) is None
        assert warm.warm(s)
        assert Decimal(reader.latest_chart_snapshot(str(INSTRUMENT), "1m", 8)["items"][-1]["close"]) == 102
        # Old history outside the complete chart window does not invalidate it.
        ingest.upsert_final_bars(s, [(last - 3000 * 60, 100, 103, 99, 101, 10)], observed_at=now)
        assert not warm.warm(s)
        # A correction committed DURING a build must reject that stale build.
        ingest.upsert_final_bars(s, [(last, 100, 103, 99, 102.1, 10)], observed_at=datetime.now(timezone.utc))
        original = warm.reader.get_canonical_candles

        def racing_read(*args):
            stale = original(*args)
            ingest.upsert_final_bars(
                s, [(last, 100, 103, 99, 102.5, 10)], observed_at=datetime.now(timezone.utc)
            )
            return stale

        with monkeypatch.context() as patch:
            patch.setattr(warm.reader, "get_canonical_candles", racing_read)
            assert not warm.warm(s)
        assert warm.warm(s)
        assert Decimal(
            reader.latest_chart_snapshot(str(INSTRUMENT), "1m", 8)["items"][-1]["close"]
        ) == Decimal("102.5")
        # A future available_at must neither leak into the chart nor mark an old
        # payload current forever. It remains dirty for a subsequent warm cycle.
        ingest.upsert_final_bars(s, [(last, 100, 103, 99, 103, 10)], observed_at=now + timedelta(hours=1))
        assert not warm.warm(s)
        assert reader.latest_chart_snapshot(str(INSTRUMENT), "1m", 8) is None
        assert Decimal(
            reader.get_canonical_candles(str(INSTRUMENT), "1m", last, last, 1)[0]["close"]
        ) == Decimal("102.5")
        with api.public_transaction() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE market_chart_snapshots SET generation=0")
    finally:
        for p in pools:
            p.close()


def test_actual_failures_are_reused_deduplicated_and_known_before_prediction(migrated_database):
    # Direct projection fixtures exercise SQL source precedence and chronology.
    with psycopg.connect(migrated_database) as c:

        def add(context, id, stage, end, phase, sampled=True):
            c.execute(
                """INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,score_bucket,
                stage_time,phase,resolved_time,sampled,formed) VALUES(%s,%s,'double_top','FORMING','UNSCORED',%s,%s,%s,%s,false)""",
                (context, id, stage, phase, end, sampled),
            )

        add("replay", "same", 100, 110, "CONFIRMED")
        add("recorded", "same", 100, 120, "INVALIDATED")
        add("recorded", "expired", 130, 140, "EXPIRED")
        add("replay", "win", 150, 160, "CONFIRMED")
        add("recorded", "future", 170, 220, "INVALIDATED")
        add("replay", "overlap", 105, 115, "CONFIRMED")
        c.execute(
            "UPDATE market_structure_stages SET formed=true WHERE candidate_id IN ('same','win','future')"
        )
        e = {"pattern": "double_top", "phase": "FORMING", "confidence": None, "id": "new"}
        r = reference(c, "replay", e, 200, recorded_context="recorded")
        assert r["sample_count"] == 3 and r["successes"] == 1
        assert r["formation"]["sample_count"] == 3 and r["formation"]["successes"] == 2
        assert r["formation"]["probability"] is None
        assert r["recorded_count"] == 2 and r["replay_count"] == 1
        assert r["invalidated"] == 1 and r["expired"] == 1
        assert {f["id"] for f in r["failure_examples"]} == {"same", "expired"}
        assert r["overlap_excluded"] == 1
        # No historical outcome from the same close as a new forecast.
        early = reference(c, None, e, 120, recorded_context="recorded")
        assert early["sample_count"] == 0
