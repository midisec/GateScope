"""Read-only, exact-series and bounded-query source for high-resolution VPVR."""
from __future__ import annotations

from uuid import UUID

from gatescope.domain.volume_profile import MAX_BARS, VolumeProfilePlan, build_volume_profile


class PostgresVolumeProfileRepository:
    def __init__(self, pool):
        self.pool = pool

    def get(self, instrument_id: UUID, plan: VolumeProfilePlan, *, asof: int):
        # Read complete source slots only. In particular a partial right edge
        # must not turn a valid 50,000-slot request into 50,001 fetched candles.
        source_end = min(plan.first_open+plan.expected_bars*plan.source_step_seconds,
                         (asof//plan.source_step_seconds)*plan.source_step_seconds)
        with self.pool.public_transaction() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            instrument_row = connection.execute(
                "SELECT id,venue,market,symbol FROM instruments WHERE id=%s", (instrument_id,)
            ).fetchone()
            if instrument_row is None:
                raise LookupError("INSTRUMENT_NOT_FOUND")
            instrument = dict(zip(("id", "venue", "market", "symbol"), instrument_row, strict=True))
            # Match bars_current_lookup_idx in descending order. The outer sort
            # sees <=50,001 canonical timestamps, never a full-history sort.
            # Availability is tested before choosing the latest KNOWN revision;
            # finality and close/duration/OHLC tests happen AFTER canonicalization.
            # Thus a newer known non-final revision cannot revive an older final.
            raw = connection.execute("""
                SELECT * FROM (
                  SELECT DISTINCT ON (open_time)
                    extract(epoch FROM open_time) AS ts, extract(epoch FROM close_time) AS close_ts,
                    open,high,low,close,volume,is_final,revision,
                    extract(epoch FROM available_at),extract(epoch FROM created_at),
                    venue,market,instrument_id,timeframe,quality_flags
                  FROM bars WHERE instrument_id=%s AND timeframe=%s AND venue=%s AND market=%s
                    AND open_time>=to_timestamp(%s) AND open_time<to_timestamp(%s)
                    AND available_at<=to_timestamp(%s) AND created_at<=to_timestamp(%s)
                  ORDER BY open_time DESC,revision DESC LIMIT %s
                ) canonical ORDER BY ts ASC
                """, (instrument_id, plan.source_timeframe, instrument["venue"], instrument["market"],
                      plan.first_open, source_end, asof, asof, MAX_BARS+1)).fetchall()
        names = ("ts", "close_ts", "open", "high", "low", "close", "volume", "is_final", "revision",
                 "available_at", "created_at", "venue", "market", "instrument_id", "timeframe", "quality_flags")
        return build_volume_profile(instrument, plan, [dict(zip(names, row, strict=True)) for row in raw], asof=asof)
