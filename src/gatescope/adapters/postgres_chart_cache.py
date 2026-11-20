"""A rebuildable 1500-bar read model. Market worker warms it without a browser."""
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.application.market_payloads import canonical_bar, bar_payload
from gatescope.domain.market import TIMEFRAME_SECONDS

CHART_BARS=1500


class PostgresChartCache:
    def __init__(self,pool):
        self.pool=pool
        self.reader=PostgresMarketReadDatabase(pool)

    def warm(self,subscription):
        s=subscription
        build_started=datetime.now(timezone.utc)
        with self.pool.public_transaction() as c:
            c.execute("INSERT INTO market_chart_snapshots(instrument_id,timeframe) VALUES(%s,%s) ON CONFLICT DO NOTHING",(s.instrument_id,s.timeframe))
            row=c.execute("SELECT generation,built_generation FROM market_chart_snapshots WHERE instrument_id=%s AND timeframe=%s",(s.instrument_id,s.timeframe)).fetchone()
        if row[0]==row[1]:return False
        last=self.reader.latest_candle_time(str(s.instrument_id),s.timeframe)
        first=max(0,last-(CHART_BARS-1)*TIMEFRAME_SECONDS[s.timeframe]) if last is not None else 0
        rows=self.reader.get_canonical_candles(str(s.instrument_id),s.timeframe,first,last,CHART_BARS) if last is not None else []
        bars=[canonical_bar({"market":s.market},str(s.instrument_id),s.timeframe,r) for r in rows]
        items=[bar_payload(b) for b in bars]
        payload={"items":items,"quality_flags":sorted({q for b in bars for q in b.quality_flags})}
        with self.pool.public_transaction() as c:
            # No old generation may overwrite a revision arriving during a build.
            # A committed but not-yet-visible revision must not be marked built:
            # it can become available later without another generation change.
            changed=c.execute("""UPDATE market_chart_snapshots SET built_generation=%s,built_at=%s,
                first_open_time=%s,last_open_time=%s,bar_count=%s,payload_json=%s
                WHERE instrument_id=%s AND timeframe=%s AND generation=%s AND built_generation<generation
                AND NOT EXISTS(SELECT 1 FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                    AND open_time>=to_timestamp(%s) AND available_at>%s)""",
                (row[0],datetime.now(timezone.utc),bars[0].open_time if bars else None,
                 bars[-1].open_time if bars else None,len(bars),Jsonb(payload),s.instrument_id,s.timeframe,row[0],
                 s.instrument_id,s.timeframe,first,build_started))
        return bool(changed.rowcount)
