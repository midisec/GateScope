"""Formal MFA, recovery and identity audit facts.

Revision ID: 0004_formal_identity
Revises: 0003_alert_facts
"""

from __future__ import annotations

from alembic import op

revision = "0004_formal_identity"
down_revision = "0003_alert_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        ALTER TABLE sessions ADD COLUMN mfa_verified_at timestamptz;
        ALTER TABLE sessions ADD COLUMN auth_strength text NOT NULL DEFAULT 'PASSWORD'
          CHECK (auth_strength IN ('PASSWORD','MFA','RECOVERY'));

        CREATE TABLE mfa_methods (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          method_type text NOT NULL CHECK (method_type IN ('TOTP')),
          label text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
          status text NOT NULL CHECK (status IN ('PENDING','ACTIVE','REVOKED')),
          encrypted_secret bytea NOT NULL,
          secret_nonce bytea NOT NULL,
          key_version text NOT NULL,
          last_accepted_step bigint,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          confirmed_at timestamptz,
          revoked_at timestamptz,
          UNIQUE (tenant_id, user_id, label)
        );
        CREATE UNIQUE INDEX mfa_one_pending_totp_idx
          ON mfa_methods(tenant_id, user_id)
          WHERE method_type = 'TOTP' AND status = 'PENDING';

        CREATE TABLE recovery_codes (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          code_hash text NOT NULL,
          generation integer NOT NULL CHECK (generation > 0),
          created_at timestamptz NOT NULL,
          expires_at timestamptz,
          used_at timestamptz,
          UNIQUE (tenant_id, user_id, code_hash)
        );
        CREATE INDEX recovery_codes_active_idx ON recovery_codes(tenant_id, user_id, generation)
          WHERE used_at IS NULL;

        CREATE TABLE password_history (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          password_hash text NOT NULL,
          created_at timestamptz NOT NULL
        );
        CREATE INDEX password_history_user_idx ON password_history(tenant_id, user_id, created_at DESC);

        CREATE TABLE identity_audit_facts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          actor_user_id uuid REFERENCES users(id),
          subject_user_id uuid REFERENCES users(id),
          action text NOT NULL,
          outcome text NOT NULL CHECK (outcome IN ('SUCCESS','DENIED','FAILED')),
          occurred_at timestamptz NOT NULL,
          trace_id text NOT NULL,
          detail_json jsonb NOT NULL,
          previous_hash text,
          fact_hash text NOT NULL,
          UNIQUE (tenant_id, fact_hash)
        );
        CREATE INDEX identity_audit_time_idx ON identity_audit_facts(tenant_id, occurred_at DESC, id DESC);
        """
    )
    for table in ["mfa_methods", "recovery_codes", "password_history", "identity_audit_facts"]:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (tenant_id = gatescope_security.current_tenant_id())
                WITH CHECK (tenant_id = gatescope_security.current_tenant_id())'''
        )
    op.execute(
        """
        CREATE TRIGGER identity_audit_facts_immutable
          BEFORE UPDATE OR DELETE ON identity_audit_facts
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
        GRANT SELECT, INSERT, UPDATE ON mfa_methods, recovery_codes TO gatescope_auth;
        GRANT SELECT, INSERT ON password_history, identity_audit_facts TO gatescope_auth;
        GRANT SELECT ON identity_audit_facts TO gatescope_api;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in ["identity_audit_facts", "password_history", "recovery_codes", "mfa_methods"]:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS auth_strength")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS mfa_verified_at")
