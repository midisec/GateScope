"""Subscribe every watchlist instrument to all Web chart timeframes.

Revision ID: 0013_watchlist_multitimeframe
Revises: 0012_web_telegram_configuration
"""

from __future__ import annotations

from alembic import op

revision = "0013_watchlist_multitimeframe"
down_revision = "0012_web_telegram_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
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
          SELECT DISTINCT i.id, i.venue, i.market, i.symbol, i.settle_asset, tf.timeframe
            FROM public.watchlist_items wi
            JOIN public.watchlists w ON w.id=wi.watchlist_id
            JOIN public.instruments i ON i.id=wi.instrument_id
            CROSS JOIN unnest(ARRAY['1m','5m','15m','1h','4h','1d']::text[]) AS tf(timeframe)
           WHERE w.deleted_at IS NULL AND i.status='ACTIVE'
           ORDER BY i.id, tf.timeframe
        $$;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
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
        """
    )
