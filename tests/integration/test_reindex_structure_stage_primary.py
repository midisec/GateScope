"""Maintenance is exercised only on tiny disposable gatescope_test fixtures."""
from pathlib import Path
import sys

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import reindex_structure_stage_primary as maintenance


def prepare(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        # This fixture is restricted to the gatescope_test database by conftest.
        # Installing diagnostics here never installs anything on production.
        assert conn.execute("SELECT current_database()").fetchone()[0] == "gatescope_test"
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
        conn.execute("""INSERT INTO market_structure_stages
          (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,phase,resolved_time,sampled,formed)
          SELECT 'maintenance-fixture','candidate-'||n::text,'double_bottom','FORMING','HIGH',n,'INVALIDATED',n+1,true,false
          FROM generate_series(1,500) n""")


def fingerprint(conn):
    return conn.execute("SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase)) FROM market_structure_stages s").fetchone()


def test_diagnostic_default_keeps_oid_rows_and_constraint(migrated_database):
    prepare(migrated_database)
    with psycopg.connect(migrated_database) as conn:
        before = maintenance.valid_primary(maintenance.indexes(conn))
        facts = fingerprint(conn)
    report = maintenance.run(migrated_database)
    assert report["status"] == "DRY_RUN"
    assert report["physical_index_reclaimed_bytes"] is None
    assert report["removed_invalid_artifacts"] == []
    with psycopg.connect(migrated_database) as conn:
        assert maintenance.valid_primary(maintenance.indexes(conn)) == before
        assert fingerprint(conn) == facts


def test_single_index_concurrent_build_preserves_every_row_and_primary_constraint(migrated_database, monkeypatch):
    prepare(migrated_database)
    with psycopg.connect(migrated_database) as conn:
        before = maintenance.valid_primary(maintenance.indexes(conn))
        facts = fingerprint(conn)
        cover = conn.execute("SELECT 'public.structure_reference_covering'::regclass::oid").fetchone()[0]
    # Tiny fixtures cannot satisfy production's 128MiB savings threshold. Bypass
    # ONLY this diagnostic eligibility in this disposable test; runtime limits,
    # real concurrent DDL, PK/OID checks and cancellation logic stay unchanged.
    monkeypatch.setattr(maintenance, "density_decision", lambda stats, size: {"eligible": True})
    report = maintenance.run(migrated_database, apply=True, max_seconds=10)
    assert report["status"] == "REBUILT", report
    assert report["build_query_ended"] is True
    assert report["remaining_reindex_artifacts"] == []
    with psycopg.connect(migrated_database) as conn:
        after = maintenance.valid_primary(maintenance.indexes(conn))
        assert after["oid"] != before["oid"]
        assert after["constraints"] == before["constraints"]
        assert fingerprint(conn) == facts
        assert conn.execute("SELECT 'public.structure_reference_covering'::regclass::oid").fetchone()[0] == cover
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("""INSERT INTO market_structure_stages
              (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,phase,sampled,formed)
              VALUES('maintenance-fixture','candidate-1','double_bottom','FORMING','HIGH',1,'FORMING',true,false)""")
