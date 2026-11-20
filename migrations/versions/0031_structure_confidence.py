"""Admit geometry 3.2; preserve every earlier model's immutable evidence."""
from alembic import op

revision="0031_structure_confidence"
down_revision="0030_telegram_market_queries"
branch_labels=depends_on=None

OLD="model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3','structure-study-3.1')"
NEW="model_version NOT IN ('structure-study-2','structure-study-2.1','structure-study-3','structure-study-3.1','structure-study-3.2')"


def change(before,after):
    if op.get_bind().dialect.name!="postgresql":return
    definition=op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('gatescope_security.request_pattern_tracking(uuid,text,text)'::regprocedure)").scalar_one()
    if before not in definition:raise RuntimeError("Unexpected tracking function")
    op.execute(definition.replace(before,after))


def upgrade():change(OLD,NEW)


def downgrade():change(NEW,OLD)
