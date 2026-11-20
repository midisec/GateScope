"""Real SQL annual indicator boundary, immutable replay and owner isolation.

5101 candles deliberately cross the old cap without a costly full-year loadtest.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from apps.api.research import build_research_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.quant.artifacts import report_digest
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, TENANT_B, USER_A, USER_B, seed_two_tenants
from tests.unit.test_research_backtest import make_bars


def test_long_indicator_frozen_restart_pagination_and_owner_isolation(migrated_database, monkeypatch):
    seed_two_tenants(migrated_database)
    pools = [PostgresPool(make_conninfo(migrated_database, options=f"-c role={role}"))
        for role in ("gatescope_api", "gatescope_strategy", "gatescope_market", "gatescope_mcp")]
    for pool in pools:
        pool.open()
    api_pool, worker_pool, market_pool, mcp_pool = pools
    try:
        source = make_bars(5101)
        PostgresMarketIngestRepository(market_pool).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            [(int(b.open_time.timestamp()), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)) for b in source],
            observed_at=datetime.now(timezone.utc))
        app = FastAPI()
        app.include_router(build_research_router(api_pool))
        identity = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A, role=SimpleNamespace(value="OWNER"))

        @app.middleware("http")
        async def identify(request: Request, call_next):
            request.state.auth_session = identity
            return await call_next(request)

        with TestClient(app) as client:
            coverage = client.get(f"/api/v2/research/coverage?instrument_id={INSTRUMENT}&timeframe=1h&limit=8760").json()
            assert coverage["available_bars"] == 5101
            assert coverage["missing_bars"] == 3659
            assert coverage["internal_missing_bars"] == 0
            submitted = client.post("/api/v2/research/backtests", json={
                "instrument_id": str(INSTRUMENT), "timeframe": "1h", "bar_limit": 8760,
                "spec": {"strategy": "momentum", "direction": "BOTH", "availability_mode": "HISTORICAL_CLOSE"}})
            assert submitted.status_code == 202, submitted.text
            run = UUID(submitted.json()["id"])
            worker = BacktestRepository(worker_pool)
            import gatescope.adapters.postgres_indicator_research as annual
            original = annual.run_backtest

            class ProcessInterrupted(BaseException):
                pass

            def interrupt(*args, **kwargs):
                raise ProcessInterrupted()

            monkeypatch.setattr(annual, "run_backtest", interrupt)
            with pytest.raises(ProcessInterrupted):
                worker.run_one()
            frozen = BacktestRepository(api_pool).get(TENANT_A, USER_A, run)
            assert frozen["state"] == "RUNNING"
            assert frozen["progress"]["input_complete"] is True
            with worker_pool.transaction(TENANT_A, USER_A) as conn:
                count = conn.execute("SELECT sum(row_count) FROM research_indicator_chunks WHERE run_id=%s AND kind='inputs'", (run,)).fetchone()[0]
                assert count == 5101
                conn.execute("UPDATE research_backtests SET lease_until=now()-interval '1 second' WHERE id=%s", (run,))
            monkeypatch.setattr(annual, "run_backtest", original)
            # A resumed completed freeze must not query mutable source bars again.
            def forbidden_refetch(*args, **kwargs):
                raise AssertionError("already-frozen input was fetched again")
            monkeypatch.setattr(BacktestRepository, "iter_bar_batches", forbidden_refetch)
            assert BacktestRepository(worker_pool).run_one()
            result = client.get(f"/api/v2/research/backtests/{run}").json()
            assert result["state"] == "SUCCEEDED", result["error"]
            report = result["report"]
            assert report["bar_count"] == 5101
            assert report["cache_coverage"]["missing_bars"] == 3659
            assert "INCOMPLETE_REQUESTED_HISTORY" in report["quality_flags"]
            assert report["full"]["metrics"]["trade_count"] > 0
            assert report["input_bars"] == report["full"]["trades"] == []
            assert report_digest(report) == report["artifact_digest"]
            base = f"/api/v2/research/backtests/{run}/indicator"
            tail = client.get(f"{base}/inputs?offset=5100&limit=1").json()
            assert tail["total"] == 5101 and not tail["has_more"]
            assert tail["items"][0]["open_time"] == source[-1].open_time.isoformat()
            assert "source_hash" in tail["items"][0]
            assert client.get(f"{base}/inputs?offset=5101&limit=1").json()["items"] == []
            assert len(client.get(f"{base}/inputs?offset=510&limit=5").json()["items"]) == 5
            trades = client.get(f"{base}/trades?offset=0&limit=2").json()
            assert len(trades["items"]) == 2
            chart = client.get(f"{base}/chart?trade_index=0&limit=100").json()
            selected = chart["selected_trade"]
            assert selected == trades["items"][0]
            times = {bar["time"] for bar in chart["candles"]}
            assert selected["entry_time"] in times and selected["exit_bar_open_time"] in times
            default_chart = client.get(f"{base}/chart").json()
            assert len(default_chart["candles"]) == 512
            assert default_chart["offset"] == 5101-512
            assert all(point["peak_equity"] >= point["equity"] for point in default_chart["equity_curve"])
            assert client.get(f"{base}/chart?trade_index=550000").status_code == 404
            assert client.get(f"{base}/inputs?limit=513").status_code == 422
            from gatescope.adapters.postgres_mcp_research import McpResearchRepository
            mcp=McpResearchRepository(mcp_pool)
            exported=mcp.backtest_detail(TENANT_A,USER_A,run,section="trades",limit=2)
            assert exported["total"]==report["full"]["metrics"]["trade_count"]
            assert exported["items"]==trades["items"]
            assert exported["evidence_basis"]=="FROZEN_INDICATOR_CHUNKS_SHA256"
            assert mcp.backtest_detail(TENANT_A,USER_A,run,section="inputs",limit=1,offset=5100)["items"]==tail["items"]
            with pytest.raises(LookupError):
                mcp.backtest_detail(TENANT_A,USER_B,run,section="inputs")
            with worker_pool.transaction(TENANT_A, USER_A) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("UPDATE research_indicator_chunks SET digest=repeat('0',64) WHERE run_id=%s", (run,))
            for tenant, user in ((TENANT_A, USER_B), (TENANT_B, USER_B)):
                identity.tenant_id, identity.user_id = tenant, user
                assert client.get(f"{base}/inputs").status_code == 404
                assert client.get(f"{base}/chart").status_code == 404
    finally:
        for pool in pools:
            pool.close()
