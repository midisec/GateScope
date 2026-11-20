from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.research import build_research_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.quant.backtest import StrategySpec
from gatescope.quant.artifacts import report_digest
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, INSTRUMENT, seed_two_tenants
from tests.unit.test_research_backtest import make_bars


def test_api_queue_worker_report_and_owner_isolation(migrated_database):
    seed_two_tenants(migrated_database)
    pools = [PostgresPool(make_conninfo(migrated_database, options=f"-c role={role}"))
             for role in ("gatescope_api", "gatescope_strategy", "gatescope_market")]
    for pool in pools:
        pool.open()
    api_pool, worker_pool, market_pool = pools
    try:
        document={"source":"fixed discovery trial ledger"}
        with worker_pool.transaction(TENANT_A,USER_A) as conn:
            conn.execute("INSERT INTO research_discovery_reports(id,tenant_id,owner_user_id,name,trial_count,report_json,artifact_digest) VALUES (%s,%s,%s,'trial ledger',2376,%s,%s)",
                (uuid4(),TENANT_A,USER_A,Jsonb(document),report_digest(document)))
        bars = make_bars()
        rows = [(int(b.open_time.timestamp()), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)) for b in bars]
        PostgresMarketIngestRepository(market_pool).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            rows, observed_at=datetime.now(timezone.utc))
        app = FastAPI()
        app.include_router(build_research_router(api_pool))
        identity = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A, role=SimpleNamespace(value="OWNER"))

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            request.state.auth_session = identity
            return await call_next(request)

        with TestClient(app) as client:
            assert len(client.get("/api/v2/research/strategies").json()["items"]) == 6
            catalog = client.get("/api/v2/research/strategies").json()
            assert len(catalog["rules"]) == 26
            assert catalog["defaults"]["availability_mode"] == "HISTORICAL_CLOSE"
            coverage = client.get(f"/api/v2/research/coverage?instrument_id={INSTRUMENT}&timeframe=1h").json()
            assert coverage["available_bars"] == 500
            invalid = client.post("/api/v2/research/backtests", json={"instrument_id": str(INSTRUMENT), "spec": {"fast_period": 50, "slow_period": 20}})
            assert invalid.status_code == 422
            identity.role.value = "VIEWER"
            assert client.post("/api/v2/research/backtests", json={"instrument_id": str(INSTRUMENT)}).status_code == 403
            identity.role.value = "OWNER"
            response = client.post("/api/v2/research/backtests", json={"instrument_id": str(INSTRUMENT), "timeframe": "1h", "bar_limit": 500, "spec": {"strategy": "breakout", "direction": "BOTH"}})
            assert response.status_code == 202
            identifier = UUID(response.json()["id"])
            duplicate = client.post("/api/v2/research/backtests", json={"instrument_id": str(INSTRUMENT)})
            assert duplicate.status_code == 409
            worker = BacktestRepository(worker_pool)
            assert worker.run_one()
            assert not worker.run_one()
            result = client.get(f"/api/v2/research/backtests/{identifier}")
            assert result.status_code == 200
            assert result.json()["state"] == "SUCCEEDED", result.json()["error"]
            assert result.json()["report"]["evidence"]["decision"] == "RESEARCH_ONLY"
            assert result.json()["report"]["evidence"]["tested_hypotheses"] == 2377
            assert report_digest(result.json()["report"]) == result.json()["report"]["artifact_digest"]
            assert len(result.json()["report"]["indicator_series"]) == 500
            with market_pool.public_transaction() as conn:
                stored = conn.execute("SELECT payload_hash FROM bars ORDER BY open_time LIMIT 1").fetchone()[0]
            assert result.json()["report"]["source_manifest"][0]["hash"] == stored
            history = client.get("/api/v2/research/backtests").json()["items"]
            assert history[0]["metrics"] == result.json()["report"]["full"]["metrics"]
            assert history[0]["bar_count"] == 500
            assert history[0]["screening"]["status"] == "OBSERVE_ONLY"
            assert history[0]["screening"]["thresholds_enforced"] is False
            assert client.get("/api/v2/research/backtests?screening=PASS").json()["total"] == 0
            assert client.get("/api/v2/research/backtests?screening=REJECTED").json()["total"] == 0
            assert client.get("/api/v2/research/backtests?screening=OBSERVE_ONLY").json()["total"] == 1
            assert client.get("/api/v2/research/backtests?screening=bogus").status_code == 422
            assert "report" not in history[0]
            assert client.get("/api/v2/research/backtests?limit=1").json()["total"] == 1
            assert client.get("/api/v2/research/backtests?limit=1&offset=1").json()["items"] == []
            assert client.get("/api/v2/research/backtests?offset=-1").status_code == 422
            assert client.get("/api/v2/research/backtests?limit=101").status_code == 422
            filtered = client.get(f"/api/v2/research/backtests?instrument_id={INSTRUMENT}&timeframe=1h").json()
            assert filtered["total"] == 1 and filtered["items"][0]["id"] == str(identifier)
            assert client.get(f"/api/v2/research/backtests?instrument_id={INSTRUMENT}&timeframe=5m").json()["total"] == 0
            assert client.get(f"/api/v2/research/backtests?instrument_id={USER_B}").json()["items"] == []
            assert client.get("/api/v2/research/backtests?timeframe=invalid").status_code == 422
            later = client.post("/api/v2/research/backtests", json={"instrument_id": str(INSTRUMENT)})
            assert later.status_code == 202
            first_page = client.get("/api/v2/research/backtests?limit=1").json()
            assert first_page["total"] == 2 and first_page["has_more"]
            assert first_page["next_offset"] == 1
            assert client.get("/api/v2/research/backtests?limit=1&offset=1").json()["items"][0]["id"] == str(identifier)
            assert client.get(f"/api/v2/research/trend?instrument_id={INSTRUMENT}").status_code == 200
        repository = BacktestRepository(api_pool)
        assert repository.list(TENANT_B, USER_B) == []
        assert repository.list(TENANT_A, USER_B) == []
        assert repository.count(TENANT_A, USER_B) == 0
        assert repository.count(TENANT_B, USER_B) == 0
        with pytest.raises(LookupError):
            repository.get(TENANT_A, USER_B, identifier)
        with worker_pool.transaction(TENANT_A, USER_A) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE research_backtest_results SET report_json='{}' WHERE run_id=%s", (identifier,))
        # A separate identity creates a composite run; exercise real queue + prefix scanner.
        composite = repository.create(TENANT_A, USER_B, INSTRUMENT, "1h",
            StrategySpec(
                strategy="composite", rule_ids=["trend", "pattern:double_bottom"], direction="BOTH", availability_mode="HISTORICAL_CLOSE"),
            500, None, None)
        while worker.run_one():
            pass
        composite = repository.get(TENANT_A, USER_B, composite["id"])
        assert composite["state"] == "SUCCEEDED"
        assert composite["report"]["selection"]["rules"][1]["name"] == "双底"
        assert composite["report"]["full"]["metrics"]["trade_count"] > 0
        assert composite["report"]["availability"]["assumed"]
        assert composite["report"]["evidence"]["tested_hypotheses"] == 1
    finally:
        for pool in pools:
            pool.close()
