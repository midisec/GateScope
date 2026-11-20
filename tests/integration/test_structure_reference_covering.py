"""Concurrent index lifecycle and unchanged real reference-query semantics."""

from importlib import import_module
from pathlib import Path
from threading import Thread
import time

import psycopg
from alembic import command

from gatescope.adapters.annual_structure_study import observed_reference
from tests.integration.conftest import migration_config


migration = import_module("migrations.versions.0039_structure_reference_covering")


def config(database_url):
    result = migration_config(Path(__file__).resolve().parents[2], database_url)
    result.attributes["database_url_is_explicit"] = True
    return result


def index_state(conn):
    return conn.execute("""SELECT c.oid,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(c.oid)
        FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname='structure_reference_covering'""").fetchone()


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def test_covering_index_upgrade_plan_semantics_and_downgrade_preserve_facts(migrated_database):
    cfg = config(migrated_database)
    command.downgrade(cfg, migration.down_revision)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        assert index_state(conn) is None
        conn.execute("""INSERT INTO market_structure_stages
            (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,
             phase,resolved_time,sampled,formed,prediction,forecast_json,outcome_json)
            SELECT 'context-'||(n%30)::text,'candidate-'||n::text,
                CASE WHEN n%3=0 THEN 'double_top' ELSE 'double_bottom' END,
                'FORMED','LOW',n*2,
                CASE WHEN n%4=0 THEN 'CONFIRMED' WHEN n%4=1 THEN 'EXPIRED' ELSE 'INVALIDATED' END,
                n*2+1,true,true,0.6,
                jsonb_build_object('geometry',repeat(md5(n::text),8)),
                jsonb_build_object('outcome',repeat(md5((n+1)::text),8))
            FROM generate_series(1,30000) n""")
        conn.execute("VACUUM (ANALYZE) public.market_structure_stages")
        old_index = conn.execute("SELECT 'public.structure_reference_lookup'::regclass::oid").fetchone()[0]
        permissions = conn.execute("""SELECT has_table_privilege('gatescope_api','market_structure_stages','SELECT'),
            has_table_privilege('gatescope_api','market_structure_stages','UPDATE'),
            has_table_privilege('gatescope_strategy','market_structure_stages','UPDATE')""").fetchone()
        frozen = conn.execute("""SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase))
            FROM market_structure_stages s""").fetchone()
        queries = []

        class Capture:
            def execute(self, query, args):
                queries.append((query, args))
                return conn.execute(query, args)

        event = {"id": "current", "pattern": "double_top", "phase": "FORMED", "confidence": .6}
        before = observed_reference(Capture(), "context-0", "context-3", event, 40000)
        assert before["sample_count"] > 0
        query, args = queries[-1]
        before_plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, args).fetchone()[0][0]["Plan"]
        assert not any(n.get("Index Name") == migration.INDEX_NAME for n in nodes(before_plan))

    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        created = index_state(conn)
        assert created is not None and created[1:4] == (True, True, True)
        assert created[4] == migration.INDEX_DEFINITION
        after = observed_reference(conn, "context-0", "context-3", event, 40000)
        assert after == before
        after_plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, args).fetchone()[0][0]["Plan"]
        covering = [n for n in nodes(after_plan) if n.get("Index Name") == migration.INDEX_NAME]
        assert covering and all(n["Node Type"] == "Index Only Scan" for n in covering)
        assert sum(n["Heap Fetches"] for n in covering) == 0
        assert conn.execute("SELECT 'public.structure_reference_lookup'::regclass::oid").fetchone()[0] == old_index
        assert conn.execute("""SELECT has_table_privilege('gatescope_api','market_structure_stages','SELECT'),
            has_table_privilege('gatescope_api','market_structure_stages','UPDATE'),
            has_table_privilege('gatescope_strategy','market_structure_stages','UPDATE')""").fetchone() == permissions
        assert conn.execute("""SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase))
            FROM market_structure_stages s""").fetchone() == frozen
    # Simulate an interruption after successful concurrent DDL but before the
    # Alembic version update: rerunning must retain the same valid index OID.
    command.stamp(cfg, migration.down_revision)
    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database) as conn:
        assert index_state(conn)[0] == created[0]
    command.downgrade(cfg, migration.down_revision)
    with psycopg.connect(migrated_database) as conn:
        assert index_state(conn) is None
        assert conn.execute("SELECT 'public.structure_reference_lookup'::regclass::oid").fetchone()[0] == old_index
        assert conn.execute("""SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase))
            FROM market_structure_stages s""").fetchone() == frozen
        assert observed_reference(conn, "context-0", "context-3", event, 40000) == before


def test_retry_rebuilds_index_left_invalid_by_cancelled_concurrent_build(migrated_database):
    cfg = config(migrated_database)
    command.downgrade(cfg, migration.down_revision)
    errors = []
    with psycopg.connect(migrated_database, autocommit=True) as admin:
        with psycopg.connect(migrated_database) as blocker, psycopg.connect(migrated_database, autocommit=True) as builder:
            # A live writer lets CREATE INDEX CONCURRENTLY commit its INVALID
            # catalog entry, then wait before scanning. Cancel the real build;
            # never manufacture state by writing PostgreSQL system catalogs.
            blocker.execute("UPDATE market_structure_stages SET formed=formed")
            backend = builder.info.backend_pid

            def build():
                try:
                    builder.execute(migration.CREATE_INDEX)
                except psycopg.Error as exc:
                    errors.append(exc)

            thread = Thread(target=build)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                incomplete = None
                while time.monotonic() < deadline:
                    incomplete = index_state(admin)
                    if incomplete is not None:
                        break
                    time.sleep(.01)
                assert incomplete is not None and incomplete[1] is False
                assert admin.execute("SELECT pg_cancel_backend(%s)", (backend,)).fetchone()[0]
            finally:
                blocker.rollback()
                if thread.is_alive():
                    admin.execute("SELECT pg_cancel_backend(%s)", (backend,))
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], psycopg.errors.QueryCanceled)
        failed = index_state(admin)
        assert failed is not None and failed[1] is False
    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database) as conn:
        recovered = index_state(conn)
        assert recovered is not None and recovered[1:4] == (True, True, True)
        assert recovered[0] != failed[0]
        assert recovered[4] == migration.INDEX_DEFINITION
        assert conn.execute("SELECT 'public.structure_reference_lookup'::regclass IS NOT NULL").fetchone()[0]
