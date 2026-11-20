"""Encrypted, tenant-scoped Telegram Bot configuration from the Web control plane.

Revision ID: 0012_web_telegram_configuration
Revises: 0011_formal_market_worker
"""

from __future__ import annotations

from alembic import op

revision = "0012_web_telegram_configuration"
down_revision = "0011_formal_market_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE telegram_bot_configs (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL UNIQUE REFERENCES tenants(id),
          bot_username text NOT NULL CHECK (bot_username ~ '^[A-Za-z][A-Za-z0-9_]{4,63}$'),
          token_fingerprint text NOT NULL CHECK (token_fingerprint ~ '^SHA256:[0-9A-F]{12}$'),
          allowed_user_ids bigint[] NOT NULL CHECK (cardinality(allowed_user_ids) BETWEEN 1 AND 100),
          webhook_id text NOT NULL CHECK (length(webhook_id) BETWEEN 32 AND 128),
          webhook_secret_hash text NOT NULL CHECK (length(webhook_secret_hash)=64),
          secret_ciphertext bytea NOT NULL,
          secret_nonce bytea NOT NULL CHECK (octet_length(secret_nonce)=12),
          wrapped_data_key bytea NOT NULL,
          encryption_context text NOT NULL CHECK (length(encryption_context) BETWEEN 16 AND 512),
          key_version text NOT NULL CHECK (length(key_version) BETWEEN 1 AND 80),
          secret_version integer NOT NULL CHECK (secret_version > 0),
          status text NOT NULL CHECK (status IN ('ACTIVE','NEEDS_HTTPS','VALIDATION_FAILED','DISABLED')),
          last_safe_error_code text,
          created_by uuid NOT NULL REFERENCES users(id),
          updated_by uuid NOT NULL REFERENCES users(id),
          validated_at timestamptz,
          webhook_configured_at timestamptz,
          disabled_at timestamptz,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );

        CREATE TABLE telegram_bot_config_locators (
          webhook_id_hash text PRIMARY KEY CHECK (length(webhook_id_hash)=64),
          tenant_id uuid NOT NULL UNIQUE
        );
        REVOKE ALL ON telegram_bot_config_locators FROM PUBLIC;

        CREATE OR REPLACE FUNCTION gatescope_security.replace_telegram_bot_config_locator(
          requested_hash text, requested_tenant uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
        BEGIN
          IF requested_tenant IS DISTINCT FROM gatescope_security.current_tenant_id()
             OR length(requested_hash) <> 64 THEN
            RAISE EXCEPTION 'invalid Telegram config locator context';
          END IF;
          DELETE FROM public.telegram_bot_config_locators WHERE tenant_id=requested_tenant;
          INSERT INTO public.telegram_bot_config_locators(webhook_id_hash, tenant_id)
          VALUES (requested_hash, requested_tenant);
        END
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.remove_telegram_bot_config_locator(
          requested_tenant uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
        BEGIN
          IF requested_tenant IS DISTINCT FROM gatescope_security.current_tenant_id() THEN
            RAISE EXCEPTION 'invalid Telegram config locator context';
          END IF;
          DELETE FROM public.telegram_bot_config_locators WHERE tenant_id=requested_tenant;
        END
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.resolve_telegram_bot_config_tenant(
          requested_hash text
        ) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT tenant_id FROM public.telegram_bot_config_locators
          WHERE webhook_id_hash=requested_hash
        $$;

        REVOKE ALL ON FUNCTION gatescope_security.replace_telegram_bot_config_locator(text, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.remove_telegram_bot_config_locator(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.resolve_telegram_bot_config_tenant(text) FROM PUBLIC;

        ALTER TABLE telegram_bot_configs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE telegram_bot_configs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON telegram_bot_configs
          USING (tenant_id = gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id = gatescope_security.current_tenant_id());

        GRANT SELECT,INSERT,UPDATE ON telegram_bot_configs TO gatescope_api;
        GRANT SELECT ON telegram_bot_configs TO gatescope_notification;
        GRANT EXECUTE ON FUNCTION
          gatescope_security.replace_telegram_bot_config_locator(text, uuid),
          gatescope_security.remove_telegram_bot_config_locator(uuid),
          gatescope_security.resolve_telegram_bot_config_tenant(text)
          TO gatescope_api;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.resolve_telegram_bot_config_tenant(text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.remove_telegram_bot_config_locator(uuid)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.replace_telegram_bot_config_locator(text, uuid)")
    op.execute("DROP TABLE IF EXISTS telegram_bot_config_locators")
    op.execute("DROP TABLE IF EXISTS telegram_bot_configs")
