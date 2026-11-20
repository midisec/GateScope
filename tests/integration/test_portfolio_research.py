"""Portfolio API/RLS/allocation checks using synthetic immutable source studies.

This fixture tests finite-capital accounting, not pattern recognition accuracy.
"""
from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import UUID,uuid4

import psycopg
import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from apps.api.research import build_research_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_portfolio_research import PortfolioResearchRepository
from gatescope.quant.artifacts import report_digest
from gatescope.quant.backtest import StrategySpec
from tests.integration.test_postgres_rls import INSTRUMENT,TENANT_A,TENANT_B,USER_A,USER_B,seed_two_tenants
from tests.unit.test_portfolio_research import START,candidate


def source_run(api,worker,instrument,name,*,unfilled=False):
    run=BacktestRepository(api).create(TENANT_A,USER_A,instrument,"1m",
        StrategySpec(engine_mode="STRUCTURE",availability_mode="HISTORICAL_CLOSE",slippage_bps=0,fee_bps=10,funding_bps_per_8h=0),300,None,None)["id"]
    case=candidate(name,name,entry=120,exit=180)
    policy={"fee_bps":10.,"slippage_bps":0.,"funding_bps_per_8h":0.,"max_holding_bars":48}
    record={**case,"plan":{"side":1,"stop_price":95.,"policy":policy}}
    with worker.transaction(TENANT_A,USER_A) as conn:
        for index in range(300):
            stamp=START+index*60
            bar={"ts":stamp,"open":100.,"high":110.,"low":95.,"close":100.,"volume":10.,"available_at":stamp+60}
            conn.execute("INSERT INTO research_structure_inputs(run_id,tenant_id,owner_user_id,bar_index,open_time,bar_json) VALUES(%s,%s,%s,%s,%s,%s)",
                (run,TENANT_A,USER_A,index,stamp,Jsonb(bar)))
        conn.execute("""INSERT INTO research_structure_cases(run_id,tenant_id,owner_user_id,candidate_id,pattern,observed_time,
            selected,baseline,selection_reasons,environment,state,entry_time,exit_time,net_return,fees,funding,case_json)
            VALUES(%s,%s,%s,%s,'double_bottom',%s,true,true,'[]','{}','TARGET',%s,%s,.0979,.0021,0,%s)""",
            (run,TENANT_A,USER_A,name,START+1,START+120,START+180,Jsonb(record)))
        if unfilled:
            missed={**record,"entry_time":None,"exit_time":None,"state":"MISSED"}
            conn.execute("""INSERT INTO research_structure_cases(run_id,tenant_id,owner_user_id,candidate_id,pattern,observed_time,
                selected,baseline,selection_reasons,environment,state,case_json)
                VALUES(%s,%s,%s,'missed','double_bottom',%s,true,true,'[]','{}','MISSED',%s)""",
                (run,TENANT_A,USER_A,START+1,Jsonb(missed)))
        report={"range":{"from":datetime.fromtimestamp(START,timezone.utc).isoformat(),
            "to":datetime.fromtimestamp(START+18000,timezone.utc).isoformat()},"engine_version":"structure-fixture",
            "input_digest":"synthetic-portfolio-test","bar_count":300,"full":{"metrics":{"trade_count":1}}}
        report["artifact_digest"]=report_digest(report)
        conn.execute("INSERT INTO research_backtest_results(run_id,tenant_id,owner_user_id,report_json) VALUES(%s,%s,%s,%s)",
            (run,TENANT_A,USER_A,Jsonb(report)))
        conn.execute("UPDATE research_backtests SET state='SUCCEEDED',completed_at=now() WHERE id=%s",(run,))
    return run


def test_portfolio_shared_cash_sources_isolation_and_frozen_pages(migrated_database):
    seed_two_tenants(migrated_database)
    other=uuid4()
    with psycopg.connect(migrated_database) as conn:
        conn.execute("""INSERT INTO instruments(id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
            VALUES(%s,'GATE','FUTURES_USDT','SOL_USDT','SOL','USDT','USDT','ACTIVE',now(),now())""",(other,))
    api=PostgresPool(make_conninfo(migrated_database,options="-c role=gatescope_api"))
    worker=PostgresPool(make_conninfo(migrated_database,options="-c role=gatescope_strategy"))
    api.open()
    worker.open()
    try:
        left=source_run(api,worker,INSTRUMENT,"left",unfilled=True)
        right=source_run(api,worker,other,"right")
        app=FastAPI()
        app.include_router(build_research_router(api))
        identity=SimpleNamespace(tenant_id=TENANT_A,user_id=USER_A,role=SimpleNamespace(value="OWNER"))

        @app.middleware("http")
        async def identify(request: Request,call_next):
            request.state.auth_session=identity
            return await call_next(request)

        with TestClient(app) as client:
            payload={"source_run_ids":[str(left),str(right)],"initial_equity":1000,"max_positions":1}
            identity.role.value="VIEWER"
            assert client.post("/api/v2/research/portfolios",json=payload).status_code==403
            identity.role.value="OWNER"
            submitted=client.post("/api/v2/research/portfolios",json=payload)
            assert submitted.status_code==202,submitted.text
            run=UUID(submitted.json()["id"])
            assert client.post("/api/v2/research/portfolios",json=payload).status_code==409
            assert PortfolioResearchRepository(worker).run_one()
            assert not PortfolioResearchRepository(worker).run_one()
            fetched=client.get(f"/api/v2/research/portfolios/{run}").json()
            assert fetched["state"]=="SUCCEEDED",fetched["error"]
            report=fetched["report"]
            assert report_digest(report)==report["artifact_digest"]
            assert report["source_case_count"]==3 and report["accepted_count"]==1 and report["rejected_count"]==2
            assert report["rejection_reasons"]=={"SOURCE_MISSED":1,"MAX_CONCURRENT_POSITIONS":1}
            expected_profit=(10/(.05+.001*(1+.95)))*(.1-.001*(1+1.1))
            assert report["final_cash"]==pytest.approx(1000+expected_profit)
            assert report["final_reserved"]==0
            assert report["metrics"]["trade_count"]==1 and report["metrics_available"]
            assert report["source_manifest"][0]["run_id"]==str(left)
            assert client.get("/api/v2/research/portfolios").json()["total"]==1
            base=f"/api/v2/research/portfolios/{run}"
            trades=client.get(f"{base}/trades?limit=1").json()
            assert trades["total"]==1 and trades["items"][0]["symbol"]=="BTC_USDT"
            assert trades["items"][0]["net_pnl"]==pytest.approx(expected_profit)
            decisions=client.get(f"{base}/decisions?limit=2").json()
            assert decisions["total"]==3 and decisions["next_offset"]==2
            curve=client.get(f"{base}/curve?offset=299&limit=10").json()
            assert curve["total"]==301 and len(curve["items"])==2
            assert all(point["cash"]>=0 for point in client.get(f"{base}/details/curve?limit=512").json()["items"])
            assert client.get(f"{base}/curve?offset=1000001").status_code==422
            with worker.transaction(TENANT_A,USER_A) as conn:
                with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                    conn.execute("UPDATE research_portfolios SET report_json='{}' WHERE id=%s",(run,))
            with worker.transaction(TENANT_B,USER_B) as conn:
                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    conn.execute("""INSERT INTO research_portfolio_chunks(run_id,tenant_id,owner_user_id,kind,chunk_index,row_count,payload,digest)
                        VALUES(%s,%s,%s,'trades',900,1,%s,%s)""",(run,TENANT_B,USER_B,b"invalid","0"*64))
            with worker.transaction(TENANT_A,USER_A) as conn:
                for _label,query in (
                    ("input","INSERT INTO research_structure_inputs(run_id,tenant_id,owner_user_id,bar_index,open_time,bar_json) VALUES(%s,%s,%s,400,9999999999,'{}')"),
                    ("event","INSERT INTO research_structure_events(run_id,tenant_id,owner_user_id,candidate_id,sequence,observed_time,phase,pattern,event_json) VALUES(%s,%s,%s,'late',1,9999999999,'CONFIRMED','double_bottom','{}')"),
                    ("case","INSERT INTO research_structure_cases(run_id,tenant_id,owner_user_id,candidate_id,pattern,observed_time,selected,baseline,selection_reasons,environment,state,case_json) VALUES(%s,%s,%s,'late','double_bottom',9999999999,true,true,'[]','{}','TARGET','{}')"),
                ):
                    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                        with conn.transaction():
                            conn.execute(query,(left,TENANT_A,USER_A))
            for tenant,user in ((TENANT_A,USER_B),(TENANT_B,USER_B)):
                identity.tenant_id,identity.user_id=tenant,user
                assert client.get(f"{base}").status_code==404
                assert client.get(f"{base}/trades").status_code==404
                assert client.get("/api/v2/research/portfolios").json()["total"]==0
                assert client.post("/api/v2/research/portfolios",json=payload).status_code==404
    finally:
        api.close()
        worker.close()
