"""Persist route attribution and semantic dedupe keys for notification policy.

Revision ID: 0007_notification_route_policy
Revises: 0006_mcp_read_plane
"""

from __future__ import annotations

from alembic import op

revision = "0007_notification_route_policy"
down_revision = "0006_mcp_read_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE notification_deliveries
          ADD COLUMN route_id uuid REFERENCES notification_routes(id),
          ADD COLUMN dedupe_key text CHECK (dedupe_key IS NULL OR length(dedupe_key)=64);
        CREATE INDEX notification_deliveries_route_window_idx
          ON notification_deliveries(tenant_id, route_id, created_at DESC)
          WHERE route_id IS NOT NULL;
        CREATE INDEX notification_deliveries_dedupe_idx
          ON notification_deliveries(tenant_id, route_id, dedupe_key, created_at DESC)
          WHERE route_id IS NOT NULL AND dedupe_key IS NOT NULL;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS notification_deliveries_dedupe_idx")
    op.execute("DROP INDEX IF EXISTS notification_deliveries_route_window_idx")
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS dedupe_key")
    op.execute("ALTER TABLE notification_deliveries DROP COLUMN IF EXISTS route_id")
