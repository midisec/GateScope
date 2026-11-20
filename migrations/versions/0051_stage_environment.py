"""Nullable onset-only structure environment provenance; no history rescan."""
from alembic import op

revision = "0051_stage_environment"
down_revision = "0050_shared_capital_portfolio"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # Nullable/no default is a metadata-only column addition. NOT VALID avoids
    # scanning the large legacy table while still checking every NEW write.
    op.execute("ALTER TABLE market_structure_stages ADD COLUMN environment_key text")
    op.execute("""ALTER TABLE market_structure_stages ADD CONSTRAINT market_structure_stage_environment_key_valid
      CHECK(environment_key IS NULL OR environment_key ~ '^se1:(UP|DOWN|RANGE):(HIGH|NORMAL|LOW):(HIGH|NORMAL|LOW)$') NOT VALID""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # The column is a rebuildable projection; immutable onset journals retain
    # the original label. Downgrade does not erase or reinterpret those facts.
    op.execute("ALTER TABLE market_structure_stages DROP COLUMN environment_key")
