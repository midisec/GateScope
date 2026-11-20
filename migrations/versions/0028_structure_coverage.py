"""Version coverage/geometry rules without modifying any archived facts."""
from alembic import op

revision = "0028_structure_coverage"
down_revision = "0027_discovery_reports"
branch_labels = depends_on = None

OLD = "model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3')"
NEW = "model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3','structure-study-3.1')"


def change(before, after):
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)").scalar_one()
    if before not in definition:
        raise RuntimeError("Unexpected tracking function")
    op.execute(definition.replace(before, after))


def upgrade():
    change(OLD, NEW)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""CREATE INDEX pattern_event_replay_contexts ON market_pattern_events
            (instrument_id,timeframe,context_id,version,recorded_at) WHERE source_mode='REPLAY'""")


def downgrade():
    change(NEW, OLD)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX pattern_event_replay_contexts")
