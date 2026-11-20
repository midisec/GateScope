"""Tenant-scoped MCP clients, bearer tokens and immutable invocation audit.

Revision ID: 0006_mcp_read_plane
Revises: 0005_telegram_notifications
"""

from __future__ import annotations

from alembic import op

revision = "0006_mcp_read_plane"
down_revision = "0005_telegram_notifications"
branch_labels = None
depends_on = None

PRIVATE_TABLES = ["mcp_clients", "mcp_tokens", "mcp_audit_facts"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE mcp_clients (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, owner_user_id, name)
        );

        CREATE TABLE mcp_tokens (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          client_id uuid NOT NULL REFERENCES mcp_clients(id),
          created_by uuid NOT NULL REFERENCES users(id),
          label text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
          token_hash text NOT NULL UNIQUE CHECK (length(token_hash) = 64),
          token_hint text NOT NULL CHECK (length(token_hint) BETWEEN 8 AND 20),
          scopes text[] NOT NULL,
          expires_at timestamptz NOT NULL,
          last_used_at timestamptz,
          revoked_at timestamptz,
          created_at timestamptz NOT NULL,
          CHECK (cardinality(scopes) BETWEEN 1 AND 4),
          CHECK (scopes <@ ARRAY['mcp:connect','market:read','analysis:read','alerts:read']::text[]),
          CHECK ('mcp:connect' = ANY(scopes)),
          CHECK (expires_at > created_at)
        );
        CREATE INDEX mcp_tokens_client_idx ON mcp_tokens(tenant_id, client_id, created_at DESC);

        CREATE TABLE mcp_audit_facts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          token_id uuid NOT NULL REFERENCES mcp_tokens(id),
          client_id uuid NOT NULL REFERENCES mcp_clients(id),
          subject_user_id uuid NOT NULL REFERENCES users(id),
          tool_name text NOT NULL CHECK (length(tool_name) BETWEEN 1 AND 100),
          argument_hash text NOT NULL CHECK (length(argument_hash) = 64),
          outcome text NOT NULL CHECK (outcome IN ('SUCCESS','DENIED','ERROR')),
          safe_error_code text,
          trace_id text NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
          created_at timestamptz NOT NULL
        );
        CREATE INDEX mcp_audit_facts_lookup_idx
          ON mcp_audit_facts(tenant_id, client_id, created_at DESC);
        CREATE TRIGGER mcp_audit_facts_immutable_update
          BEFORE UPDATE ON mcp_audit_facts FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.reject_fact_mutation();
        CREATE TRIGGER mcp_audit_facts_immutable_delete
          BEFORE DELETE ON mcp_audit_facts FOR EACH ROW
          EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        CREATE TABLE gatescope_security.mcp_token_locators (
          token_hash text PRIMARY KEY CHECK (length(token_hash) = 64),
          token_id uuid NOT NULL UNIQUE REFERENCES public.mcp_tokens(id) ON DELETE CASCADE,
          tenant_id uuid NOT NULL REFERENCES public.tenants(id),
          subject_user_id uuid NOT NULL REFERENCES public.users(id)
        );
        REVOKE ALL ON gatescope_security.mcp_token_locators FROM PUBLIC;

        CREATE OR REPLACE FUNCTION gatescope_security.register_mcp_token_locator(
          requested_hash text, requested_token_id uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
        DECLARE current_tenant uuid := gatescope_security.current_tenant_id();
        DECLARE current_subject uuid := gatescope_security.current_subject_id();
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.mcp_tokens mt
            JOIN public.mcp_clients mc ON mc.id=mt.client_id AND mc.tenant_id=mt.tenant_id
            WHERE mt.id=requested_token_id AND mt.token_hash=requested_hash
              AND mt.tenant_id=current_tenant AND mt.created_by=current_subject
              AND mc.owner_user_id=current_subject AND mc.status='ACTIVE'
          ) THEN
            RAISE EXCEPTION 'MCP token is outside the current principal';
          END IF;
          INSERT INTO gatescope_security.mcp_token_locators
            (token_hash, token_id, tenant_id, subject_user_id)
          VALUES (requested_hash, requested_token_id, current_tenant, current_subject);
        END
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.remove_mcp_token_locator(
          requested_token_id uuid
        ) RETURNS void
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
          DELETE FROM gatescope_security.mcp_token_locators locator
          WHERE locator.token_id=requested_token_id
            AND locator.tenant_id=gatescope_security.current_tenant_id()
            AND locator.subject_user_id=gatescope_security.current_subject_id()
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.remove_mcp_client_locators(
          requested_client_id uuid
        ) RETURNS void
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
          DELETE FROM gatescope_security.mcp_token_locators locator
          USING public.mcp_tokens mt
          WHERE locator.token_id=mt.id AND mt.client_id=requested_client_id
            AND locator.tenant_id=gatescope_security.current_tenant_id()
            AND locator.subject_user_id=gatescope_security.current_subject_id()
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.resolve_mcp_token(requested_hash text)
        RETURNS TABLE(
          token_id uuid, tenant_id uuid, subject_user_id uuid, client_id uuid,
          scopes text[], expires_at timestamptz
        )
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public, gatescope_security AS $$
          SELECT mt.id, mt.tenant_id, locator.subject_user_id, mt.client_id,
                 mt.scopes, mt.expires_at
          FROM gatescope_security.mcp_token_locators locator
          JOIN public.mcp_tokens mt
            ON mt.id=locator.token_id AND mt.tenant_id=locator.tenant_id
          JOIN public.mcp_clients mc
            ON mc.id=mt.client_id AND mc.tenant_id=mt.tenant_id
          JOIN public.users u ON u.id=locator.subject_user_id
          JOIN public.memberships membership
            ON membership.tenant_id=mt.tenant_id
           AND membership.user_id=locator.subject_user_id
          WHERE locator.token_hash=requested_hash
            AND mt.token_hash=requested_hash
            AND mt.revoked_at IS NULL AND mt.expires_at>statement_timestamp()
            AND mc.status='ACTIVE' AND mc.owner_user_id=locator.subject_user_id
            AND u.status='ACTIVE' AND membership.status='ACTIVE'
        $$;

        REVOKE ALL ON FUNCTION gatescope_security.register_mcp_token_locator(text, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.remove_mcp_token_locator(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.remove_mcp_client_locators(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.resolve_mcp_token(text) FROM PUBLIC;
        """
    )
    for table in PRIVATE_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'''CREATE POLICY tenant_isolation ON "{table}"
                USING (tenant_id = gatescope_security.current_tenant_id())
                WITH CHECK (tenant_id = gatescope_security.current_tenant_id())'''
        )
    op.execute(
        """GRANT SELECT, INSERT, UPDATE ON mcp_clients, mcp_tokens TO gatescope_api;
           GRANT SELECT ON mcp_audit_facts TO gatescope_api;
           GRANT SELECT, UPDATE ON mcp_tokens TO gatescope_mcp;
           GRANT SELECT ON mcp_clients, alert_rules, alert_rule_versions TO gatescope_mcp;
           GRANT INSERT ON mcp_audit_facts TO gatescope_mcp;
           GRANT EXECUTE ON FUNCTION
             gatescope_security.register_mcp_token_locator(text, uuid),
             gatescope_security.remove_mcp_token_locator(uuid),
             gatescope_security.remove_mcp_client_locators(uuid)
           TO gatescope_api;
           GRANT EXECUTE ON FUNCTION gatescope_security.resolve_mcp_token(text) TO gatescope_mcp;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.resolve_mcp_token(text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.remove_mcp_client_locators(uuid)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.remove_mcp_token_locator(uuid)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.register_mcp_token_locator(text, uuid)")
    op.execute("DROP TABLE IF EXISTS gatescope_security.mcp_token_locators")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
