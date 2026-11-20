"""Versioned, cost-net performance observations; never execution permission."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Sequence

POLICY_VERSION = "performance-observation-2"
METRIC_VERSION = "oos-performance-1.0.0"
MIN_DAYS, MIN_TRADES = 90, 30
THRESHOLDS = (
    ("OOS_SHARPE", "sharpe_ratio", "夏普比率", ">", 1.5),
    ("OOS_DRAWDOWN", "max_drawdown", "最大回撤", "<", 0.15),
    ("OOS_HIT_RATE", "win_rate", "命中率", ">", 0.55),
    ("OOS_T_STAT", "t_statistic", "t 统计量", ">", 2.0),
)


def daily_performance(curve: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Complete UTC days including flat days; never interpolate.

    Bartlett/Newey-West L=floor(4*(n/100)^(2/9)), variance floored at sample
    variance. Conservative HAC risk score, not exact finite-horizon annual SR.
    """
    midnight = []
    for point in curve:
        stamp = datetime.fromisoformat(point["time"]).astimezone(timezone.utc)
        if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0):
            midnight.append((stamp, float(point["equity"])))
    returns = [
        right[1] / left[1] - 1
        for left, right in zip(midnight, midnight[1:], strict=False)
        if (right[0] - left[0]).total_seconds() == 86400 and left[1] > 0
    ]
    n = len(returns)
    result: dict[str, Any] = {
        "sharpe_ratio": None,
        "sharpe_iid": None,
        "t_statistic": None,
        "daily_observations": n,
        "hac_lags": 0,
        "daily_mean_return": None,
        "daily_long_run_variance": None,
        "metric_version": METRIC_VERSION,
        "return_basis": "complete-UTC-daily-net-MTM",
        "risk_free_daily": 0.0,
        "annualization_days": 365,
        "variance_method": "Newey-West-Bartlett-with-sample-variance-floor",
    }
    if n < 2 or any(not math.isfinite(r) for r in returns):
        return result
    mean = math.fsum(returns) / n
    centered = [r - mean for r in returns]
    variance = math.fsum(r * r for r in centered) / (n - 1)
    lags = min(n - 1, math.floor(4 * (n / 100) ** (2 / 9)))
    hac = math.fsum(r * r for r in centered) / n + 2 * math.fsum(
        (1 - lag / (lags + 1)) * math.fsum(centered[i] * centered[i - lag] for i in range(lag, n)) / n
        for lag in range(1, lags + 1)
    )
    conservative = max(variance, hac)
    result.update(daily_mean_return=mean, daily_long_run_variance=conservative, hac_lags=lags)
    if variance <= 1e-24 or not math.isfinite(conservative):
        return result
    result.update(
        sharpe_ratio=mean / math.sqrt(conservative) * math.sqrt(365),
        sharpe_iid=mean / math.sqrt(variance) * math.sqrt(365),
        t_statistic=mean / math.sqrt(conservative / n),
    )
    return result


def performance_screen(validation: dict[str, Any], *, full_drawdown: float | None = None) -> dict[str, Any]:
    checks = []
    for code, key, label, operator, threshold in THRESHOLDS:
        raw = validation.get(key)
        value: float | None = None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                value = float(raw) if math.isfinite(raw) else None
            except OverflowError:
                value = None
        if key == "max_drawdown" and full_drawdown is not None:
            value = max(value, full_drawdown) if value is not None and math.isfinite(full_drawdown) else None
        if value is not None and key in {"max_drawdown", "win_rate"} and not 0 <= value <= 1:
            value = None
        passed = value is not None and (value > threshold if operator == ">" else value < threshold)
        checks.append(
            {
                "gate_code": code,
                "metric": key,
                "label": label,
                "value": value,
                "operator": operator,
                "threshold": threshold,
                "passed": passed,
                "enforced": False,
            }
        )
    days, trades = validation.get("daily_observations", 0), validation.get("trade_count", 0)
    enough = type(days) is int and type(trades) is int and days >= MIN_DAYS and trades >= MIN_TRADES
    observations = [c["gate_code"] for c in checks if not c["passed"]] + (
        [] if enough else ["OOS_SAMPLE_SIZE"]
    )
    return {
        "policy_version": POLICY_VERSION,
        "status": "OBSERVE_ONLY",
        "thresholds_enforced": False,
        "shortlisted": True,
        "deployable": False,
        "checks": checks,
        "sample_passed": enough,
        "minimum_daily_observations": MIN_DAYS,
        "minimum_trades": MIN_TRADES,
        "daily_observations": validation.get("daily_observations", 0),
        "trade_count": validation.get("trade_count", 0),
        "reason_codes": [],
        "observation_codes": observations,
        "basis": "绩效仅供观察，不再按夏普、回撤、命中率、t 统计量或关联样本门槛淘汰；回撤取全区间与验证区间较大值；不授予交易权限",
    }
