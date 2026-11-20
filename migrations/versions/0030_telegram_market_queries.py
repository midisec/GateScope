"""Durable, tenant-bound, read-only Telegram market queries."""
from alembic import op

revision="0030_telegram_market_queries"
down_revision="0029_background_chart_cache"
branch_labels=depends_on=None


def upgrade():
    if op.get_bind().dialect.name!="postgresql":return
    op.execute("""CREATE TABLE telegram_market_requests (
        id uuid PRIMARY KEY,tenant_id uuid NOT NULL REFERENCES tenants(id),
        user_id uuid NOT NULL REFERENCES users(id),connection_id uuid NOT NULL REFERENCES telegram_connections(id),
        update_id bigint NOT NULL,command_json jsonb NOT NULL,response_json jsonb,
        status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','SENDING','SENT','FAILED')),
        attempt_count integer NOT NULL DEFAULT 0,next_attempt_at timestamptz NOT NULL DEFAULT now(),
        lease_owner text,lease_expires_at timestamptz,safe_error text,telegram_message_id bigint,
        created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(tenant_id,update_id)
    );
    CREATE INDEX telegram_market_due ON telegram_market_requests(next_attempt_at) WHERE status IN ('PENDING','SENDING');
    CREATE INDEX telegram_market_user ON telegram_market_requests(tenant_id,user_id,created_at);
    ALTER TABLE telegram_market_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE telegram_market_requests FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON telegram_market_requests
      USING(tenant_id=gatescope_security.current_tenant_id()) WITH CHECK(tenant_id=gatescope_security.current_tenant_id());
    GRANT SELECT,INSERT ON telegram_market_requests TO gatescope_api;
    GRANT SELECT,UPDATE,DELETE ON telegram_market_requests TO gatescope_notification;
    GRANT SELECT ON watchlists,watchlist_items,instruments,bars,market_pattern_tracking,market_pattern_events,
      market_sync_state,market_live_candles,market_chart_snapshots TO gatescope_notification;
    CREATE FUNCTION gatescope_security.telegram_market_due_tenants()
      RETURNS TABLE(tenant_id uuid) LANGUAGE sql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $$
        SELECT tenant_id FROM public.telegram_market_requests
        WHERE (status IN ('PENDING','SENDING') AND next_attempt_at<=now()
          AND (lease_expires_at IS NULL OR lease_expires_at<now()))
          OR created_at<now()-interval '7 days'
        GROUP BY tenant_id ORDER BY min(created_at) LIMIT 20
      $$;
    REVOKE ALL ON FUNCTION gatescope_security.telegram_market_due_tenants() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION gatescope_security.telegram_market_due_tenants() TO gatescope_notification;
    """)


def downgrade():
    if op.get_bind().dialect.name!="postgresql":return
    op.execute("""DROP FUNCTION gatescope_security.telegram_market_due_tenants(); DROP TABLE telegram_market_requests;
      REVOKE SELECT ON watchlists,watchlist_items,market_pattern_tracking,market_pattern_events,
        market_sync_state,market_live_candles,market_chart_snapshots FROM gatescope_notification""")
