"""Drop only the legacy stage lookup covered by the valid newer index.

Concurrent drop needs no replacement copy and never deletes business rows.
Run only one migration process. If cancelled, the old index may remain INVALID;
rerun this guarded migration to finish removal, never change pg_index manually.
Downgrade rebuilds the old index and needs index-size plus sorting/WAL headroom;
it is not an emergency low-disk rollback. Prefer forward fixes in production.
"""
from contextlib import contextmanager

from alembic import op

revision = "0055_drop_redundant_stage_lookup"
down_revision = "0054_replay_purge_single_context"
branch_labels = depends_on = None

OLD_NAME = "structure_reference_lookup"
COVER_NAME = "structure_reference_covering"
OLD_DEFINITION = (
    "CREATE INDEX structure_reference_lookup ON public.market_structure_stages USING btree "
    "(context_id, pattern, entry_phase, score_bucket, stage_time)"
)
COVER_DEFINITION = (
    "CREATE INDEX structure_reference_covering ON public.market_structure_stages USING btree "
    "(context_id, pattern, entry_phase, score_bucket, stage_time) "
    "INCLUDE (candidate_id, phase, resolved_time, sampled, formed)"
)
DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.structure_reference_lookup"
CREATE_INDEX = OLD_DEFINITION.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)


def _index_state(connection, name):
    if name not in (OLD_NAME, COVER_NAME):
        raise ValueError("UNEXPECTED_STAGE_INDEX_NAME")
    row = connection.exec_driver_sql("""SELECT jsonb_build_object(
        'oid',c.oid,'kind',c.relkind,'same_table',i.indrelid='public.market_structure_stages'::regclass,
        'definition',pg_get_indexdef(c.oid),'valid',i.indisvalid,'ready',i.indisready,'live',i.indislive,
        'unique',i.indisunique,'primary',i.indisprimary,'exclusion',i.indisexclusion,
        'replica_identity',i.indisreplident,'clustered',i.indisclustered,
        'key_attributes',(i.indkey::smallint[])[0:i.indnkeyatts-1],
        'opclasses',i.indclass::oid[],'collations',i.indcollation::oid[],'options',i.indoption::smallint[],
        'key_count',i.indnkeyatts,'attribute_count',i.indnatts,
        'predicate',pg_get_expr(i.indpred,i.indrelid),'expressions',pg_get_expr(i.indexprs,i.indrelid),
        'constraint_dependency',EXISTS(SELECT 1 FROM pg_constraint p WHERE p.conindid=c.oid),
        'incoming_dependency',EXISTS(SELECT 1 FROM pg_depend d
          WHERE d.refclassid='pg_class'::regclass AND d.refobjid=c.oid),
        'extension_dependency',EXISTS(SELECT 1 FROM pg_depend d
          WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype IN ('e','x')))
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_index i ON i.indexrelid=c.oid
      WHERE n.nspname='public' AND c.relname=%s""", (name,)).fetchone()
    return row[0] if row else None


def _validate(state, *, covering=False, require_valid=True):
    expected = COVER_DEFINITION if covering else OLD_DEFINITION
    if state is None:
        raise RuntimeError("REQUIRED_STAGE_INDEX_MISSING")
    if (state["kind"] != "i" or state["same_table"] is not True
        or " ".join((state["definition"] or "").split()) != expected
        or state["key_count"] != 5 or state["attribute_count"] != (10 if covering else 5)
        or state["predicate"] is not None or state["expressions"] is not None
        or any(state[name] is not False for name in ("unique", "primary", "exclusion", "replica_identity",
                "clustered", "constraint_dependency", "incoming_dependency", "extension_dependency"))):
        raise RuntimeError("UNEXPECTED_STAGE_INDEX_SHAPE_OR_DEPENDENCY")
    if require_valid and not all(state[name] is True for name in ("valid", "ready", "live")):
        raise RuntimeError("COVERING_STAGE_INDEX_NOT_USABLE" if covering else "LEGACY_STAGE_INDEX_NOT_USABLE")


def _guard_pair(cover, old):
    _validate(cover, covering=True)
    if old is None:
        return
    # An interrupted DROP may already have made the exact old index invalid.
    # It may only be finished while the independent covering index stays valid.
    _validate(old, require_valid=False)
    if any(old[key] != cover[key] for key in ("key_attributes", "opclasses", "collations", "options")):
        raise RuntimeError("STAGE_INDEX_KEYS_NOT_IDENTICAL")


def _drop(connection):
    try:
        op.execute(DROP_INDEX)
    except Exception as error:
        try:
            leftover = _index_state(connection, OLD_NAME)
            state = "ABSENT" if leftover is None else ":".join(
                str(leftover[key]) for key in ("valid", "ready", "live")
            )
        except Exception:
            state = "UNKNOWN"
        raise RuntimeError("STAGE_INDEX_DROP_INTERRUPTED_VALID_READY_LIVE=" + state
                           + "; RERUN_GUARDED_MIGRATION_NO_CATALOG_EDITS") from error


@contextmanager
def _timeouts(connection, *, building=False):
    previous = connection.exec_driver_sql(
        "SELECT current_setting('lock_timeout'),current_setting('statement_timeout')"
    ).fetchone()
    try:
        op.execute("SET lock_timeout='1s'")
        op.execute("SET statement_timeout='60s'" if building else "SET statement_timeout='30s'")
        yield
    finally:
        connection.exec_driver_sql(
            "SELECT set_config('lock_timeout',%s,false),set_config('statement_timeout',%s,false)",
            tuple(previous),
        )


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        with _timeouts(connection):
            cover = _index_state(connection, COVER_NAME)
            old = _index_state(connection, OLD_NAME)
            _guard_pair(cover, old)
            if old is not None:
                _drop(connection)
            if _index_state(connection, OLD_NAME) is not None:
                raise RuntimeError("REDUNDANT_STAGE_INDEX_STILL_PRESENT_RETRY_GUARDED_DROP")
            _validate(_index_state(connection, COVER_NAME), covering=True)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        with _timeouts(connection, building=True):
            cover = _index_state(connection, COVER_NAME)
            old = _index_state(connection, OLD_NAME)
            _guard_pair(cover, old)
            if old is not None and all(old[key] is True for key in ("valid", "ready", "live")):
                return
            if old is not None:
                _drop(connection)
            op.execute(CREATE_INDEX)
            created = _index_state(connection, OLD_NAME)
            _guard_pair(_index_state(connection, COVER_NAME), created)
            _validate(created)
