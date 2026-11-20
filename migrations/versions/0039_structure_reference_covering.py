"""Cover original-stage reference reads without changing their probability SQL.

Concurrent DDL deliberately runs outside a transaction. A cancelled build can
leave an INVALID index; retry removes only this migration's exact index shape
before rebuilding. A valid matching index is retained after an interrupted
Alembic version update. Never run multiple migration processes concurrently.
"""

from alembic import op


revision = "0039_structure_reference_cover"
down_revision = "0038_shadow_ledger"
branch_labels = None
depends_on = None

INDEX_NAME = "structure_reference_covering"
INDEX_DEFINITION = (
    "CREATE INDEX structure_reference_covering ON public.market_structure_stages USING btree "
    "(context_id, pattern, entry_phase, score_bucket, stage_time) "
    "INCLUDE (candidate_id, phase, resolved_time, sampled, formed)"
)
CREATE_INDEX = INDEX_DEFINITION.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)
DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.structure_reference_covering"


def _index_state(connection):
    row = connection.exec_driver_sql("""SELECT c.relkind,
        i.indrelid='public.market_structure_stages'::regclass,
        pg_get_indexdef(c.oid),i.indisvalid,i.indisready,i.indislive
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_index i ON i.indexrelid=c.oid
        WHERE n.nspname='public' AND c.relname='structure_reference_covering'""").fetchone()
    if row is not None and (
        row[0] != "i" or row[1] is not True
        or " ".join((row[2] or "").split()) != INDEX_DEFINITION
    ):
        raise RuntimeError("Unexpected object for structure_reference_covering; no object was removed")
    return row


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        existing = _index_state(connection)
        if existing is not None:
            if all(existing[3:6]):
                return
            op.execute(DROP_INDEX)
        op.execute(CREATE_INDEX)
        created = _index_state(connection)
        if created is None or not all(created[3:6]):
            raise RuntimeError("structure_reference_covering did not become valid; retry the migration")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        if _index_state(op.get_bind()) is not None:
            op.execute(DROP_INDEX)
