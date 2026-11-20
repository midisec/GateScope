"""Isolated gatescope_test only: reloptions, rollback and original facts."""
from pathlib import Path
import sys

import psycopg
from psycopg.types.json import Jsonb
import pytest

from tests.integration.test_postgres_rls import INSTRUMENT, seed_two_tenants

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import storage_health_maintenance as maintenance


def test_storage_profile_preserves_facts_is_atomic_and_idempotent(migrated_database, tmp_path, monkeypatch):
    seed_two_tenants(migrated_database)
    monkeypatch.setattr(maintenance, "storage_paths", lambda *_: {"APPLICATION": tmp_path})
    monkeypatch.setattr(maintenance, "disk_report", lambda _: [{"scope": "APPLICATION",
        "free_bytes": 6 * maintenance.GIB, "total_bytes": 70 * maintenance.GIB, "used_percent": 91.4}])
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        conn.execute("""INSERT INTO market_pattern_events
            (context_id,candidate_id,sequence,instrument_id,timeframe,version,source_mode,phase,observed_time,event_json)
            VALUES('maintenance-test','candidate',1,%s,'1m','storage-test','REPLAY','INVALIDATED',now(),%s)""",
            (INSTRUMENT, Jsonb({"first": {"forecast": {"target_price": 123.45}}, "phase": "INVALIDATED"})))
        conn.execute("""INSERT INTO market_structure_stages
            (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,phase,sampled,formed,prediction,forecast_json)
            VALUES('maintenance-test','candidate','double_bottom','FORMING','HIGH',100,'INVALIDATED',true,false,0.75,%s)""",
            (Jsonb({"target_price": 123.45}),))
        conn.execute("""INSERT INTO market_structure_returns
            (context_id,evidence_id,pattern,phase,horizon,observed_time,status,side,net_return)
            VALUES('maintenance-test','evidence','double_bottom','FORMING',5,100,'COMPLETE',1,-0.02)""")

        def facts():
            return {table: conn.execute(f"SELECT xmin::text,to_jsonb(item) FROM {table} item WHERE context_id='maintenance-test'").fetchall()
                    for table in maintenance.TABLES}

        def options():
            return conn.execute("""SELECT c.relname,c.reloptions,t.reloptions
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_class t ON t.oid=c.reltoastrelid
                WHERE n.nspname='public' AND c.relname=ANY(%s) ORDER BY c.relname""", (list(maintenance.TABLES),)).fetchall()

        before, old_options = facts(), options()
        report = maintenance.run(conn)
        assert report["action_status"] == "DRY_RUN"
        assert facts() == before and options() == old_options

        # Actual conflicting maintenance must refuse immediately and leave all
        # three tables' options and row versions unchanged, even the first lock.
        with psycopg.connect(migrated_database) as holder:
            holder.execute("LOCK TABLE market_structure_stages IN SHARE UPDATE EXCLUSIVE MODE")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                maintenance.run(conn, apply=True)
        assert facts() == before and options() == old_options

        applied = maintenance.run(conn, apply=True)
        assert applied["action_status"] == "APPLIED"
        assert set(applied["applied_tables"]) == set(maintenance.TABLES)
        assert facts() == before
        for table, parent, toast in options():
            current = maintenance.parse_options(parent)
            current.update({"toast." + key: value for key, value in maintenance.parse_options(toast).items()})
            for key, expected in maintenance.profile(table).items():
                assert maintenance._same_option(current.get(key), expected), (table, key, current)
        final_options = options()
        again = maintenance.run(conn, apply=True)
        assert again["action_status"] == "ALREADY_CONFIGURED"
        assert again["applied_tables"] == []
        assert facts() == before and options() == final_options
