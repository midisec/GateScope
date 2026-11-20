"""Formal market subscriptions and workload heartbeat.

Revision ID: 0011_formal_market_worker
Revises: 0010_gate_testnet_execution
"""

from __future__ import annotations

from alembic import op

revision = "0011_formal_market_worker"
down_revision = "0010_gate_testnet_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE worker_heartbeats (
          workload text PRIMARY KEY CHECK (workload ~ '^[a-z][a-z0-9-]{2,79}$'),
          worker_id text NOT NULL CHECK (length(worker_id) BETWEEN 1 AND 80),
          running boolean NOT NULL,
          heartbeat_at timestamptz NOT NULL,
          status_json jsonb NOT NULL CHECK (jsonb_typeof(status_json)='object')
        );

        CREATE OR REPLACE FUNCTION gatescope_security.market_watchlist_subscriptions()
        RETURNS TABLE (
          instrument_id uuid,
          venue text,
          market text,
          symbol text,
          settle_asset text,
          default_timeframe text
        )
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = pg_catalog, public
        AS $$
          SELECT DISTINCT i.id, i.venue, i.market, i.symbol, i.settle_asset,
                          wi.default_timeframe
            FROM public.watchlist_items wi
            JOIN public.watchlists w ON w.id=wi.watchlist_id
            JOIN public.instruments i ON i.id=wi.instrument_id
           WHERE w.deleted_at IS NULL AND i.status='ACTIVE'
           ORDER BY i.id, wi.default_timeframe
        $$;

        REVOKE ALL ON worker_heartbeats FROM PUBLIC;
        REVOKE ALL ON FUNCTION gatescope_security.market_watchlist_subscriptions() FROM PUBLIC;
        GRANT SELECT ON worker_heartbeats TO gatescope_api;
        GRANT SELECT,INSERT,UPDATE ON worker_heartbeats TO gatescope_market;
        GRANT EXECUTE ON FUNCTION gatescope_security.market_watchlist_subscriptions()
          TO gatescope_market;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS gatescope_security.market_watchlist_subscriptions()")
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
