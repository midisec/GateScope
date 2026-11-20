"""Read current structure candles for alerts without changing historical facts."""

from alembic import op

revision = "0033_signal_event_consistency"
down_revision = "0032_telegram_screening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON market_live_candles TO gatescope_strategy")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON market_live_candles FROM gatescope_strategy")
