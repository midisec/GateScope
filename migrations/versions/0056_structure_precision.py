"""Allow current tracking requests for structure-study-3.3."""
from alembic import op

revision = "0056_structure_precision"
down_revision = "0055_drop_redundant_stage_lookup"
branch_labels = depends_on = None

OLD = "model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3','structure-study-3.1','structure-study-3.2')"
NEW = OLD[:-1] + ",'structure-study-3.3')"


def _replace(before, after):
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        definition = conn.exec_driver_sql(
            "SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)"
        ).scalar_one()
        if definition.count(before) != 1:
            raise RuntimeError("Unexpected tracking function version guard")
        conn.exec_driver_sql(definition.replace(before, after))


def upgrade():
    _replace(OLD, NEW)


def downgrade():
    _replace(NEW, OLD)
