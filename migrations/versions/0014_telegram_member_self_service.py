"""Allow authenticated workspace members to self-bind Telegram.

Revision ID: 0014_telegram_member_self_service
Revises: 0013_watchlist_multitimeframe
"""

from __future__ import annotations

from alembic import op

revision = "0014_telegram_member_self_service"
down_revision = "0013_watchlist_multitimeframe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE telegram_bot_configs
          DROP CONSTRAINT IF EXISTS telegram_bot_configs_allowed_user_ids_check;
        ALTER TABLE telegram_bot_configs
          ADD CONSTRAINT telegram_bot_configs_allowed_user_ids_check
          CHECK (cardinality(allowed_user_ids) BETWEEN 0 AND 100);
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        UPDATE telegram_bot_configs
           SET allowed_user_ids=ARRAY[1]::bigint[]
         WHERE cardinality(allowed_user_ids)=0;
        ALTER TABLE telegram_bot_configs
          DROP CONSTRAINT IF EXISTS telegram_bot_configs_allowed_user_ids_check;
        ALTER TABLE telegram_bot_configs
          ADD CONSTRAINT telegram_bot_configs_allowed_user_ids_check
          CHECK (cardinality(allowed_user_ids) BETWEEN 1 AND 100);
        """
    )
