"""Withdraw the four performance admission floors without rewriting evidence."""
from importlib import import_module

from alembic import op
from sqlalchemy import text

revision = "0034_performance_observation"
down_revision = "0033_signal_event_consistency"
branch_labels = depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Keep old append-only certificates and their 19/23-check count constraint.
    op.execute("DROP TRIGGER certification_performance_guard ON strategy_execution_certifications")
    op.execute("DROP FUNCTION gatescope_security.guard_performance_thresholds()")
    function = "gatescope_security.guard_strategy_certification_writer()"
    definition = conn.execute(text(f"SELECT pg_get_functiondef('{function}'::regprocedure)")).scalar_one()
    removed = ",'OOS_SHARPE','OOS_DRAWDOWN','OOS_HIT_RATE','OOS_T_STAT'"
    if removed not in definition:
        raise RuntimeError("unexpected certification policy definition")
    op.execute(text(definition.replace(removed, "")))
    function = "gatescope_security.guard_testnet_drill_admission()"
    definition = conn.execute(text(f"SELECT pg_get_functiondef('{function}'::regprocedure)")).scalar_one()
    old = " AND jsonb_array_length(certification.gate_report_json)=23"
    if old not in definition:
        raise RuntimeError("unexpected drill admission definition")
    op.execute(text(definition.replace(old, " AND jsonb_array_length(certification.gate_report_json) IN (19,23)")))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    function = "gatescope_security.guard_testnet_drill_admission()"
    definition = conn.execute(text(f"SELECT pg_get_functiondef('{function}'::regprocedure)")).scalar_one()
    definition = definition.replace(" AND jsonb_array_length(certification.gate_report_json) IN (19,23)", "")
    op.execute(text(definition))
    # Restore precisely the historical policy; never delete newer evidence.
    import_module("migrations.versions.0026_performance_admission").upgrade()
