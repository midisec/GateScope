"""Small bounded runs exercise real SQL/RLS, checkpoints and frozen evidence."""
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
from gatescope.adapters.postgres_structure_research import StructureResearchRepository
from gatescope.quant.artifacts import report_digest
from gatescope.quant.backtest import StrategySpec
from tests.integration.test_postgres_rls import TENANT_A,TENANT_B,USER_A,USER_B,INSTRUMENT,seed_two_tenants
from tests.unit.test_structure_research import make_structure_bars


def test_same_engine_queue_resume_evidence_pages_and_owner_isolation(migrated_database):
    seed_two_tenants(migrated_database)
    pools = [PostgresPool(make_conninfo(migrated_database,options=f"-c role={role}"))
        for role in ("gatescope_api","gatescope_strategy","gatescope_market")]
    for pool in pools:
        pool.open()
    api,worker,market = pools
    try:
        bars = make_structure_bars(320)
        PostgresMarketIngestRepository(market).upsert_final_bars(
            MarketSubscription(INSTRUMENT,"GATE","FUTURES_USDT","BTC_USDT","USDT","1h"),
            [(int(b.open_time.timestamp()),float(b.open),float(b.high),float(b.low),float(b.close),float(b.volume)) for b in bars],
            observed_at=datetime.now(timezone.utc))
        app = FastAPI()
        app.include_router(build_research_router(api))
        identity = SimpleNamespace(tenant_id=TENANT_A,user_id=USER_A,role=SimpleNamespace(value="OWNER"))
        @app.middleware("http")
        async def auth(request: Request, call_next):
            request.state.auth_session=identity
            return await call_next(request)
        with TestClient(app) as client:
            catalog = client.get("/api/v2/research/strategies").json()
            assert len(catalog["structure_catalog"])==28
            response = client.post("/api/v2/research/backtests",json={"instrument_id":str(INSTRUMENT),"bar_limit":320,
                "spec":{"engine_mode":"STRUCTURE","structure_patterns":["double_bottom"],
                    "availability_mode":"HISTORICAL_CLOSE","max_holding_bars":12}})
            assert response.status_code==202,response.text
            identifier=UUID(response.json()["id"])
            assert not BacktestRepository(worker).run_one()  # Legacy claimer cannot steal a chunked run.
            engine=StructureResearchRepository(worker)
            assert engine.run_slice(max_bars=17,budget_seconds=10)
            first=client.get(f"/api/v2/research/backtests/{identifier}").json()
            assert first["state"]=="RUNNING" and first["progress"]["processed_bars"]==17
            assert client.get("/api/v2/research/backtests?engine_mode=INDICATOR").json()["total"]==0
            assert client.get("/api/v2/research/backtests?engine_mode=STRUCTURE").json()["total"]==1
            # New repository object stands for a process restart; no re-detection reset.
            restarted=StructureResearchRepository(worker)
            for _ in range(25):
                assert restarted.run_slice(max_bars=23,budget_seconds=10)
                current=client.get(f"/api/v2/research/backtests/{identifier}").json()
                if current["state"] in ("SUCCEEDED","FAILED"):
                    break
            assert current["state"]=="SUCCEEDED",current["error"]
            report=current["report"]
            assert report["bar_count"]==320 and report_digest(report)==report["artifact_digest"]
            assert report["structure_research"]["case_count"]>0
            assert "input_bars" not in report and "candles" not in report
            assert report["structure_research"]["portfolio_metrics_available"] is False
            assert report["screening"]["thresholds_enforced"] is False
            assert report["structure_research"]["summary"]["interval"] is None
            pages=client.get(f"/api/v2/research/backtests/{identifier}/cases?limit=1").json()
            assert len(pages["items"])==1 and pages["total"]>0
            if pages["total"]>1:
                assert pages["has_more"] and pages["next_offset"]==1
            assert client.get(f"/api/v2/research/backtests/{identifier}/cases?limit=101").status_code==422
            assert client.get(f"/api/v2/research/backtests/{identifier}/cases?state=invalid").status_code==422
            inputs=client.get(f"/api/v2/research/backtests/{identifier}/inputs?limit=10&offset=310").json()
            assert len(inputs["items"])==10 and inputs["total"]==320 and not inputs["has_more"]
            events=client.get(f"/api/v2/research/backtests/{identifier}/events?limit=1").json()
            assert events["total"]>0 and len(events["items"])==1
            with worker.transaction(TENANT_A,USER_A) as conn:
                checkpoint=conn.execute("SELECT checkpoint_json FROM research_structure_checkpoints WHERE run_id=%s",(identifier,)).fetchone()[0]
                assert len(checkpoint["bars"])<=480 and checkpoint["processed"]==320
                with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                    conn.execute("UPDATE research_structure_cases SET state='TARGET' WHERE run_id=%s",(identifier,))
            identity.user_id=USER_B
            assert client.get(f"/api/v2/research/backtests/{identifier}/cases").status_code==404
            assert client.get(f"/api/v2/research/backtests/{identifier}/inputs").status_code==404
            assert client.get(f"/api/v2/research/backtests/{identifier}/events").status_code==404
            identity.tenant_id=TENANT_B
            assert client.get("/api/v2/research/backtests?engine_mode=STRUCTURE").json()["total"]==0
    finally:
        for pool in pools:
            pool.close()


def test_slice_exception_preserves_committed_cursor_and_rolls_back_new_inputs(migrated_database,monkeypatch):
    seed_two_tenants(migrated_database)
    pool=PostgresPool(migrated_database)
    pool.open()
    try:
        bars=make_structure_bars(310)
        PostgresMarketIngestRepository(pool).upsert_final_bars(
            MarketSubscription(INSTRUMENT,"GATE","FUTURES_USDT","BTC_USDT","USDT","1h"),
            [(int(b.open_time.timestamp()),float(b.open),float(b.high),float(b.low),float(b.close),float(b.volume)) for b in bars],
            observed_at=datetime.now(timezone.utc))
        request=BacktestRepository(pool).create(TENANT_A,USER_A,INSTRUMENT,"1h",
            StrategySpec(engine_mode="STRUCTURE",availability_mode="HISTORICAL_CLOSE"),310,None,None)
        engine=StructureResearchRepository(pool)
        assert engine.run_slice(max_bars=11,budget_seconds=10)
        import gatescope.adapters.postgres_structure_research as module
        actual=module.observe
        calls=0
        def broken(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==3:
                raise RuntimeError("private diagnostic must not be exposed")
            return actual(*args,**kwargs)
        monkeypatch.setattr(module,"observe",broken)
        assert engine.run_slice(max_bars=11,budget_seconds=10)
        with pool.transaction(TENANT_A,USER_A) as conn:
            checkpoint=conn.execute("SELECT checkpoint_json FROM research_structure_checkpoints WHERE run_id=%s",(request["id"],)).fetchone()[0]
            assert checkpoint["processed"]==11
            assert conn.execute("SELECT count(*) FROM research_structure_inputs WHERE run_id=%s",(request["id"],)).fetchone()[0]==11
            conn.execute("UPDATE research_backtests SET lease_until=NULL WHERE id=%s",(request["id"],))
        current=BacktestRepository(pool).get(TENANT_A,USER_A,request["id"])
        assert current["state"]=="RUNNING" and "private" not in current["error"]
        monkeypatch.setattr(module,"observe",actual)
        assert engine.run_slice(max_bars=11,budget_seconds=10)
        assert BacktestRepository(pool).get(TENANT_A,USER_A,request["id"])["progress"]["processed_bars"]==22
    finally:
        pool.close()
