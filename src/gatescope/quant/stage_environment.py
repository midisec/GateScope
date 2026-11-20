"""Causal, fixed-version environment labels; descriptive, not a trade filter."""
from __future__ import annotations

import math
from statistics import median

from gatescope.indicators.builtins import EMAValue


VERSION = "se1"


def stage_environment_key(bars, step_seconds):
    """Only the caller's already-closed prefix is admitted, max 240 candles.

    Three axes: EMA20/50 trend with 0.25 ATR14 separation and five-bar slope;
    ATR14 against preceding 60 ATR14 values; volume against preceding 20 bars.
    At least 110 contiguous valid candles are required. No outcome, future bar,
    user setting, optimization, or current confidence influences these labels.
    """
    if not isinstance(step_seconds, int) or isinstance(step_seconds, bool) or step_seconds <= 0:
        return None
    rows = bars[-240:]
    for index in range(len(rows)-1, 0, -1):
        if rows[index]["ts"]-rows[index-1]["ts"] != step_seconds:
            rows = rows[index:]
            break
    if len(rows) < 110:
        return None
    try:
        values = [{key: float(row[key]) for key in ("open", "high", "low", "close", "volume")} for row in rows]
        if any(not all(math.isfinite(v) and (v >= 0 if k == "volume" else v > 0) for k, v in row.items())
               or row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"])
               for row in values):
            return None
        ranges = [max(row["high"]-row["low"], abs(row["high"]-previous["close"]), abs(row["low"]-previous["close"]))
                  for previous, row in zip(values[:-1], values[1:], strict=True)]
        atr = math.fsum(ranges[-14:])/14
        previous_atrs = [math.fsum(ranges[end-14:end])/14 for end in range(len(ranges)-60, len(ranges))]
        baseline = median(previous_atrs)
        volume_mean = math.fsum(row["volume"] for row in values[-21:-1])/20
        if atr <= 0 or baseline <= 0 or volume_mean <= 0:
            return None
        fast, slow = EMAValue(20), EMAValue(50)
        fast_values = [fast.push(row["close"]) for row in values]
        for row in values:
            slow.push(row["close"])
        trend = "UP" if fast_values[-1]-slow.value > .25*atr and fast_values[-1] > fast_values[-6] else (
            "DOWN" if slow.value-fast_values[-1] > .25*atr and fast_values[-1] < fast_values[-6] else "RANGE")
        atr_ratio = atr/baseline
        volatility = "HIGH" if atr_ratio >= 1.5 else "LOW" if atr_ratio <= 2/3 else "NORMAL"
        volume_ratio = values[-1]["volume"]/volume_mean
        volume = "HIGH" if volume_ratio >= 1.5 else "LOW" if volume_ratio < .7 else "NORMAL"
        return f"{VERSION}:{trend}:{volatility}:{volume}"
    except (ValueError, TypeError, KeyError, OverflowError, ArithmeticError):
        return None
