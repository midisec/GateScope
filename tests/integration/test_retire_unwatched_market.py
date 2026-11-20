"""Isolated PostgreSQL only: retirement preserves immutable evidence and peers."""
import copy
from pathlib import Path
import sys
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres_structure_archive import context_id, write_journal
from gatescope.adapters import annual_structure_study as annual
from gatescope.quant.structure_archive_codec import unpack_structure_record
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_B, USER_B, seed_two_tenants
from tests.unit.test_retire_unwatched_market import NOW, VERSION, tracking_state

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import retire_unwatched_market as retire

OTHER = UUID("40000000-0000-0000-0000-000000000044")


def seed(conn):
    conn.execute("""INSERT INTO instruments(id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
        VALUES(%s,'GATE','FUTURES_USDT','ETH_USDT','ETH','USDT','USDT','ACTIVE',now(),now())""", (OTHER,))
    for instrument in (INSTRUMENT, OTHER):
        state = tracking_state()
        record = state["records"]["a" * 64]
        record["forecast_outcome"] = {"status": "PENDING"}
        record["latest"]["forecast_outcome"] = {"status": "PENDING"}
        conn.execute("INSERT INTO market_sync_state(instrument_id,timeframe,active) VALUES(%s,'1m',%s)", (instrument, instrument == OTHER))
        conn.execute("""INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json)
            VALUES(%s,'1m',%s,%s)""", (instrument, VERSION, Jsonb(state)))
        conn.execute("""INSERT INTO market_pattern_studies(instrument_id,timeframe,version,state,cursor,total)
            VALUES(%s,'1m',%s,'RUNNING',100,1000)""", (instrument, VERSION))
        conn.execute("""INSERT INTO market_live_candles(instrument_id,timeframe,open_time,close_time,open,high,low,close,volume,observed_at)
            VALUES(%s,'1m',to_timestamp(%s),to_timestamp(%s),100,101,99,100,5,%s)""",
            (instrument, state["last_ts"], state["last_ts"] + 60, NOW))
        conn.execute("INSERT INTO market_chart_snapshots(instrument_id,timeframe) VALUES(%s,'1m')", (instrument,))
        conn.execute("""INSERT INTO market_history_deferred(instrument_id,timeframe,from_time,to_time,retry_at,reason)
            VALUES(%s,'1m',100,160,%s,'ARCHIVE_NOT_PUBLISHED')""", (instrument, NOW))
        journal = [{"candidate_id": record["id"], "sequence": 1, "from_phase": None, "phase": "FORMING",
            "observed_time": record["observed_time"], "record": record}]
        context = context_id(instrument, "1m", version=VERSION)
        write_journal(conn, instrument, "1m", context, journal, "RECORDED", version=VERSION)
        annual.project_journal(conn, context, journal)


def run(conn, **kwargs):
    return retire.run(conn, instrument_id=INSTRUMENT, expected_symbol="BTC_USDT",
        expected_market="FUTURES_USDT", now=NOW, **kwargs)


@pytest.fixture
def isolated_storage_probe(monkeypatch):
    monkeypatch.setattr(retire, "storage_paths", lambda *args: {"APPLICATION": Path("/isolated")})
    monkeypatch.setattr(retire, "capacity", lambda _: None)


def test_retire_preserves_original_record_and_other_instruments(migrated_database, isolated_storage_probe):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        seed(conn)
        original = conn.execute("""SELECT gatescope_security.structure_event_record(event_json,event_blob)
            FROM market_pattern_events WHERE instrument_id=%s""", (INSTRUMENT,)).fetchone()[0]
        original = unpack_structure_record(original)
        assert run(conn)["status"] == "DRY_RUN"
        assert conn.execute("SELECT count(*) FROM market_pattern_tracking").fetchone()[0] == 2
        audits = []
        result = run(conn, apply=True, audit=lambda row: audits.append(copy.deepcopy(row)))
        assert result["status"] == "COMMITTED" and result["censored_candidates"] == 1
        assert [row["status"] for row in audits] == ["PLAN", "COMMITTED"]
        for table in retire.HOT_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 0
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE instrument_id=%s", (OTHER,)).fetchone()[0] == 1
        events = conn.execute("""SELECT phase,gatescope_security.structure_event_record(event_json,event_blob)
            FROM market_pattern_events WHERE instrument_id=%s ORDER BY sequence""", (INSTRUMENT,)).fetchall()
        assert [row[0] for row in events] == ["FORMING", "CENSORED"]
        assert unpack_structure_record(events[0][1]) == original
        final = unpack_structure_record(events[1][1])
        assert final["first"] == original["first"] and final["reason"] == "MONITORING_STOPPED"
        assert final["forecast_outcome"]["status"] == "CENSORED"
        stage = conn.execute("SELECT phase,outcome_json FROM market_structure_stages WHERE context_id=%s",
            (context_id(INSTRUMENT, "1m", version=VERSION),)).fetchone()
        assert stage[0] == "CENSORED" and stage[1]["status"] == "CENSORED"
        assert conn.execute("SELECT active FROM market_sync_state WHERE instrument_id=%s", (INSTRUMENT,)).fetchone() == (False,)
        assert run(conn)["censored_candidates"] == 0


def test_other_owner_watchlist_blocks_retirement_and_worker_lock_is_nonblocking(migrated_database, isolated_storage_probe):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        seed(conn)
        watch = conn.execute("""INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at)
            VALUES(gen_random_uuid(),%s,%s,'Other owner',now(),now()) RETURNING id""", (TENANT_B, USER_B)).fetchone()[0]
        conn.execute("""INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at)
            VALUES(gen_random_uuid(),%s,%s,%s,0,'1m',now(),now())""", (TENANT_B, watch, INSTRUMENT))
        with pytest.raises(retire.Refused, match="WATCHED"):
            run(conn, apply=True, audit=lambda _: None)
        assert conn.execute("SELECT count(*) FROM market_pattern_tracking").fetchone()[0] == 2
        conn.execute("DELETE FROM watchlist_items WHERE watchlist_id=%s", (watch,))
        with psycopg.connect(migrated_database) as holder:
            holder.execute("SELECT pg_advisory_xact_lock(7342284)")
            with pytest.raises(retire.Refused, match="WORKER_BUSY"):
                run(conn, apply=True, audit=lambda _: None)
        assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == 2


def paused_rule(conn):
    rule = conn.execute("""INSERT INTO alert_rules(id,tenant_id,owner_user_id,name,current_version,status,created_at,updated_at)
        VALUES(gen_random_uuid(),%s,%s,'Retirement concurrency',1,'PAUSED',now(),now()) RETURNING id""",
        (TENANT_B, USER_B)).fetchone()[0]
    conn.execute("""INSERT INTO alert_rule_versions(id,tenant_id,alert_rule_id,version,condition_json,routing_json,created_by,created_at)
        VALUES(gen_random_uuid(),%s,%s,1,%s,'{}',%s,now())""",
        (TENANT_B, rule, Jsonb({"instrument_id": str(INSTRUMENT), "timeframe": "1m"}), USER_B))
    return rule


def test_retire_blocks_target_resume_new_alert_and_sync_activation_but_not_other_market_reads(migrated_database, isolated_storage_probe):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        seed(conn)
        rule = paused_rule(conn)
        verified = []
        def audit(row):
            if row["status"] != "PLAN":
                return
            statements = [
                ("UPDATE alert_rules SET status='ACTIVE' WHERE id=%s", (rule,)),
                ("UPDATE market_sync_state SET active=true WHERE instrument_id=%s", (INSTRUMENT,)),
                ("""INSERT INTO alert_rules(id,tenant_id,owner_user_id,name,current_version,status,created_at,updated_at)
                    VALUES(gen_random_uuid(),%s,%s,'Concurrent new',1,'ACTIVE',now(),now())""", (TENANT_B, USER_B)),
            ]
            for sql, args in statements:
                with psycopg.connect(migrated_database, autocommit=True) as writer:
                    writer.execute("SET lock_timeout='150ms'")
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        writer.execute(sql, args)
                    assert writer.execute("SELECT active FROM market_sync_state WHERE instrument_id=%s", (OTHER,)).fetchone() == (True,)
                    assert writer.execute("UPDATE market_sync_state SET updated_at=updated_at WHERE instrument_id=%s", (OTHER,)).rowcount == 1
                verified.append(True)
        assert run(conn, apply=True, audit=audit)["status"] == "COMMITTED"
        assert len(verified) == 3
        assert conn.execute("SELECT status FROM alert_rules WHERE id=%s", (rule,)).fetchone() == ("PAUSED",)
        assert conn.execute("SELECT active FROM market_sync_state WHERE instrument_id=%s", (INSTRUMENT,)).fetchone() == (False,)


def test_existing_alert_writer_causes_immediate_safe_retirement_refusal(migrated_database, isolated_storage_probe):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        seed(conn)
        rule = paused_rule(conn)
        with psycopg.connect(migrated_database) as holder:
            holder.execute("UPDATE alert_rules SET etag=etag+1 WHERE id=%s", (rule,))
            with pytest.raises(retire.Refused, match="DEPENDENCY_WRITER_BUSY"):
                run(conn, apply=True, audit=lambda _: None)
        assert conn.execute("SELECT count(*) FROM market_pattern_tracking").fetchone()[0] == 2


def test_deferred_cache_is_removed_in_fixed_batches_without_delaying_core_retirement(migrated_database, isolated_storage_probe):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        seed(conn)
        conn.execute("""INSERT INTO market_history_deferred(instrument_id,timeframe,from_time,to_time,retry_at,reason)
            SELECT %s,'1m',1000+n*60,1000+n*60,now(),'ARCHIVE_NOT_PUBLISHED' FROM generate_series(1,300) n""", (INSTRUMENT,))
        first = run(conn, apply=True, audit=lambda _: None)
        assert first["status"] == "COMMITTED" and first["deferred_remaining"] is True
        assert first["hot_rows"]["market_history_deferred"] == 256
        assert conn.execute("SELECT count(*) FROM market_history_deferred WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 45
        assert conn.execute("SELECT count(*) FROM market_pattern_tracking WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 0
        second = run(conn, apply=True, audit=lambda _: None)
        assert second["deferred_remaining"] is False and second["censored_candidates"] == 0
        assert second["hot_rows"]["market_history_deferred"] == 45
        assert conn.execute("SELECT count(*) FROM market_history_deferred WHERE instrument_id=%s", (OTHER,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM market_pattern_events WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 2
