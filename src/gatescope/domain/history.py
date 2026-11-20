"""Shared, explicit rolling-year market data policy."""
from bisect import bisect_right
from collections.abc import Iterable
from datetime import datetime
from typing import Literal, TypedDict

from gatescope.domain.market import TIMEFRAME_SECONDS

HISTORY_DAYS = 365
HISTORY_SECONDS = HISTORY_DAYS * 86400


class HistoryBounds(TypedDict):
    start: int
    end: int
    target_bars: int
    listing_time: int | None
    range_policy: Literal["ROLLING_YEAR", "LISTING_CLIPPED"]
    requested_start: int


def year_bounds(now: datetime, timeframe: str) -> tuple[int, int, int]:
    step = TIMEFRAME_SECONDS[timeframe]
    end = int(now.timestamp()) // step * step - step
    count = HISTORY_SECONDS // step
    return end - (count - 1) * step, end, count


def listing_year_bounds(
    now: datetime,
    timeframe: str,
    *,
    listing_time: int | None = None,
    first_cached_time: int | None = None,
) -> HistoryBounds:
    """Clip a rolling year only with trusted listing metadata, never cache age.

    The listing bucket is rounded down because its first candle can be partial.
    Earlier valid cached candles may widen the range but cannot shrink it. A
    contract with no closed listing bucket retains the rolling-year policy:
    persisted sync targets require at least one bar, so claiming an empty range
    is complete would fabricate coverage. The next closed bucket enables clipping.
    """
    requested_start, end, count = year_bounds(now, timeframe)
    step = TIMEFRAME_SECONDS[timeframe]
    start = requested_start
    if listing_time is not None:
        listing_bucket = listing_time // step * step
        if requested_start < listing_bucket <= end:
            start = listing_bucket
            if first_cached_time is not None and requested_start <= first_cached_time < start:
                start = first_cached_time // step * step
            count = (end - start) // step + 1
    return {
        "start": start,
        "end": end,
        "target_bars": count,
        "listing_time": listing_time,
        "range_policy": "LISTING_CLIPPED" if start > requested_start else "ROLLING_YEAR",
        "requested_start": requested_start,
    }


def eligible_gap(
    gaps: Iterable[tuple[int, int]],
    blocked: Iterable[tuple[int, int]],
    step: int,
    limit: int = 900,
) -> tuple[int, int] | None:
    """Rightmost eligible subrange of the first eligible gap, capped at limit.

    Gap order belongs to the caller (normally newest first) and its database
    cursor stays streaming. Normalize/sort/merge suppression ranges once, then
    locate each gap's rightmost usable interval in O(log B), not O(B). Adjacent
    suppressed grid intervals merge because there is no usable candle between
    them. Suppression never changes the separately calculated missing count.
    """
    if step <= 0 or limit <= 0:
        raise ValueError("history gap step and limit must be positive")
    merged: list[tuple[int, int]] = []
    for left, right in sorted(((a + step - 1) // step * step, b // step * step) for a, b in blocked):
        if left > right:
            continue  # No candle open lies inside this unaligned suppression.
        if merged and left <= merged[-1][1] + step:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    starts = [left for left, _ in merged]
    for start, end in gaps:
        if start > end:
            continue
        index = bisect_right(starts, end) - 1
        right = end
        if index >= 0 and merged[index][1] >= end:
            right = merged[index][0] - step
            if right < start:
                continue
            index -= 1
        # After merging adjacent intervals, the candle immediately before the
        # final blocked interval is necessarily unblocked: no backwards scan.
        left = max(start, merged[index][1] + step) if index >= 0 else start
        return max(left, right - (limit - 1) * step), right
    return None
