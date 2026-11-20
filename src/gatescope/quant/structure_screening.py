"""Read-only research screen, never a strategy admission or order instruction.

One predeclared 20-bar horizon; no choosing the best horizon after seeing returns.
The interval describes historical Bernoulli frequency, not today's profit odds.
"""

import math
from numbers import Real
from statistics import NormalDist

from gatescope.quant.pattern_study import catalog
from gatescope.quant.structure_detection import value_at

POLICY = "structure-screen-1.1"
HORIZON = 20
MIN_SAMPLES = 100
MIN_WIN_RATE = 0.55
MIN_GEOMETRY = 0.70
MAX_INTERVAL_WIDTH = 0.30
REASONS = {
    "FORMING": "形成中：尚无同阶段入场胜率",
    "NEUTRAL": "方向未确定",
    "GEOMETRY": "几何匹配不足 70 分",
    "SAMPLES": "历史有效样本不足 100",
    "HISTORY_CAPACITY": "当前历史窗口的独立样本容量不足 100，需更长历史；可查看观察列表",
    "WIN_RATE": "历史净胜率未超过 55%",
    "INTERVAL": "校正区间下限未超过 50% 或区间过宽",
    "EXPECTANCY": "历史平均净收益不为正",
}


def sample_capacity(timeframe_seconds, history_seconds=365 * 86400):
    """An upper bound, not a fabricated sample count or a relaxed threshold.

    Every completed non-overlapping hold consumes HORIZON candles. Actual
    capacity is lower after warm-up, gaps, anchor cutoffs and pattern grouping.
    """
    if timeframe_seconds <= 0 or history_seconds < 0:
        raise ValueError("invalid history window")
    maximum = int(history_seconds // timeframe_seconds // HORIZON)
    return {
        "timeframe_seconds": timeframe_seconds,
        "history_seconds": history_seconds,
        "maximum_independent_samples": maximum,
        "sample_requirement_reachable": maximum >= MIN_SAMPLES,
    }


def historical_stats(
    wins, count, mean, watchlist_count, *, timeframe_seconds=None, history_seconds=365 * 86400
):
    # Correct over the full user's search universe, including other coin pages,
    # both entry stages and directions. No independence between tests is assumed.
    trials = len(catalog()) * 2 * 2 * 8 * max(1, watchlist_count)
    interval = None
    if count >= 20:
        p = wins / count
        z = NormalDist().inv_cdf(1 - 0.05 / (2 * trials))
        den = 1 + z * z / count
        center = (p + z * z / (2 * count)) / den
        half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / den
        interval = {"lower": max(0.0, center - half), "upper": min(1.0, center + half)}
    capacity = sample_capacity(timeframe_seconds, history_seconds) if timeframe_seconds is not None else {}
    return {
        "sample_count": count,
        "wins": wins,
        "win_rate": wins / count if count else None,
        "mean_net_return": mean,
        "interval": interval,
        "hypotheses": trials,
        "horizon": HORIZON,
        "basis": "PRIOR_STRUCTURE_REPLAY_ONLY",
        "policy": POLICY,
        **capacity,
    }


def _hard_limits_usable(levels, last_bar):
    """Mirror lifecycle wick rules; malformed new constraints are not usable.

    An absent/empty array preserves legacy screening. Only candles needed by
    these extra constraints are validated here; this is not another admission
    threshold or a confirmation path.
    """
    limits = levels.get("hard_limits", [])
    if not isinstance(limits, list):
        return False
    if not limits:
        return True
    if not isinstance(last_bar, dict):
        return False

    def positive_number(value):
        try:
            return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0
        except (OverflowError, ValueError):
            return False

    for limit in limits:
        if not isinstance(limit, dict):
            return False
        side, price = limit.get("side"), limit.get("price")
        inclusive = limit.get("inclusive", True)
        if (
            not isinstance(side, Real)
            or isinstance(side, bool)
            or side not in (-1, 1)
            or not positive_number(price)
            or not isinstance(inclusive, bool)
        ):
            return False
        wick = last_bar.get("high" if side > 0 else "low")
        if not positive_number(wick):
            return False
        distance = side * (wick - price)
        if distance >= 0 if inclusive else distance > 0:
            return False
    return True


def usable(record, last_bar, as_of, step):
    """Reject stale/resolved geometry without changing the historical lifecycle.

    Confirmed records are terminal in that lifecycle, so independently recheck
    their frozen invalidation line against every subsequent supplied candle.
    Intrabar observations may veto a candidate but never confirm one.
    """
    if record["phase"] not in ("FORMING", "FORMED", "CONFIRMED"):
        return False
    if record.get("deadline_time", 0) <= as_of:
        return False
    if record["observed_time"] > as_of:
        return False
    if record["phase"] == "CONFIRMED" and as_of - record["observed_time"] > 2 * step:
        return False
    event = record["latest"]
    levels = event.get("levels") or {}
    try:
        if not _hard_limits_usable(levels, last_bar):
            return False
        if "upper" in levels and value_at(levels["upper"], last_bar["ts"]) <= value_at(
            levels["lower"], last_bar["ts"]
        ):
            return False
        side = levels.get("invalid_side")
        if side:
            limit = (
                value_at(levels["invalid_edge"], last_bar["ts"])
                if "invalid_edge" in levels
                else levels["invalid_price"]
            )
            price = (
                last_bar["high"]
                if levels.get("hard") and side > 0
                else last_bar["low"]
                if levels.get("hard")
                else last_bar["close"]
            )
            if not math.isfinite(limit) or side * (price - limit) >= (
                0 if levels.get("hard") else levels["buffer"]
            ):
                return False
    except (KeyError, TypeError, ZeroDivisionError):
        return False
    return bool(levels)


def evaluate(record, stats):
    reasons = []
    event = record["latest"]
    if record["phase"] == "FORMING":
        reasons.append("FORMING")
    if event.get("signal_direction") not in ("bullish", "bearish"):
        reasons.append("NEUTRAL")
    score = event.get("confidence")
    if score is None or not math.isfinite(score) or score < MIN_GEOMETRY:
        reasons.append("GEOMETRY")
    if stats["sample_count"] < MIN_SAMPLES:
        reasons.append("SAMPLES")
    if stats.get("sample_requirement_reachable") is False:
        reasons.append("HISTORY_CAPACITY")
    if stats["win_rate"] is None or stats["win_rate"] <= MIN_WIN_RATE:
        reasons.append("WIN_RATE")
    interval = stats.get("interval")
    if not interval or interval["lower"] <= 0.5 or interval["upper"] - interval["lower"] > MAX_INTERVAL_WIDTH:
        reasons.append("INTERVAL")
    mean = stats["mean_net_return"]
    if mean is None or not math.isfinite(mean) or mean <= 0:
        reasons.append("EXPECTANCY")
    return {"qualified": not reasons, "reasons": reasons, "statistics": stats}
