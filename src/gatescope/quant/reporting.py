"""Pure report dimensions. Statistical diagnostics never grant trading eligibility."""
from __future__ import annotations

import math
from datetime import datetime
from statistics import NormalDist
from typing import Any, Sequence


def win_probability(trades: Sequence[dict[str, Any]], attempts: int = 1) -> dict[str, Any]:
    n = len(trades)
    wins = sum(t["net_pnl"] > 0 for t in trades)
    alpha = .05 / max(1, attempts)
    result = {"sample_count": n, "wins": wins, "losses_or_flat": n - wins,
        "win_rate": wins / n if n else None, "interval": None,
        "method": "Wilson score / Bonferroni attempts", "confidence_level": 1 - alpha,
        "minimum_sample": 20, "assumption": "独立同分布二项近似；交易相关性与反复调参可能使区间失真，不是下一笔盈利概率"}
    if n >= 20:
        p, z = wins / n, NormalDist().inv_cdf(1 - alpha / 2)
        denominator = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denominator
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
        result["interval"] = {"lower": max(0, center - half), "upper": min(1, center + half)}
    return result


def drawdown_analysis(curve: Sequence[dict[str, Any]], *, output: Any = None) -> dict[str, Any]:
    if not curve:
        return {"series": [], "summary": None}
    peak, peak_index, worst, trough, peak_at_worst = float(curve[0]["equity"]), 0, 0.0, 0, 0
    series = [] if output is None else output
    for index, point in enumerate(curve):
        value = float(point["equity"])
        if value >= peak:
            peak, peak_index = value, index
        depth = (peak - value) / peak if peak > 0 else 0.0
        if depth > worst:
            worst, trough, peak_at_worst = depth, index, peak_index
        series.append({**point, "peak_equity": peak, "drawdown": -depth, "drawdown_amount": peak - value})
    # The peak is invariant throughout recovery detection. Reloading its old
    # chunk per current point otherwise turns a sequential annual spill scan
    # into repeated cross-chunk decompression during an unrecovered drawdown.
    recovery_equity = curve[peak_at_worst]["equity"]
    recovery = next((i for i in range(trough + 1, len(curve)) if curve[i]["equity"] >= recovery_equity), None) if worst else None
    end = recovery if recovery is not None else len(curve) - 1
    seconds = (datetime.fromisoformat(curve[end]["time"]) - datetime.fromisoformat(curve[peak_at_worst]["time"])).total_seconds() if worst else 0
    return {"series": series, "summary": {"max_drawdown": worst,
        "peak_time": curve[peak_at_worst]["time"] if worst else None,
        "trough_time": curve[trough]["time"] if worst else None,
        "recovery_time": curve[recovery]["time"] if recovery is not None else None,
        "peak_equity": recovery_equity, "trough_equity": curve[trough]["equity"],
        "duration_seconds": seconds, "recovered": recovery is not None,
        "basis": "bar-close marked-to-market equity, net of modeled costs"}}


def trade_dimensions(trades: Sequence[dict[str, Any]], duration_seconds: float) -> dict[str, Any]:
    durations = [(datetime.fromisoformat(t["exit_time"]) - datetime.fromisoformat(t["entry_time"])).total_seconds() for t in trades]
    return {"first_entry_time": trades[0]["entry_time"] if trades else None,
        "last_exit_time": trades[-1]["exit_time"] if trades else None,
        "average_holding_seconds": sum(durations) / len(durations) if durations else None,
        "long_count": sum(t["side"] == 1 for t in trades), "short_count": sum(t["side"] == -1 for t in trades),
        "exposure_fraction": sum(durations) / duration_seconds if duration_seconds else 0,
        "average_net_pnl": sum(t["net_pnl"] for t in trades) / len(trades) if trades else None}
