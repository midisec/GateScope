"""Grant the notification worker the data plane privileges it consumes.

Revision ID: 0016_notification_worker_runtime_grants
Revises: 0015_telegram_webhook_inbox_grant
"""

from __future__ import annotations

from alembic import op

revision = "0016_notification_worker_runtime_grants"
down_revision = "0015_telegram_webhook_inbox_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("GRANT SELECT, UPDATE ON operations_current TO gatescope_notification")
    op.execute(
        "GRANT SELECT ON alert_rule_versions, instruments TO gatescope_notification"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("REVOKE SELECT, UPDATE ON operations_current FROM gatescope_notification")
    op.execute(
        "REVOKE SELECT ON alert_rule_versions, instruments FROM gatescope_notification"
    )
