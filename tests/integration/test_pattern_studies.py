from datetime import datetime, timezone, timedelta
from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from apps.api.pattern_studies import build_pattern_study_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.adapters.postgres_structure_archive import StructureArchive, context_id, write_journal
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.pattern_study import catalog
from tests.integration.test_postgres_rls import TENANT_A, USER_A, INSTRUMENT, seed_two_tenants
from tests.unit.test_research_backtest import make_bars


def test_public_study_queue_roles_snapshot_and_restart(migrated_database):
    seed_two_tenants(migrated_database)
    pools = [PostgresPool(make_conninfo(migrated_database, options=f"-c role={role}"), max_size=2)
             for role in ("gatescope_api", "gatescope_strategy", "gatescope_market")]
    for pool in pools:
        pool.open()
    api_pool, worker_pool, market_pool = pools
    try:
        bars = make_bars(180)
        shift=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)-bars[-1].close_time-timedelta(days=1)
        bars=[replace(b,open_time=b.open_time+shift,close_time=b.close_time+shift,available_at=b.available_at+shift,payload_hash="") for b in bars]
        rows = [(int(b.open_time.timestamp()), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)) for b in bars]
        PostgresMarketIngestRepository(market_pool).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"), rows, observed_at=datetime.now(timezone.utc))
        app = FastAPI()
        app.include_router(build_pattern_study_router(api_pool))

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.auth_session = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A, role=SimpleNamespace(value="VIEWER"))
            return await call_next(request)

        worker = PatternStudyRepository(worker_pool)
        with TestClient(app) as client:
            query = f"instrument_id={INSTRUMENT}&timeframe=1h"
            assert client.get("/api/v2/patterns/study?" + query).json()["state"] == "NOT_REQUESTED"
            current = client.get("/api/v2/patterns/current?" + query)
            assert current.status_code == 200, current.text
            assert len(current.json()["catalog"]) == len(catalog())
            assert current.json()["tracking_status"] == "QUEUED"
            assert worker.track_slice()
            with worker_pool.public_transaction() as conn:
                assert conn.execute("SELECT count(*) FROM market_structure_stages WHERE context_id=%s",
                    (context_id(INSTRUMENT,"1h"),)).fetchone()[0]>0
            assert PatternStudyRepository(api_pool).current(INSTRUMENT,"1h")["bars_scanned"] == 180
            page=client.get("/api/v2/patterns/current?"+query+"&limit=1").json()
            assert page["total"]>1 and len(page["items"])==1
            assert page["tracked_total"]==sum(page["phase_counts"].values())
            next_page=client.get("/api/v2/patterns/current?"+query+"&limit=1&offset=1").json()
            assert next_page["items"][0]["id"]!=page["items"][0]["id"]
            assert client.get("/api/v2/patterns/current?"+query+"&limit=999").status_code==422
            assert client.get("/api/v2/patterns/current?"+query+"&family=nonsense").status_code==422
            assert client.get("/api/v2/patterns/current?"+query+"&q=unmatched").json()["total"]==0
            payload = {"instrument_id": str(INSTRUMENT), "timeframe": "1h"}
            response = client.post("/api/v2/patterns/study", json=payload)
            assert response.status_code == 202, response.text
            requested = response.json()["requested_at"]
            assert client.post("/api/v2/patterns/study", json=payload).json()["requested_at"] == requested
            assert worker.run_slice(max_steps=3)
            partial = client.get("/api/v2/patterns/study?" + query).json()
            assert partial["state"] == "RUNNING" and partial["cursor"] == 3
            with worker_pool.public_transaction() as conn:
                frozen = conn.execute("SELECT input_json FROM market_pattern_studies").fetchone()[0]
            # Separate process/repository: resume durable cursor, never restart or reread input.
            restarted = PatternStudyRepository(worker_pool)
            for _ in range(8):
                if not restarted.run_slice(seconds=5, max_steps=64):
                    break
            result = client.get("/api/v2/patterns/study?" + query).json()
            assert result["state"] == "READY", result
            report = result["report"]
            assert report["bar_count"] == 180 and len(report["rows"]) == len(catalog())*8
            assert report["version"] == VERSION and len(report["completion_rows"])==len(catalog())*10
            assert report["metadata"]["snapshot_at"] == frozen["metadata"]["snapshot_at"]
            assert len(report["metadata"]["input_digest"])==64
            assert report["metadata"]["period_days"]==365 and report["metadata"]["target_bars"]==8760
            assert report["metadata"]["missing_bars"]==8580
            assert report["availability_mode"] == "HISTORICAL_CLOSE"
            # Replaying the fixture may exceed five wall-clock minutes on a
            # contended runner. Exercise cooldown with an explicit fresh clock,
            # then restore the original time which identifies its archive context.
            with worker_pool.public_transaction() as conn:
                original_requested = conn.execute(
                    "SELECT requested_at FROM market_pattern_studies WHERE instrument_id=%s AND timeframe='1h'",
                    (INSTRUMENT,),
                ).fetchone()[0]
                assert conn.execute(
                    """UPDATE market_pattern_studies SET requested_at=now()
                       WHERE instrument_id=%s AND timeframe='1h'
                       RETURNING requested_at>now()-interval '5 minutes'""", (INSTRUMENT,),
                ).fetchone() == (True,)
            try:
                assert client.post("/api/v2/patterns/study", json=payload).json()["state"] == "READY"  # Cooldown.
            finally:
                with worker_pool.public_transaction() as conn:
                    conn.execute(
                        "UPDATE market_pattern_studies SET requested_at=%s WHERE instrument_id=%s AND timeframe='1h'",
                        (original_requested, INSTRUMENT),
                    )
            assert client.post("/api/v2/patterns/study", json={**payload, "timeframe": "nonsense"}).status_code == 422
            assert client.post("/api/v2/patterns/study", json={**payload, "instrument_id": str(USER_A)}).status_code == 404
            assert not worker.run_slice()
            listing=client.get("/api/v2/patterns/events?"+query+"&source=REPLAY&limit=2").json()
            assert listing["total"]>2 and len(listing["items"])==2
            first=listing["items"][0]
            detail=client.get(f"/api/v2/patterns/events/{first['context_id']}/{first['id']}")
            assert detail.status_code==200,detail.text
            assert detail.json()["transitions"] and detail.json()["bars"]
            assert detail.json()["price_basis"]=="ORIGINAL_REVISION_THEN_FOLLOWUP"
            failed=client.get("/api/v2/patterns/events?"+query+"&source=REPLAY&phase=INVALIDATED").json()
            assert failed["total"]>0
            assert all(r["phase"]=="INVALIDATED" for r in failed["items"])
            assert client.get("/api/v2/patterns/events?"+query+"&limit=999").status_code==422
            original_chart=detail.json()
            revised=list(rows)
            t,o,h,l,c,v=revised[50]
            revised[50]=(t,o*2,h*2,l*2,c*2,v)
            extra=make_bars(190)
            extra=[replace(b,open_time=b.open_time+shift,close_time=b.close_time+shift,available_at=b.available_at+shift,payload_hash="") for b in extra]
            revised.extend((int(b.open_time.timestamp()),float(b.open),float(b.high),float(b.low),float(b.close),float(b.volume)) for b in extra[180:])
            PostgresMarketIngestRepository(market_pool).upsert_final_bars(
                MarketSubscription(INSTRUMENT,"GATE","FUTURES_USDT","BTC_USDT","USDT","1h"),revised,observed_at=datetime.now(timezone.utc))
            with psycopg.connect(migrated_database) as conn:
                conn.execute("UPDATE market_pattern_tracking SET updated_at='epoch'")
            assert restarted.track_slice(max_steps=1)
            with psycopg.connect(migrated_database) as conn:
                conn.execute("UPDATE market_pattern_tracking SET updated_at='epoch'")
            assert PatternStudyRepository(worker_pool).track_slice(seconds=5,max_steps=16)
            with worker_pool.public_transaction() as conn:
                live=conn.execute("SELECT checkpoint_json FROM market_pattern_tracking").fetchone()[0]
                assert live["last_ts"]==int(extra[-1].open_time.timestamp())
            after_revision=client.get(f"/api/v2/patterns/events/{first['context_id']}/{first['id']}").json()
            # Corrections to market data must not replace the candidate's original candle versions.
            cutoff=original_chart["original_through"]
            assert [b for b in original_chart["bars"] if b["ts"]<cutoff]==[b for b in after_revision["bars"] if b["ts"]<cutoff]
            # Retire an old-model live candidate explicitly, not as a fabricated failure.
            old_record=original_chart["transitions"][0]["record"]
            old_state={"records":{old_record["id"]:old_record},"active":[old_record["id"]],"last_index":old_record["first_index"],
                "last_ts":old_record["first_observed_time"]-3600,"step_seconds":3600}
            old_context=context_id(INSTRUMENT,"1h",version="structure-study-2")
            with worker_pool.public_transaction() as conn:
                conn.execute("INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json) VALUES(%s,'1h','structure-study-2',%s)",(INSTRUMENT,Jsonb(old_state)))
                write_journal(conn,INSTRUMENT,"1h",old_context,[{"candidate_id":old_record["id"],"sequence":1,"phase":old_record["phase"],
                    "from_phase":None,"observed_time":old_record["observed_time"],"record":old_record}],"RECORDED",version="structure-study-2")
            assert restarted.track_slice()
            retired=StructureArchive(api_pool).list(INSTRUMENT,"1h",requested_context=old_context)
            assert retired["items"][0]["phase"]=="CENSORED"
            assert "不计成败" in retired["items"][0]["reason"]
        with api_pool.public_transaction() as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE market_pattern_studies SET report_json='{}'")
        with worker_pool.public_transaction() as conn:
            row = conn.execute("SELECT input_json,checkpoint_json FROM market_pattern_studies").fetchone()
            assert row[0]["stream"] and len(row[0]["bars"])<=480
            assert row[1]["last_index"]==179 and not row[1].get("facts")
            assert conn.execute("SELECT count(*) FROM market_pattern_reports").fetchone()[0]==1
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM market_pattern_events")
        with psycopg.connect(migrated_database) as conn:
            with pytest.raises(psycopg.Error):
                conn.execute("UPDATE market_pattern_events SET phase='CONFIRMED'")
        # A new replay never deletes prior failed records or its completed report.
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE market_pattern_studies SET requested_at=now()-interval '10 minutes'")
        PatternStudyRepository(api_pool).request(INSTRUMENT,"1h")
        assert StructureArchive(api_pool).detail(first["context_id"],first["id"])
    finally:
        for pool in pools:
            pool.close()
