"""Grant the API webhook its least-privilege inbox access.

Revision ID: 0015_telegram_webhook_inbox_grant
Revises: 0014_telegram_member_self_service
"""

from __future__ import annotations

from alembic import op

revision = "0015_telegram_webhook_inbox_grant"
down_revision = "0014_telegram_member_self_service"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("GRANT SELECT, INSERT, UPDATE ON inbox_events TO gatescope_api")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("REVOKE SELECT, INSERT, UPDATE ON inbox_events FROM gatescope_api")
