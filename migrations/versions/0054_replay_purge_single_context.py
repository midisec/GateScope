"""Reduce maintenance manifests to one fully validated REPLAY context.

Only a bounded maintenance function's discovery budget changes. Existing jobs,
binding scans, reference guards, privilege restrictions and immutable rows are
untouched. No new index, data scan or deletion runs during this migration.
"""
from alembic import op

revision = "0054_replay_purge_single_context"
down_revision = "0053_unwatched_replay_purge"
branch_labels = depends_on = None

BEFORE = "preview=gatescope_security.inspect_replay_purge(target,32);"
AFTER = "preview=gatescope_security.inspect_replay_purge(target,1);"


def _replace(before, after):
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='3s'")
    definition = op.get_bind().exec_driver_sql(
        "SELECT pg_get_functiondef('gatescope_security.open_replay_purge(uuid,uuid)'::regprocedure)"
    ).scalar_one()
    if definition.count(before) != 1 or after in definition:
        raise RuntimeError("UNEXPECTED_REPLAY_PURGE_MANIFEST_FUNCTION")
    op.execute(definition.replace(before, after, 1))


def upgrade():
    _replace(BEFORE, AFTER)


def downgrade():
    _replace(AFTER, BEFORE)
