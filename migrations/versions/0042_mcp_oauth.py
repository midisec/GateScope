"""ChatGPT OAuth authorization grants and hash-only rotating credentials.

Revision ID: 0042_mcp_oauth
Revises: 0041_coinglass_scheduling
"""

from alembic import op

revision = "0042_mcp_oauth"
down_revision = "0041_coinglass_scheduling"
branch_labels = None
depends_on = None

PRIVATE_TABLES = ["oauth_grants", "oauth_codes", "oauth_refresh_tokens", "oauth_access_tokens"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
      CREATE TABLE gatescope_security.oauth_registered_clients (
        client_id text PRIMARY KEY CHECK (length(client_id) BETWEEN 20 AND 100),
        client_name text NOT NULL CHECK (length(client_name) BETWEEN 1 AND 120),
        redirect_uris text[] NOT NULL CHECK (cardinality(redirect_uris) BETWEEN 1 AND 5),
        auth_method text NOT NULL CHECK (auth_method IN ('none','client_secret_post','client_secret_basic')),
        grant_types text[] NOT NULL CHECK (grant_types <@ ARRAY['authorization_code','refresh_token']::text[]),
        scopes text[] NOT NULL CHECK (scopes <@ ARRAY['mcp:connect','market:read','analysis:read']::text[]),
        secret_hash text CHECK (length(secret_hash)=64),
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK ('authorization_code'=ANY(grant_types)), CHECK ('mcp:connect'=ANY(scopes)),
        CHECK ((auth_method='none') = (secret_hash IS NULL))
      );
      CREATE TABLE gatescope_security.oauth_pending_requests (
        handle_hash text PRIMARY KEY CHECK (length(handle_hash)=64),
        nonce_hash text NOT NULL CHECK (length(nonce_hash)=64),
        client_id text NOT NULL REFERENCES gatescope_security.oauth_registered_clients(client_id),
        redirect_uri text NOT NULL, state text NOT NULL CHECK (length(state) BETWEEN 1 AND 2048),
        resource text NOT NULL, challenge text NOT NULL CHECK (length(challenge)=43),
        scopes text[] NOT NULL, expires_at timestamptz NOT NULL,
        tenant_id uuid REFERENCES public.tenants(id), user_id uuid REFERENCES public.users(id),
        consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX oauth_pending_expiry_idx ON gatescope_security.oauth_pending_requests(expires_at);
      CREATE INDEX oauth_pending_client_idx ON gatescope_security.oauth_pending_requests(client_id);
      REVOKE ALL ON gatescope_security.oauth_registered_clients,
        gatescope_security.oauth_pending_requests FROM PUBLIC;
      GRANT SELECT,INSERT ON gatescope_security.oauth_registered_clients TO gatescope_api;
      GRANT SELECT,INSERT,UPDATE,DELETE ON gatescope_security.oauth_pending_requests TO gatescope_api;

      CREATE TABLE oauth_grants (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id),
        mcp_client_id uuid NOT NULL UNIQUE REFERENCES mcp_clients(id),
        oauth_client_id text NOT NULL REFERENCES gatescope_security.oauth_registered_clients(client_id),
        issuer text NOT NULL, resource text NOT NULL, scopes text[] NOT NULL,
        expires_at timestamptz NOT NULL, revoked_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (scopes <@ ARRAY['mcp:connect','market:read','analysis:read']::text[]),
        CHECK ('mcp:connect'=ANY(scopes)), CHECK (expires_at>created_at)
      );
      CREATE INDEX oauth_grants_owner_idx ON oauth_grants(tenant_id,user_id,created_at DESC);
      CREATE INDEX oauth_grants_expiry_idx ON oauth_grants(expires_at);
      CREATE INDEX oauth_grants_registered_client_idx ON oauth_grants(oauth_client_id);
      CREATE TABLE oauth_codes (
        code_hash text PRIMARY KEY CHECK (length(code_hash)=64),
        tenant_id uuid NOT NULL REFERENCES tenants(id), grant_id uuid NOT NULL REFERENCES oauth_grants(id),
        redirect_uri text NOT NULL, challenge text NOT NULL CHECK (length(challenge)=43),
        expires_at timestamptz NOT NULL, used_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE TABLE oauth_refresh_tokens (
        token_hash text PRIMARY KEY CHECK (length(token_hash)=64),
        tenant_id uuid NOT NULL REFERENCES tenants(id), grant_id uuid NOT NULL REFERENCES oauth_grants(id),
        scopes text[] NOT NULL, expires_at timestamptz NOT NULL, used_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX oauth_codes_grant_idx ON oauth_codes(grant_id);
      CREATE INDEX oauth_refresh_grant_idx ON oauth_refresh_tokens(grant_id);
      CREATE TABLE oauth_access_tokens (
        token_id uuid PRIMARY KEY REFERENCES mcp_tokens(id),
        tenant_id uuid NOT NULL REFERENCES tenants(id), grant_id uuid NOT NULL REFERENCES oauth_grants(id)
      );
      CREATE INDEX oauth_access_grant_idx ON oauth_access_tokens(grant_id);

      CREATE FUNCTION gatescope_security.cleanup_oauth_ephemeral_credentials()
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER
      SET search_path = pg_catalog, public, gatescope_security AS $$
      BEGIN
        DELETE FROM gatescope_security.oauth_pending_requests WHERE handle_hash IN (
          SELECT handle_hash FROM gatescope_security.oauth_pending_requests
          WHERE expires_at<statement_timestamp() ORDER BY expires_at LIMIT 500);
        DELETE FROM gatescope_security.oauth_registered_clients WHERE client_id IN (
          SELECT c.client_id FROM gatescope_security.oauth_registered_clients c
          WHERE c.created_at<statement_timestamp()-interval '30 days'
            AND NOT EXISTS(SELECT 1 FROM public.oauth_grants g WHERE g.oauth_client_id=c.client_id)
            AND NOT EXISTS(SELECT 1 FROM gatescope_security.oauth_pending_requests p WHERE p.client_id=c.client_id)
          ORDER BY c.created_at LIMIT 500);
        DELETE FROM public.oauth_codes WHERE code_hash IN (
          SELECT c.code_hash FROM public.oauth_codes c JOIN public.oauth_grants g ON g.id=c.grant_id
          WHERE g.expires_at<statement_timestamp()-interval '7 days' LIMIT 500);
        DELETE FROM public.oauth_refresh_tokens WHERE token_hash IN (
          SELECT r.token_hash FROM public.oauth_refresh_tokens r JOIN public.oauth_grants g ON g.id=r.grant_id
          WHERE g.expires_at<statement_timestamp()-interval '7 days' LIMIT 500);
      END $$;

      CREATE FUNCTION gatescope_security.locate_oauth_credential(requested_hash text, requested_kind text)
      RETURNS TABLE(tenant_id uuid, user_id uuid, grant_id uuid)
      LANGUAGE sql SECURITY DEFINER STABLE
      SET search_path = pg_catalog, public, gatescope_security AS $$
        SELECT g.tenant_id,g.user_id,g.id FROM public.oauth_grants g JOIN (
          SELECT c.grant_id FROM public.oauth_codes c
          WHERE requested_kind='code' AND c.code_hash=requested_hash
          UNION ALL
          SELECT r.grant_id FROM public.oauth_refresh_tokens r
          WHERE requested_kind='refresh_token' AND r.token_hash=requested_hash
          UNION ALL
          SELECT a.grant_id FROM public.oauth_access_tokens a JOIN public.mcp_tokens mt ON mt.id=a.token_id
          WHERE requested_kind='access_token' AND mt.token_hash=requested_hash
        ) credential ON credential.grant_id=g.id
        WHERE length(requested_hash)=64
      $$;

      CREATE FUNCTION gatescope_security.resolve_oauth_access(
        requested_hash text, expected_issuer text, expected_resource text
      ) RETURNS TABLE(token_id uuid,tenant_id uuid,subject_user_id uuid,client_id uuid,
        scopes text[],expires_at timestamptz)
      LANGUAGE sql SECURITY DEFINER STABLE
      SET search_path = pg_catalog, public, gatescope_security AS $$
        SELECT mt.id,mt.tenant_id,g.user_id,mt.client_id,mt.scopes,mt.expires_at
        FROM public.mcp_tokens mt
        JOIN public.oauth_access_tokens a ON a.token_id=mt.id AND a.tenant_id=mt.tenant_id
        JOIN public.oauth_grants g ON g.id=a.grant_id AND g.tenant_id=a.tenant_id
        JOIN public.mcp_clients mc ON mc.id=g.mcp_client_id AND mc.id=mt.client_id AND mc.tenant_id=g.tenant_id
        JOIN public.users u ON u.id=g.user_id AND mt.created_by=u.id
        JOIN public.tenants t ON t.id=g.tenant_id
        JOIN public.memberships m ON m.tenant_id=g.tenant_id AND m.user_id=g.user_id
        JOIN gatescope_security.mcp_token_locators loc ON loc.token_id=mt.id AND loc.token_hash=mt.token_hash
        WHERE mt.token_hash=requested_hash AND mt.revoked_at IS NULL AND mt.expires_at>statement_timestamp()
          AND g.issuer=expected_issuer AND g.resource=expected_resource
          AND g.revoked_at IS NULL AND g.expires_at>statement_timestamp()
          AND mc.status='ACTIVE' AND mc.owner_user_id=g.user_id
          AND u.status='ACTIVE' AND t.status='ACTIVE' AND m.status='ACTIVE' AND m.role='OWNER'
          AND mt.scopes <@ g.scopes
      $$;

      CREATE FUNCTION gatescope_security.revoke_oauth_family_on_token_revocation()
      RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path = pg_catalog, public, gatescope_security AS $$
      BEGIN
        IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL THEN
          UPDATE public.oauth_grants g SET revoked_at=COALESCE(g.revoked_at,NEW.revoked_at)
          FROM public.oauth_access_tokens a WHERE a.token_id=NEW.id AND a.grant_id=g.id
            AND a.tenant_id=NEW.tenant_id AND g.tenant_id=NEW.tenant_id;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER mcp_token_oauth_revocation AFTER UPDATE OF revoked_at ON mcp_tokens
        FOR EACH ROW EXECUTE FUNCTION gatescope_security.revoke_oauth_family_on_token_revocation();
      REVOKE ALL ON FUNCTION gatescope_security.locate_oauth_credential(text,text),
        gatescope_security.resolve_oauth_access(text,text,text),
        gatescope_security.cleanup_oauth_ephemeral_credentials(),
        gatescope_security.revoke_oauth_family_on_token_revocation() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION gatescope_security.locate_oauth_credential(text,text) TO gatescope_api;
      GRANT EXECUTE ON FUNCTION gatescope_security.cleanup_oauth_ephemeral_credentials() TO gatescope_api;
      GRANT EXECUTE ON FUNCTION gatescope_security.resolve_oauth_access(text,text,text) TO gatescope_api,gatescope_mcp;
    """)
    for table in PRIVATE_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'''CREATE POLICY tenant_isolation ON "{table}"
          USING (tenant_id=gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id=gatescope_security.current_tenant_id())''')
    op.execute("GRANT SELECT,INSERT,UPDATE ON oauth_grants,oauth_codes,oauth_refresh_tokens,oauth_access_tokens TO gatescope_api")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS mcp_token_oauth_revocation ON mcp_tokens")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.revoke_oauth_family_on_token_revocation()")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.resolve_oauth_access(text,text,text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.locate_oauth_credential(text,text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.cleanup_oauth_ephemeral_credentials()")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}"')
    op.execute("DROP TABLE IF EXISTS gatescope_security.oauth_pending_requests")
    op.execute("DROP TABLE IF EXISTS gatescope_security.oauth_registered_clients")
