"""PostgreSQL/Timescale identity, config, market and outbox foundation.

Revision ID: 0002_postgres_foundation
Revises: 0001_phase0_security
"""

from __future__ import annotations

from alembic import op

revision = "0002_postgres_foundation"
down_revision = "0001_phase0_security"
branch_labels = None
depends_on = None

PRIVATE_TABLES = [
    "memberships", "sessions", "system_settings", "chart_layouts",
    "indicator_templates", "watchlists", "watchlist_items", "alert_rules",
    "alert_rule_versions", "notification_channels", "outbox_events",
    "inbox_events", "operations_current",
]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE SCHEMA IF NOT EXISTS gatescope_security")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gatescope_security.current_tenant_id()
        RETURNS uuid LANGUAGE plpgsql STABLE AS $$
        BEGIN
          RETURN nullif(current_setting('app.tenant_id', true), '')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
          RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gatescope_security.current_subject_id()
        RETURNS uuid LANGUAGE plpgsql STABLE AS $$
        BEGIN
          RETURN nullif(current_setting('app.subject_id', true), '')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
          RETURN NULL;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON SCHEMA gatescope_security FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA gatescope_security FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE tenants (
          id uuid PRIMARY KEY,
          slug text NOT NULL UNIQUE CHECK (slug = lower(slug) AND slug ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          status text NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED')),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );
        CREATE TABLE users (
          id uuid PRIMARY KEY,
          normalized_username text NOT NULL UNIQUE CHECK (normalized_username = lower(normalized_username)),
          display_username text NOT NULL,
          password_hash text NOT NULL,
          status text NOT NULL CHECK (status IN ('ACTIVE','LOCKED','DISABLED')),
          mfa_required boolean NOT NULL DEFAULT true,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
          locked_until timestamptz,
          password_changed_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL
        );
        CREATE TABLE memberships (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          role text NOT NULL CHECK (role IN ('VIEWER','RESEARCHER','OPERATOR','OWNER')),
          status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, user_id)
        );
        CREATE TABLE sessions (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          user_id uuid NOT NULL REFERENCES users(id),
          token_hash text NOT NULL UNIQUE,
          csrf_hash text NOT NULL,
          authenticated_at timestamptz NOT NULL,
          recent_auth_at timestamptz,
          expires_at timestamptz NOT NULL,
          revoked_at timestamptz,
          last_seen_at timestamptz NOT NULL,
          created_at timestamptz NOT NULL
        );
        CREATE INDEX sessions_user_active_idx ON sessions(user_id, expires_at) WHERE revoked_at IS NULL;
        CREATE TABLE system_settings (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          setting_key text NOT NULL,
          schema_version integer NOT NULL CHECK (schema_version > 0),
          value_json jsonb NOT NULL,
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, setting_key)
        );
        CREATE TABLE chart_layouts (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id), name text NOT NULL,
          spec_json jsonb NOT NULL, etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          deleted_at timestamptz, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX chart_layouts_active_name_idx
          ON chart_layouts(tenant_id, owner_user_id, name) WHERE deleted_at IS NULL;
        CREATE TABLE indicator_templates (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id), name text NOT NULL,
          spec_json jsonb NOT NULL, etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          deleted_at timestamptz, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX indicator_templates_active_name_idx
          ON indicator_templates(tenant_id, owner_user_id, name) WHERE deleted_at IS NULL;
        CREATE TABLE watchlists (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0), deleted_at timestamptz,
          created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX watchlists_active_name_idx
          ON watchlists(tenant_id, owner_user_id, name) WHERE deleted_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE instruments (
          id uuid PRIMARY KEY,
          venue text NOT NULL,
          market text NOT NULL,
          symbol text NOT NULL,
          base_asset text NOT NULL,
          quote_asset text NOT NULL,
          settle_asset text,
          status text NOT NULL,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          UNIQUE (venue, market, symbol)
        );
        CREATE TABLE instrument_revisions (
          id uuid PRIMARY KEY,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          revision integer NOT NULL CHECK (revision > 0),
          effective_at timestamptz NOT NULL,
          available_at timestamptz NOT NULL,
          tick_size numeric(38,18), lot_size numeric(38,18), min_notional numeric(38,18),
          contract_multiplier numeric(38,18), metadata_json jsonb NOT NULL,
          payload_hash text NOT NULL,
          created_at timestamptz NOT NULL,
          UNIQUE (instrument_id, revision), UNIQUE (instrument_id, payload_hash)
        );
        CREATE TABLE bars (
          venue text NOT NULL,
          market text NOT NULL,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          open_time timestamptz NOT NULL,
          close_time timestamptz NOT NULL,
          open numeric(38,18) NOT NULL CHECK (open > 0),
          high numeric(38,18) NOT NULL CHECK (high > 0),
          low numeric(38,18) NOT NULL CHECK (low > 0),
          close numeric(38,18) NOT NULL CHECK (close > 0),
          volume numeric(38,18) NOT NULL CHECK (volume >= 0),
          is_final boolean NOT NULL,
          source_sequence text,
          revision integer NOT NULL CHECK (revision > 0),
          available_at timestamptz NOT NULL,
          payload_hash text NOT NULL,
          quality_flags text[] NOT NULL DEFAULT '{}',
          created_at timestamptz NOT NULL,
          PRIMARY KEY (venue, market, instrument_id, timeframe, open_time, revision),
          CHECK (close_time > open_time),
          CHECK (high >= greatest(open, close, low)),
          CHECK (low <= least(open, close, high)),
          UNIQUE (venue, market, instrument_id, timeframe, open_time, payload_hash)
        );
        SELECT create_hypertable('bars', 'open_time', if_not_exists => TRUE, migrate_data => TRUE);
        CREATE INDEX bars_current_lookup_idx ON bars(instrument_id, timeframe, open_time DESC, revision DESC);
        CREATE TABLE watchlist_items (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          watchlist_id uuid NOT NULL REFERENCES watchlists(id), instrument_id uuid NOT NULL REFERENCES instruments(id),
          position integer NOT NULL CHECK (position >= 0), default_timeframe text NOT NULL,
          created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, watchlist_id, instrument_id), UNIQUE (tenant_id, watchlist_id, position)
        );
        CREATE TABLE alert_rules (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL REFERENCES users(id),
          name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          current_version integer NOT NULL CHECK (current_version > 0), status text NOT NULL CHECK (status IN ('ACTIVE','PAUSED','DELETED')),
          etag bigint NOT NULL DEFAULT 1 CHECK (etag > 0),
          created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
        CREATE UNIQUE INDEX alert_rules_active_name_idx
          ON alert_rules(tenant_id, owner_user_id, name) WHERE status<>'DELETED';
        CREATE TABLE alert_rule_versions (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          alert_rule_id uuid NOT NULL REFERENCES alert_rules(id), version integer NOT NULL CHECK (version > 0),
          condition_json jsonb NOT NULL, routing_json jsonb NOT NULL, created_by uuid NOT NULL REFERENCES users(id),
          created_at timestamptz NOT NULL, UNIQUE (tenant_id, alert_rule_id, version)
        );
        CREATE TABLE notification_channels (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          channel_type text NOT NULL CHECK (channel_type IN ('TELEGRAM','WEB')),
          alias text NOT NULL, status text NOT NULL CHECK (status IN ('PENDING','ACTIVE','PAUSED','REVOKED')),
          connection_id uuid, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
          UNIQUE (tenant_id, alias)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE outbox_events (
          event_id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          aggregate_type text NOT NULL, aggregate_id uuid NOT NULL, event_type text NOT NULL,
          schema_version integer NOT NULL CHECK (schema_version > 0), payload_json jsonb NOT NULL,
          occurred_at timestamptz NOT NULL, created_at timestamptz NOT NULL,
          lease_owner text, lease_expires_at timestamptz, attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
          next_attempt_at timestamptz NOT NULL, published_at timestamptz, dead_lettered_at timestamptz
        );
        CREATE INDEX outbox_dispatch_idx ON outbox_events(next_attempt_at, occurred_at) WHERE published_at IS NULL AND dead_lettered_at IS NULL;
        CREATE TABLE inbox_events (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          source text NOT NULL, external_id text NOT NULL, payload_hash text NOT NULL,
          received_at timestamptz NOT NULL, processed_at timestamptz, created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, source, external_id)
        );
        CREATE TABLE operations_current (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          actor_user_id uuid REFERENCES users(id), operation_type text NOT NULL,
          resource_type text, resource_id uuid, state text NOT NULL,
          progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
          result_link text, safe_error_code text, version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
          expires_at timestamptz, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
        );
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

    roles = [
        "gatescope_api", "gatescope_auth", "gatescope_market", "gatescope_strategy",
        "gatescope_notification", "gatescope_executor", "gatescope_mcp", "gatescope_chart",
        "gatescope_paper",
    ]
    for role in roles:
        op.execute(f"GRANT USAGE ON SCHEMA public, gatescope_security TO {role}")
        op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA gatescope_security TO {role}")
    for role in ["gatescope_api", "gatescope_auth", "gatescope_strategy", "gatescope_notification", "gatescope_executor", "gatescope_mcp", "gatescope_chart", "gatescope_paper"]:
        op.execute(f"GRANT SELECT ON tenants, users, memberships TO {role}")
    op.execute("GRANT SELECT, INSERT, UPDATE ON sessions, users TO gatescope_auth")
    op.execute("GRANT SELECT, INSERT, UPDATE ON system_settings, chart_layouts, indicator_templates, watchlists, watchlist_items, alert_rules, alert_rule_versions, operations_current TO gatescope_api")
    op.execute("GRANT DELETE ON watchlist_items TO gatescope_api")
    op.execute("GRANT SELECT ON instruments, instrument_revisions, bars TO gatescope_api, gatescope_strategy, gatescope_mcp, gatescope_chart")
    op.execute("GRANT SELECT, INSERT, UPDATE ON instruments, instrument_revisions, bars TO gatescope_market")
    op.execute("GRANT SELECT, INSERT, UPDATE ON outbox_events TO gatescope_api, gatescope_strategy, gatescope_notification")
    op.execute("GRANT SELECT, INSERT, UPDATE ON inbox_events TO gatescope_market, gatescope_notification")
    op.execute("GRANT SELECT, INSERT, UPDATE ON notification_channels TO gatescope_api, gatescope_notification")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in reversed([
        "operations_current", "inbox_events", "outbox_events", "notification_channels",
        "alert_rule_versions", "alert_rules", "watchlist_items", "bars", "instrument_revisions",
        "instruments", "watchlists", "indicator_templates", "chart_layouts", "system_settings",
        "sessions", "memberships", "users", "tenants",
    ]):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.current_subject_id()")
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.current_tenant_id()")
    op.execute("DROP SCHEMA IF EXISTS gatescope_security")
