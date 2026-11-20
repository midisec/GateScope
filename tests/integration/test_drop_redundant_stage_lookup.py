"""Real concurrent DDL tests use only the disposable gatescope_test database."""
from importlib import import_module
from pathlib import Path
from threading import Thread
import time

from alembic import command
import psycopg

from gatescope.adapters.annual_structure_study import observed_reference
from tests.integration.conftest import migration_config

migration = import_module("migrations.versions.0055_drop_redundant_stage_lookup")


def config(database_url):
    cfg = migration_config(Path(__file__).resolve().parents[2], database_url)
    cfg.attributes["database_url_is_explicit"] = True
    return cfg


def index_state(conn, name):
    return conn.execute("""SELECT c.oid,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(c.oid)
      FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relname=%s""", (name,)).fetchone()


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def test_drop_preserves_all_rows_reference_output_permissions_and_valid_cover(migrated_database):
    cfg = config(migrated_database)
    # Explicit legacy precondition: 0055's downgrade recreates the prior index.
    command.downgrade(cfg, migration.down_revision)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        assert index_state(conn, migration.OLD_NAME)[1:4] == (True, True, True)
        cover = index_state(conn, migration.COVER_NAME)
        conn.execute("""INSERT INTO market_structure_stages
          (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,
           phase,resolved_time,sampled,formed,prediction,forecast_json,outcome_json)
          SELECT 'test-context-'||(n%10)::text,'candidate-'||n::text,'double_bottom','FORMED','LOW',n*2,
            CASE WHEN n%3=0 THEN 'CONFIRMED' WHEN n%3=1 THEN 'INVALIDATED' ELSE 'EXPIRED' END,
            n*2+1,true,true,.6,'{"frozen":true}','{"sample":"kept"}'
          FROM generate_series(1,5000) n""")
        conn.execute("VACUUM (ANALYZE) public.market_structure_stages")
        before = conn.execute("SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase)) FROM market_structure_stages s").fetchone()
        permissions = conn.execute("""SELECT has_table_privilege('gatescope_api','market_structure_stages','SELECT'),
          has_table_privilege('gatescope_api','market_structure_stages','UPDATE'),
          has_table_privilege('gatescope_strategy','market_structure_stages','UPDATE')""").fetchone()
        event = {"id": "current", "pattern": "double_bottom", "phase": "FORMED", "confidence": .6}
        expected = observed_reference(conn, "test-context-0", "test-context-3", event, 9000)
    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database, autocommit=True) as conn:
        assert index_state(conn, migration.OLD_NAME) is None
        assert index_state(conn, migration.COVER_NAME) == cover
        assert conn.execute("SELECT count(*),md5(string_agg(row_to_json(s)::text,'' ORDER BY context_id,candidate_id,entry_phase)) FROM market_structure_stages s").fetchone() == before
        assert observed_reference(conn, "test-context-0", "test-context-3", event, 9000) == expected
        assert conn.execute("""SELECT has_table_privilege('gatescope_api','market_structure_stages','SELECT'),
          has_table_privilege('gatescope_api','market_structure_stages','UPDATE'),
          has_table_privilege('gatescope_strategy','market_structure_stages','UPDATE')""").fetchone() == permissions
        plan = conn.execute("""EXPLAIN (FORMAT JSON) SELECT candidate_id,phase,resolved_time,sampled,formed
          FROM market_structure_stages WHERE context_id='test-context-0' AND pattern='double_bottom'
          AND entry_phase='FORMED' AND score_bucket='LOW' AND stage_time>=0 AND stage_time<9000""").fetchone()[0][0]["Plan"]
        assert any(node.get("Index Name") == migration.COVER_NAME for node in nodes(plan))
    # Completed concurrent DDL + interrupted Alembic version update is retry-safe.
    command.stamp(cfg, migration.down_revision)
    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database) as conn:
        assert index_state(conn, migration.COVER_NAME) == cover
    command.downgrade(cfg, migration.down_revision)
    with psycopg.connect(migrated_database) as conn:
        assert index_state(conn, migration.OLD_NAME)[1:4] == (True, True, True)
        assert index_state(conn, migration.COVER_NAME) == cover


def test_cancelled_concurrent_drop_is_finished_without_touching_valid_cover(migrated_database):
    cfg = config(migrated_database)
    command.downgrade(cfg, migration.down_revision)
    errors = []
    with psycopg.connect(migrated_database, autocommit=True) as admin:
        cover = index_state(admin, migration.COVER_NAME)
        with psycopg.connect(migrated_database) as reader, psycopg.connect(migrated_database, autocommit=True) as dropper:
            # Hold only a real read transaction. Concurrent DROP must not obtain
            # ACCESS EXCLUSIVE or require us to falsify pg_index state.
            reader.execute("SELECT context_id FROM market_structure_stages LIMIT 1")
            backend = dropper.info.backend_pid

            def drop():
                try:
                    dropper.execute(migration.DROP_INDEX)
                except psycopg.Error as error:
                    errors.append(error)

            thread = Thread(target=drop)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                invalid = None
                while time.monotonic() < deadline:
                    invalid = index_state(admin, migration.OLD_NAME)
                    if invalid is not None and invalid[1] is False:
                        break
                    time.sleep(.02)
                assert invalid is not None and invalid[1] is False
                assert admin.execute("SELECT pg_cancel_backend(%s)", (backend,)).fetchone()[0]
            finally:
                reader.rollback()
                if thread.is_alive():
                    admin.execute("SELECT pg_cancel_backend(%s)", (backend,))
                thread.join(timeout=10)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], psycopg.errors.QueryCanceled)
    command.upgrade(cfg, migration.revision)
    with psycopg.connect(migrated_database) as conn:
        assert index_state(conn, migration.OLD_NAME) is None
        assert index_state(conn, migration.COVER_NAME) == cover
