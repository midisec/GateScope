"""Telegram may read public research projections; no new write authority."""
from alembic import op

revision = '0032_telegram_screening'
down_revision = '0031_structure_confidence'
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('GRANT SELECT ON market_pattern_studies,market_structure_returns TO gatescope_notification')


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('REVOKE SELECT ON market_pattern_studies,market_structure_returns FROM gatescope_notification')
