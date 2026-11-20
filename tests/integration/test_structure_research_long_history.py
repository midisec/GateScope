"""Real PostgreSQL capacity/checkpoint coverage beyond the old 5000-bar ceiling.

The detector is deliberately stubbed to emit no events: this tests streamed SQL
storage, small transaction boundaries and restart/pagination, NOT recognition
accuracy or detector throughput. Real detection has separate integration tests.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from apps.api.research import build_research_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.adapters.postgres_structure_research import StructureResearchRepository
from gatescope.quant.artifacts import report_digest
from gatescope.quant.pattern_study import WINDOW
from tests.integration.test_postgres_rls import TENANT_A, USER_A, INSTRUMENT, seed_two_tenants
from tests.unit.test_research_backtest import make_bars


def test_6001_bars_streamed_in_small_slices_resume_and_last_input_page(migrated_database, monkeypatch):
    seed_two_tenants(migrated_database)
    observed_indices = []

    def no_candidates(candles, index, state, *, index_offset=0, **kwargs):
        # Preserve observe's minimal checkpoint shape while bypassing detection.
        absolute = index + index_offset
        observed_indices.append(absolute)
        state.update(last_index=absolute, last_ts=candles[index]["ts"], records={}, sampling={}, active=[], facts=[])
        return []

    monkeypatch.setattr("gatescope.adapters.postgres_structure_research.observe", no_candidates)
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"))
    market = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"))
    for pool in (api, worker, market):
        pool.open()
    try:
        bars = make_bars(6001)
        assert bars[-1].close_time < datetime.now(timezone.utc)
        PostgresMarketIngestRepository(market).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            [(int(bar.open_time.timestamp()), float(bar.open), float(bar.high), float(bar.low),
              float(bar.close), float(bar.volume)) for bar in bars],
            observed_at=datetime.now(timezone.utc),
        )
        app = FastAPI()
        app.include_router(build_research_router(api))

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.auth_session = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A,
                role=SimpleNamespace(value="OWNER"))
            return await call_next(request)

        with TestClient(app) as client:
            response = client.post("/api/v2/research/backtests", json={
                "instrument_id": str(INSTRUMENT), "timeframe": "1h", "bar_limit": 6001,
                "spec": {"engine_mode": "STRUCTURE", "availability_mode": "HISTORICAL_CLOSE"},
            })
            assert response.status_code == 202, response.text
            run = UUID(response.json()["id"])
            engine = StructureResearchRepository(worker)
            reads = BacktestRepository(api)
            processed, restarted, slices = 0, False, 0
            while processed < 6001 and slices < 200:
                assert engine.run_slice(max_bars=64, budget_seconds=1.5)
                current = reads.get(TENANT_A, USER_A, run)
                assert current["state"] != "FAILED", current["error"]
                now_processed = current["progress"]["processed_bars"]
                assert 0 < now_processed - processed <= 64
                processed, slices = now_processed, slices + 1
                with worker.transaction(TENANT_A, USER_A) as conn:
                    checkpoint = conn.execute(
                        "SELECT checkpoint_json FROM research_structure_checkpoints WHERE run_id=%s", (run,)
                    ).fetchone()[0]
                    assert checkpoint["processed"] == processed
                    assert len(checkpoint["bars"]) <= WINDOW
                    if processed >= 20:
                        assert not checkpoint["lifecycle"]["records"]
                if processed >= 3000 and not restarted:
                    # Discard the connection pool too: nothing in process memory
                    # may be required to continue beyond the durable cursor.
                    worker.close()
                    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"))
                    worker.open()
                    engine = StructureResearchRepository(worker)
                    restarted = True
            assert restarted and processed == 6001 and slices >= 94
            assert current["state"] == "SUCCEEDED"
            assert not engine.run_slice(max_bars=64)
            assert observed_indices == list(range(19, 6001))
            report = current["report"]
            assert report["bar_count"] == 6001 and report_digest(report) == report["artifact_digest"]
            assert "input_bars" not in report and "candles" not in report and "data_manifest" not in report
            assert report["structure_research"]["case_count"] == 0  # Empty stub is not detection evidence.
            with worker.transaction(TENANT_A, USER_A) as conn:
                count, distinct_count, first_index, last_index = conn.execute(
                    """SELECT count(*),count(DISTINCT open_time),min(bar_index),max(bar_index)
                         FROM research_structure_inputs WHERE run_id=%s""", (run,)
                ).fetchone()
                assert (count, distinct_count, first_index, last_index) == (6001, 6001, 0, 6000)
            last_page = client.get(f"/api/v2/research/backtests/{run}/inputs?offset=6000&limit=1")
            assert last_page.status_code == 200
            page = last_page.json()
            assert page["total"] == 6001 and len(page["items"]) == 1
            assert page["items"][0]["ts"] == int(bars[-1].open_time.timestamp())
            assert page["has_more"] is False and page["next_offset"] is None
            assert client.get(f"/api/v2/research/backtests/{run}/inputs?offset=6001&limit=1").json()["items"] == []
    finally:
        for pool in (api, worker, market):
            pool.close()
