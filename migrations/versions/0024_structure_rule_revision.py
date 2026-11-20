"""Admit the vetted ABC volatility-consistent model without changing old facts."""
from alembic import op

revision="0024_structure_rule_revision"
down_revision="0023_structure_lifecycle"
branch_labels=None
depends_on=None


def change(old,new):
    if op.get_bind().dialect.name!="postgresql":
        return
    definition=op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)").scalar_one()
    if old not in definition:
        raise RuntimeError("Unexpected tracking function; refusing to replace unknown definition")
    op.execute(definition.replace(old,new))


def upgrade():
    change("model_version<>'structure-study-2'","model_version NOT IN ('structure-study-2','structure-study-2.1')")


def downgrade():
    change("model_version NOT IN ('structure-study-2','structure-study-2.1')","model_version<>'structure-study-2'")
