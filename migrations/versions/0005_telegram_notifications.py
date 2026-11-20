"""Telegram linking and durable notification delivery control plane.

Revision ID: 0005_telegram_notifications
Revises: 0004_formal_identity
"""

from __future__ import annotations

from alembic import op

revision = "0005_telegram_notifications"
down_revision = "0004_formal_identity"
branch_labels = None
depends_on = None

PRIVATE_TABLES = [
    "telegram_connections",
    "telegram_link_tokens",
    "notification_routes",
    "notification_deliveries",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE telegram_connections (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          telegram_user_id bigint NOT NULL,
          telegram_chat_id bigint NOT NULL,
          telegram_username text,
          status text NOT NULL CHECK (status IN ('PENDING_CONFIRMATION','ACTIVE','REVOKED')),
          verified_at timestamptz,
          revoked_at timestamptz,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, user_id, telegram_chat_id)
        );
        CREATE INDEX telegram_connections_chat_idx
          ON telegram_connections(telegram_chat_id) WHERE status='ACTIVE';

        CREATE TABLE telegram_link_tokens (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          session_id uuid NOT NULL REFERENCES sessions(id),
          token_hash text NOT NULL UNIQUE,
          expires_at timestamptz NOT NULL,
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
          consumed_at timestamptz,
          created_at timestamptz NOT NULL
        );
        CREATE INDEX telegram_link_tokens_expiry_idx
          ON telegram_link_tokens(expires_at) WHERE consumed_at IS NULL;

        CREATE TABLE telegram_link_token_locators (
          token_hash text PRIMARY KEY CHECK (length(token_hash)=64),
          tenant_id uuid NOT NULL,
          expires_at timestamptz NOT NULL
        );
        REVOKE ALL ON telegram_link_token_locators FROM PUBLIC;

        CREATE OR REPLACE FUNCTION gatescope_security.register_telegram_link_locator(
          requested_hash text, requested_tenant uuid, requested_expiry timestamptz
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
        BEGIN
          IF requested_tenant IS DISTINCT FROM gatescope_security.current_tenant_id()
             OR length(requested_hash) <> 64 THEN
            RAISE EXCEPTION 'invalid locator context';
          END IF;
          INSERT INTO public.telegram_link_token_locators(token_hash, tenant_id, expires_at)
          VALUES (requested_hash, requested_tenant, requested_expiry);
        END
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.resolve_telegram_link_tenant(requested_hash text)
        RETURNS uuid
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT tenant_id FROM public.telegram_link_token_locators
          WHERE token_hash=requested_hash AND expires_at>statement_timestamp()
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.remove_telegram_link_locator(requested_hash text)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public, gatescope_security AS $$
        BEGIN
          DELETE FROM public.telegram_link_token_locators
          WHERE token_hash=requested_hash
            AND tenant_id=gatescope_security.current_tenant_id();
        END
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.register_telegram_link_locator(text, uuid, timestamptz) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.resolve_telegram_link_tenant(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.remove_telegram_link_locator(text) FROM PUBLIC;

        ALTER TABLE notification_channels
          ADD COLUMN owner_user_id uuid REFERENCES users(id),
          ADD COLUMN etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          ADD CONSTRAINT notification_channels_connection_fk
            FOREIGN KEY (connection_id) REFERENCES telegram_connections(id);

        CREATE TABLE notification_routes (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          channel_id uuid NOT NULL REFERENCES notification_channels(id),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          event_types text[] NOT NULL,
          filters_json jsonb NOT NULL,
          quiet_hours_json jsonb NOT NULL,
          rate_policy_json jsonb NOT NULL,
          status text NOT NULL CHECK (status IN ('ACTIVE','PAUSED','DELETED')),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX notification_routes_active_name_idx
          ON notification_routes(tenant_id, owner_user_id, name) WHERE status<>'DELETED';

        CREATE TABLE notification_deliveries (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          channel_id uuid NOT NULL REFERENCES notification_channels(id),
          event_id uuid NOT NULL,
          template_version integer NOT NULL CHECK (template_version > 0),
          status text NOT NULL CHECK (status IN ('PENDING','SENDING','SENT','RETRY','FAILED','DEAD_LETTER')),
          attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          next_attempt_at timestamptz NOT NULL,
          lease_owner text,
          lease_expires_at timestamptz,
          telegram_message_id bigint,
          safe_error_code text,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, channel_id, event_id, template_version)
        );
        CREATE INDEX notification_deliveries_dispatch_idx
          ON notification_deliveries(next_attempt_at, created_at)
          WHERE status IN ('PENDING','RETRY');

        CREATE OR REPLACE FUNCTION gatescope_security.notification_outbox_due_tenants(requested_limit integer)
        RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT oe.tenant_id
          FROM public.outbox_events oe
          WHERE oe.published_at IS NULL AND oe.dead_lettered_at IS NULL
            AND oe.next_attempt_at<=statement_timestamp()
            AND (oe.lease_expires_at IS NULL OR oe.lease_expires_at<statement_timestamp())
            AND oe.event_type IN ('notification.requested.v1','notification.test.requested.v1')
          ORDER BY oe.tenant_id
          LIMIT LEAST(GREATEST(requested_limit, 1), 100)
        $$;

        CREATE OR REPLACE FUNCTION gatescope_security.notification_delivery_due_tenants(requested_limit integer)
        RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT nd.tenant_id
          FROM public.notification_deliveries nd
          WHERE (
              nd.status IN ('PENDING','RETRY') AND nd.next_attempt_at<=statement_timestamp()
            ) OR (
              nd.status='SENDING' AND nd.lease_expires_at<statement_timestamp()
            )
          ORDER BY nd.tenant_id
          LIMIT LEAST(GREATEST(requested_limit, 1), 100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.notification_outbox_due_tenants(integer) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.notification_delivery_due_tenants(integer) FROM PUBLIC;
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
        """GRANT SELECT, INSERT, UPDATE ON telegram_connections, telegram_link_tokens,
                    notification_routes, notification_deliveries
             TO gatescope_api, gatescope_notification"""
    )
    op.execute(
        """GRANT EXECUTE ON FUNCTION
             gatescope_security.register_telegram_link_locator(text, uuid, timestamptz),
             gatescope_security.resolve_telegram_link_tenant(text),
             gatescope_security.remove_telegram_link_locator(text),
             gatescope_security.notification_outbox_due_tenants(integer),
             gatescope_security.notification_delivery_due_tenants(integer)
           TO gatescope_api, gatescope_notification"""
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON notification_channels TO gatescope_api, gatescope_notification")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.notification_delivery_due_tenants(integer)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.notification_outbox_due_tenants(integer)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.remove_telegram_link_locator(text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.resolve_telegram_link_tenant(text)")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.register_telegram_link_locator(text, uuid, timestamptz)")
    op.execute("ALTER TABLE notification_channels DROP CONSTRAINT IF EXISTS notification_channels_connection_fk")
    op.execute("ALTER TABLE notification_channels DROP COLUMN IF EXISTS etag")
    op.execute("ALTER TABLE notification_channels DROP COLUMN IF EXISTS owner_user_id")
    for table in reversed(PRIVATE_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.execute("DROP TABLE IF EXISTS telegram_link_token_locators")
