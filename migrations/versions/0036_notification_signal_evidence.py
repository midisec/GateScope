"""Allow the notification worker to resolve owner-bound immutable signals."""
from alembic import op

revision = "0036_signal_notice"
down_revision = "0035_execution_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON alert_event_facts, alert_rules TO gatescope_notification")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON alert_event_facts, alert_rules FROM gatescope_notification")
