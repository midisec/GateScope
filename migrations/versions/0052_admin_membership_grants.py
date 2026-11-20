"""Enable tenant-bound identity membership writes, without expanding API roles."""
from alembic import op

revision = "0052_admin_membership_grants"
down_revision = "0051_stage_environment"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # The existing FORCE RLS policy still enforces app.tenant_id. Only the
    # identity workload gets INSERT/UPDATE; no DELETE or user/tenant escalation.
    op.execute("GRANT INSERT, UPDATE ON memberships TO gatescope_auth")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("REVOKE INSERT, UPDATE ON memberships FROM gatescope_auth")
