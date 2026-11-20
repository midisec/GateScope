"""Paired time-block diagnostics for fixed-rule structural observations.

Cases may overlap and share a market move. Resample calendar blocks jointly for
selected and baseline cases instead of treating every pattern as independent.
This is not a sealed holdout, significance guarantee or deployment permission.
"""
from __future__ import annotations

import math
import random
from typing import Any, Iterable, Mapping

VERSION = "paired-utc-day-blocks-1"


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    at = (len(ordered) - 1) * q
    lower = int(at)
    fraction = at - lower
    return ordered[lower] * (1 - fraction) + ordered[min(lower + 1, len(ordered) - 1)] * fraction


def time_block_comparison(rows: Iterable[Mapping[str, Any]], *, block_days: int = 7,
                          resamples: int = 1000, seed: int = 1729) -> dict:
    """Input UTC epoch-day groups, with selected/baseline count/sum/wins.

    Missing calendar days are explicitly zero-count, not compressed away.
    Returns are normalized per-case net returns, never daily portfolio PnL.
    The common resampled indices preserve selected-vs-baseline dependence.
    """
    if isinstance(block_days, bool) or not isinstance(block_days, int) or not 1 <= block_days <= 1000:
        raise ValueError("STRUCTURE_VALIDATION_BLOCK_INVALID")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or not 100 <= resamples <= 10000:
        raise ValueError("STRUCTURE_VALIDATION_RESAMPLES_INVALID")
    fields = ("selected_count", "selected_sum", "selected_wins", "baseline_count", "baseline_sum", "baseline_wins")
    grouped: dict[int, list[float]] = {}
    for row in rows:
        day = row.get("day")
        if not isinstance(day, int) or isinstance(day, bool):
            raise ValueError("STRUCTURE_VALIDATION_DAY_INVALID")
        values = []
        for field in fields:
            value = row.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("STRUCTURE_VALIDATION_VALUE_INVALID")
            if field.endswith(("count", "wins")) and (value < 0 or int(value) != value):
                raise ValueError("STRUCTURE_VALIDATION_COUNT_INVALID")
            values.append(float(value))
        if (values[0] > values[3] or values[2] > values[0] or values[5] > values[3]
                or values[2] > values[5] or values[0] - values[2] > values[3] - values[5]
                or (not values[0] and values[1] != 0) or (not values[3] and values[4] != 0)
                or (values[0] == values[3] and not math.isclose(values[1], values[4], rel_tol=1e-10, abs_tol=1e-12))):
            raise ValueError("STRUCTURE_VALIDATION_COHORT_INVALID")
        group = grouped.setdefault(day, [0.] * 6)
        for i, value in enumerate(values):
            group[i] += value
    span = max(grouped) - min(grouped) + 1 if grouped else 0
    totals = [sum(row[i] for row in grouped.values()) for i in range(6)]
    selected_n, selected_sum, selected_wins, baseline_n, baseline_sum, baseline_wins = totals
    selected_mean = selected_sum / selected_n if selected_n else None
    baseline_mean = baseline_sum / baseline_n if baseline_n else None
    result = {
        "version": VERSION, "method": "PAIRED_CIRCULAR_CALENDAR_BLOCK_BOOTSTRAP",
        "block_days": block_days, "calendar_days": span, "approximate_time_blocks": span // block_days,
        "selected_samples": int(selected_n), "baseline_samples": int(baseline_n),
        "selected_mean_net_return": selected_mean, "baseline_mean_net_return": baseline_mean,
        "mean_net_return_delta": selected_mean - baseline_mean if selected_mean is not None and baseline_mean is not None else None,
        "selected_net_win_rate": selected_wins / selected_n if selected_n else None,
        "baseline_net_win_rate": baseline_wins / baseline_n if baseline_n else None,
        "retention_ratio": selected_n / baseline_n if baseline_n else None,
        "mean_delta_interval": None, "selected_mean_interval": None, "selected_win_rate_interval": None,
        "confidence_level": .95, "resamples": resamples, "valid_resamples": 0,
        "sealed_holdout": False, "multiple_search_corrected": False, "portfolio_pnl": False,
        "status": "INSUFFICIENT_SAMPLES", "thresholds_enforced": False,
        "note": "按连续时间块配对重采样；描述固定规则的历史差异，不证明未来优势，不是跨多次试验校正后的认证",
    }
    if selected_n < 20 or baseline_n < 20:
        return result
    if span < 4 * block_days:
        return {**result, "status": "INSUFFICIENT_TIME_BLOCKS"}
    if span > 3660:
        return {**result, "status": "CALENDAR_BUDGET_EXCEEDED"}
    days = [grouped.get(day, [0.] * 6) for day in range(min(grouped), max(grouped) + 1)]
    # Prefix sums make each complete block O(1); no per-case sample array.
    doubled = days + days
    prefix = [[0.] * 6]
    for day in doubled:
        prefix.append([a + b for a, b in zip(prefix[-1], day, strict=True)])
    generator = random.Random(seed)
    deltas, means, win_rates = [], [], []
    for _ in range(resamples):
        remaining = span
        sampled = [0.] * 6
        while remaining:
            start = generator.randrange(span)
            length = min(block_days, remaining)
            for i in range(6):
                sampled[i] += prefix[start + length][i] - prefix[start][i]
            remaining -= length
        sn, ss, sw, bn, bs, _ = sampled
        if sn and bn:
            deltas.append(ss / sn - bs / bn)
            means.append(ss / sn)
            win_rates.append(sw / sn)
    if len(deltas) < .9 * resamples:
        return {**result, "status": "SPARSE_TIME_BLOCKS", "valid_resamples": len(deltas)}
    def interval(values: list[float]) -> dict:
        return {"lower": _quantile(values, .025), "upper": _quantile(values, .975)}
    return {**result, "status": "DESCRIPTIVE_ONLY", "valid_resamples": len(deltas),
            "mean_delta_interval": interval(deltas), "selected_mean_interval": interval(means),
            "selected_win_rate_interval": interval(win_rates)}
