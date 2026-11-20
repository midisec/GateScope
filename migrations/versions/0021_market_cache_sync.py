"""Durable public market cache progress and isolated forming candles."""
from importlib import import_module

from alembic import op

revision = "0021_market_cache_sync"
down_revision = "0020_research_backtests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    previous = import_module("migrations.versions.0018_market_strategy_subscriptions")
    op.execute(previous._subscription_function(include_strategies=True).replace(
        "ARRAY['1m','5m','15m','1h','4h','1d']", "ARRAY['1m','5m','15m','30m','1h','4h','8h','1d']",
    ))
    op.execute("""
      CREATE TABLE market_sync_state (
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        active boolean NOT NULL DEFAULT true, target_bars integer NOT NULL DEFAULT 8000,
        cached_bars integer NOT NULL DEFAULT 0, missing_bars integer NOT NULL DEFAULT 8000,
        first_open_time timestamptz, last_close_time timestamptz,
        last_live_at timestamptz, next_live_at timestamptz NOT NULL DEFAULT now(),
        live_failures integer NOT NULL DEFAULT 0, live_error_code text,
        last_history_at timestamptz, next_history_at timestamptz NOT NULL DEFAULT now(),
        history_status text NOT NULL DEFAULT 'PENDING', history_error_code text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (instrument_id,timeframe),
        CHECK (target_bars BETWEEN 5000 AND 9000),
        CHECK (cached_bars>=0 AND missing_bars>=0)
      );
      CREATE TABLE market_live_candles (
        instrument_id uuid NOT NULL REFERENCES instruments(id), timeframe text NOT NULL,
        open_time timestamptz NOT NULL, close_time timestamptz NOT NULL,
        open numeric NOT NULL, high numeric NOT NULL, low numeric NOT NULL,
        close numeric NOT NULL, volume numeric NOT NULL, observed_at timestamptz NOT NULL,
        PRIMARY KEY (instrument_id,timeframe),
        CHECK (open>0 AND close>0 AND low>0 AND high>=greatest(open,close,low)),
        CHECK (low<=least(open,close) AND volume>=0 AND close_time>open_time)
      );
      GRANT SELECT ON market_sync_state,market_live_candles TO gatescope_api;
      GRANT SELECT,INSERT,UPDATE ON market_sync_state,market_live_candles TO gatescope_market;
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    previous = import_module("migrations.versions.0018_market_strategy_subscriptions")
    op.execute(previous._subscription_function(include_strategies=True))
    op.execute("DROP TABLE market_live_candles; DROP TABLE market_sync_state")
