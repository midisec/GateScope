"""Preserve six chart periods while adding active strategy subscriptions.

Revision ID: 0018_market_strategy_subscriptions
Revises: 0017_formal_strategy_worker
"""

from __future__ import annotations

from alembic import op

revision = "0018_market_strategy_subscriptions"
down_revision = "0017_formal_strategy_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_subscription_function(include_strategies=True))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_subscription_function(include_strategies=False))


def _subscription_function(*, include_strategies: bool) -> str:
    strategy_union = """
              UNION
              SELECT (v.condition_json->>'instrument_id')::uuid,
                     v.condition_json->>'timeframe'
                FROM public.alert_rules r
                JOIN public.alert_rule_versions v
                  ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id
                 AND v.version=r.current_version
               WHERE r.status='ACTIVE'
    """ if include_strategies else ""
    return f"""
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
              SELECT wi.instrument_id, tf.timeframe
                FROM public.watchlist_items wi
                JOIN public.watchlists w ON w.id=wi.watchlist_id
                CROSS JOIN unnest(
                  ARRAY['1m','5m','15m','1h','4h','1d']::text[]
                ) AS tf(timeframe)
               WHERE w.deleted_at IS NULL
              {strategy_union}
            ) requested(instrument_id, default_timeframe)
            JOIN public.instruments i ON i.id=requested.instrument_id
           WHERE i.status='ACTIVE'
           ORDER BY requested.instrument_id, requested.default_timeframe
        $$;
    """
