"""Strict recorded-time portfolios cannot recycle a not-yet-known source exit."""
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_portfolio_research import PortfolioResearchRepository
from gatescope.quant.backtest import StrategySpec
from tests.integration.test_postgres_rls import INSTRUMENT,TENANT_A,USER_A,USER_B,seed_two_tenants
from tests.unit.test_portfolio_research import START,candidate


def test_recorded_exit_waits_for_entire_frozen_prefix_and_unknown_evidence_stays_unknown(migrated_database):
    seed_two_tenants(migrated_database)
    api=PostgresPool(make_conninfo(migrated_database,options="-c role=gatescope_api"))
    worker=PostgresPool(make_conninfo(migrated_database,options="-c role=gatescope_strategy"))
    api.open()
    worker.open()
    try:
        spec=StrategySpec(engine_mode="STRUCTURE",availability_mode="AS_RECORDED")
        run=BacktestRepository(api).create(TENANT_A,USER_A,INSTRUMENT,"1m",spec,300,None,None)["id"]
        policy={"fee_bps":10.,"slippage_bps":8.,"funding_bps_per_8h":1.,"max_holding_bars":10}
        with worker.transaction(TENANT_A,USER_A) as conn:
            for index,arrival in enumerate((60,600,180)):
                stamp=START+index*60
                bar={"ts":stamp,"open":100.,"high":110.,"low":95.,"close":100.,"volume":10.,"available_at":START+arrival}
                conn.execute("""INSERT INTO research_structure_inputs(run_id,tenant_id,owner_user_id,bar_index,open_time,bar_json)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(run,TENANT_A,USER_A,index,stamp,Jsonb(bar)))
            for name,state,last in (("target","TARGET",START+120),("missed","MISSED",START+120),("unknown","TARGET",None)):
                record={**candidate(name,entry=60,exit=180),"outcome_time":START+180,"last_bar_time":last,
                    "plan":{"side":1,"stop_price":95.,"policy":policy}}
                if state=="MISSED":
                    record.update(entry_time=None,exit_time=None,state=state)
                conn.execute("""INSERT INTO research_structure_cases(run_id,tenant_id,owner_user_id,candidate_id,pattern,
                    observed_time,selected,baseline,selection_reasons,environment,state,case_json)
                    VALUES(%s,%s,%s,%s,'double_bottom',%s,true,true,'[]','{}',%s,%s)""",
                    (run,TENANT_A,USER_A,name,START+1,state,Jsonb(record)))
        source={"run_id":str(run),"instrument_id":str(INSTRUMENT),"symbol":"BTC_USDT","timeframe":"1m","spec":spec.model_dump()}
        repository=PortfolioResearchRepository(worker)
        rows={row["candidate_id"]:row for row in repository._cases(TENANT_A,USER_A,[source])}
        # Final candle arrived on time, but an earlier candle delayed the source
        # prefix. Using only the resolving candle's availability would leak.
        assert rows["target"]["exit_time"]==START+180
        assert rows["target"]["exit_available_at"]==START+600
        assert rows["target"]["outcome_available_at"]==START+600
        assert rows["missed"]["outcome_available_at"]==START+600
        assert rows["unknown"]["exit_available_at"] is None
        assert rows["target"]["slippage_bps"]==8.
        assert rows["target"]["max_holding_bars"]==10
        assert repository._cases(TENANT_A,USER_B,[source])==[]
        historical={**source,"spec":{**source["spec"],"availability_mode":"HISTORICAL_CLOSE"}}
        previous={row["candidate_id"]:row for row in repository._cases(TENANT_A,USER_A,[historical])}
        assert previous["target"]["exit_available_at"]==START+180
    finally:
        api.close()
        worker.close()
