"""Phase 0 temporary single-user authentication and append-only audit.

Revision ID: 0001_phase0_security
Revises: None
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_phase0_security"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # Alembic creates this bookkeeping column as VARCHAR(32) by default,
        # while the descriptive revision identifiers used from 0009 onward are
        # intentionally longer.  Widen it before those revisions are stamped.
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(128)")
        return
    if dialect != "sqlite":
        return
    op.create_table(
        "auth_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64, collation="NOCASE"), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.Integer(), nullable=True),
        sa.Column("last_login_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("role IN ('OWNER')", name="ck_auth_users_role"),
    )
    op.create_index("ix_auth_users_username", "auth_users", ["username"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("authenticated_at", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.Integer(), nullable=True),
    )
    op.create_index("ix_auth_sessions_token", "auth_sessions", ["token_hash"], unique=True)
    op.create_index("ix_auth_sessions_user_active", "auth_sessions", ["user_id", "revoked_at", "expires_at"])

    op.create_table(
        "security_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_security_audit_time", "security_audit_events", ["occurred_at"])
    op.execute(
        """
        CREATE TRIGGER security_audit_no_update
        BEFORE UPDATE ON security_audit_events
        BEGIN
          SELECT RAISE(ABORT, 'security audit is append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER security_audit_no_delete
        BEFORE DELETE ON security_audit_events
        BEGIN
          SELECT RAISE(ABORT, 'security audit is append-only');
        END
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("DROP TRIGGER IF EXISTS security_audit_no_delete")
    op.execute("DROP TRIGGER IF EXISTS security_audit_no_update")
    op.drop_index("ix_security_audit_time", table_name="security_audit_events")
    op.drop_table("security_audit_events")
    op.drop_index("ix_auth_sessions_user_active", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_auth_users_username", table_name="auth_users")
    op.drop_table("auth_users")
