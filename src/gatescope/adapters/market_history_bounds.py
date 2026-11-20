"""One as-of listing-aware range for public ingestion and structural research."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from gatescope.domain.history import HistoryBounds, listing_year_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS

# Earlier values are not credible cryptocurrency contract creation timestamps.
_MIN_LISTING_TIME = 1_230_768_000  # 2009-01-01 UTC


def _listing_timestamp(value: Any, now: datetime) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        timestamp = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        if len(value) > 12:
            return None
        timestamp = int(value)
    else:
        return None
    return timestamp if _MIN_LISTING_TIME <= timestamp <= int(now.timestamp()) else None


def history_bounds(
    conn: Any,
    instrument: UUID | str,
    timeframe: str,
    now: datetime,
) -> HistoryBounds:
    """Return inclusive closed-bar open times, using only known Gate futures data.

    The newest available/effective revision is authoritative. If that revision
    omits or invalidates create_time, do not resurrect an older metadata value.
    Cache evidence may only widen a trusted range; an empty or short cache never
    proves listing age. Queries stay on indexed instrument/timeframe time ranges.
    """
    bounds = listing_year_bounds(now, timeframe)
    row = conn.execute(
        """SELECT i.venue,i.market,i.symbol,r.metadata_json
           FROM instruments i LEFT JOIN LATERAL (
             SELECT metadata_json FROM instrument_revisions
             WHERE instrument_id=i.id AND available_at<=%s AND effective_at<=%s
             ORDER BY revision DESC LIMIT 1
           ) r ON true WHERE i.id=%s""",
        (now, now, instrument),
    ).fetchone()
    if not row or row[0] != "GATE" or row[1] not in {"FUTURES_USDT", "FUTURES_BTC"}:
        return bounds
    metadata = row[3]
    if not isinstance(metadata, dict) or metadata.get("name", row[2]) != row[2]:
        return bounds
    listing_time = _listing_timestamp(metadata.get("create_time"), now)
    if listing_time is None:
        return bounds
    bounds = listing_year_bounds(now, timeframe, listing_time=listing_time)
    if bounds["range_policy"] != "LISTING_CLIPPED":
        return bounds
    step = TIMEFRAME_SECONDS[timeframe]
    earlier = conn.execute(
        """SELECT extract(epoch FROM open_time)::bigint FROM bars
           WHERE instrument_id=%s AND timeframe=%s AND venue=%s AND market=%s
             AND is_final=true AND available_at<=%s AND created_at<=%s AND close_time<=%s
             AND open_time>=to_timestamp(%s) AND open_time<to_timestamp(%s)
             AND mod(extract(epoch FROM open_time),%s)=0
           ORDER BY open_time LIMIT 1""",
        (instrument, timeframe, row[0], row[1], now, now, now,
         bounds["requested_start"], bounds["start"], step),
    ).fetchone()
    if earlier is not None:
        bounds = listing_year_bounds(
            now, timeframe, listing_time=listing_time, first_cached_time=int(earlier[0]),
        )
    return bounds
