"""Append-only pattern facts and the formal strategy workload.

Revision ID: 0017_formal_strategy_worker
Revises: 0016_notification_worker_runtime_grants
"""

from __future__ import annotations

from alembic import op

revision = "0017_formal_strategy_worker"
down_revision = "0016_notification_worker_runtime_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE pattern_event_facts (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES tenants(id),
          fact_id text NOT NULL CHECK (fact_id ~ '^[a-f0-9]{64}$'),
          series_id text NOT NULL CHECK (series_id ~ '^[a-f0-9]{64}$'),
          revision integer NOT NULL CHECK (revision > 0),
          supersedes_fact_id text,
          detector_version text NOT NULL,
          instrument_id uuid NOT NULL REFERENCES instruments(id),
          timeframe text NOT NULL,
          pattern_id text NOT NULL,
          event_time timestamptz NOT NULL,
          available_at timestamptz NOT NULL,
          confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
          confirmed boolean NOT NULL,
          payload_json jsonb NOT NULL CHECK (jsonb_typeof(payload_json)='object'),
          payload_hash text NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
          input_digest text NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
          quality_flags text[] NOT NULL,
          live_eligible boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL,
          UNIQUE (tenant_id, fact_id),
          UNIQUE (tenant_id, series_id, revision)
        );
        CREATE INDEX pattern_event_facts_lookup_idx
          ON pattern_event_facts(tenant_id, instrument_id, timeframe, available_at DESC);
        ALTER TABLE pattern_event_facts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE pattern_event_facts FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON pattern_event_facts
          USING (tenant_id = gatescope_security.current_tenant_id())
          WITH CHECK (tenant_id = gatescope_security.current_tenant_id());
        CREATE TRIGGER pattern_event_facts_immutable
          BEFORE UPDATE OR DELETE ON pattern_event_facts
          FOR EACH ROW EXECUTE FUNCTION gatescope_security.reject_fact_mutation();

        CREATE OR REPLACE FUNCTION gatescope_security.strategy_active_rule_tenants(
          requested_limit integer
        )
        RETURNS TABLE(tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT r.tenant_id
            FROM public.alert_rules r
           WHERE r.status='ACTIVE'
           ORDER BY r.tenant_id
           LIMIT LEAST(GREATEST(requested_limit, 1), 100)
        $$;
        REVOKE ALL ON FUNCTION gatescope_security.strategy_active_rule_tenants(integer)
          FROM PUBLIC;

        CREATE OR REPLACE FUNCTION gatescope_security.market_watchlist_subscriptions()
        RETURNS TABLE (
          instrument_id uuid,
          venue text,
          market text,
          symbol text,
          settle_asset text,
          default_timeframe text
        )
        LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, public AS $$
          SELECT DISTINCT requested.instrument_id, i.venue, i.market, i.symbol,
                          i.settle_asset, requested.default_timeframe
            FROM (
              SELECT wi.instrument_id, wi.default_timeframe
                FROM public.watchlist_items wi
                JOIN public.watchlists w ON w.id=wi.watchlist_id
               WHERE w.deleted_at IS NULL
              UNION
              SELECT (v.condition_json->>'instrument_id')::uuid,
                     v.condition_json->>'timeframe'
                FROM public.alert_rules r
                JOIN public.alert_rule_versions v
                  ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id
                 AND v.version=r.current_version
               WHERE r.status='ACTIVE'
            ) requested(instrument_id, default_timeframe)
            JOIN public.instruments i ON i.id=requested.instrument_id
           WHERE i.status='ACTIVE'
           ORDER BY requested.instrument_id, requested.default_timeframe
        $$;

        GRANT SELECT ON pattern_event_facts TO gatescope_api;
        GRANT SELECT, INSERT ON pattern_event_facts TO gatescope_strategy;
        GRANT SELECT ON alert_rules, alert_rule_versions TO gatescope_strategy;
        GRANT SELECT, INSERT, UPDATE ON worker_heartbeats TO gatescope_strategy;
        GRANT EXECUTE ON FUNCTION
          gatescope_security.strategy_active_rule_tenants(integer)
          TO gatescope_strategy;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        DROP FUNCTION IF EXISTS gatescope_security.strategy_active_rule_tenants(integer);
        DROP TABLE IF EXISTS pattern_event_facts CASCADE;
        """
    )
