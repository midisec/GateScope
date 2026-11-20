from datetime import datetime, timedelta, timezone
import math
import random

import pytest

from gatescope.quant.performance import daily_performance, performance_screen
from gatescope.quant.backtest import StrategySpec, features, run_backtest
from tests.unit.test_research_backtest import make_bars


def curve(returns):
    start, value = datetime(2024, 1, 1, tzinfo=timezone.utc), 10000.0
    points = [{"time": start.isoformat(), "equity": value}]
    for index, r in enumerate(returns, 1):
        value *= 1 + r
        points.append({"time": (start + timedelta(days=index)).isoformat(), "equity": value})
    return points


def passing():
    return dict(sharpe_ratio=1.6, max_drawdown=.14, win_rate=.56, t_statistic=2.1,
                daily_observations=100, trade_count=40)


@pytest.mark.parametrize("key,value", [("sharpe_ratio", 1.5), ("max_drawdown", .15),
    ("win_rate", .55), ("t_statistic", 2.0), ("sharpe_ratio", None),
    ("sharpe_ratio", float("nan")), ("sharpe_ratio", float("inf")), ("sharpe_ratio", 10**400), ("win_rate", 1.01),
    ("max_drawdown", -.01), ("daily_observations", 89), ("trade_count", 29), ("trade_count", None), ("daily_observations", float("nan"))])
def test_old_boundaries_and_unknown_metrics_are_observations_not_rejections(key, value):
    result = performance_screen({**passing(), key: value})
    assert result["shortlisted"] and not result["deployable"]
    assert result["status"] == "OBSERVE_ONLY"
    assert result["thresholds_enforced"] is False
    assert result["reason_codes"] == []
    assert result["observation_codes"]
    assert all(c["enforced"] is False for c in result["checks"])


def test_performance_is_reported_without_granting_execution_permission():
    assert performance_screen(passing())["shortlisted"]
    assert not performance_screen(passing())["deployable"]
    drawdown = performance_screen(passing(), full_drawdown=.16)
    assert drawdown["shortlisted"]
    assert drawdown["checks"][1]["value"] == .16
    assert not drawdown["checks"][1]["passed"]
    assert performance_screen({})["status"] == "OBSERVE_ONLY"


def test_daily_hac_formula_and_serial_correlation():
    rng, value, values = random.Random(17), 0., []
    for _ in range(300):
        value = .85 * value + rng.gauss(0, .001)
        values.append(.001 + value)
    result = daily_performance(curve(values))
    n, mean = len(values), sum(values) / len(values)
    lag = math.floor(4 * (n / 100) ** (2 / 9))
    centered = [v - mean for v in values]
    variance = sum(v*v for v in centered) / (n - 1)
    hac = sum(v*v for v in centered) / n
    for k in range(1, lag + 1):
        hac += 2 * (1 - k / (lag + 1)) * sum(centered[i]*centered[i-k] for i in range(k, n)) / n
    assert result["daily_long_run_variance"] == pytest.approx(max(variance, hac))
    assert result["sharpe_ratio"] < result["sharpe_iid"]
    assert result["t_statistic"] == pytest.approx(mean / math.sqrt(max(variance, hac)/n))
    assert result["t_statistic"] == pytest.approx(result["sharpe_ratio"] * math.sqrt(n/365))


def test_flat_days_and_partial_days_do_not_inflate_samples():
    points = curve([0., .01, 0., -.01] * 30)
    intraday = {"time": "2024-04-30T12:00:00+00:00", "equity": 100000.}
    result = daily_performance(points + [intraday])
    assert result["daily_observations"] == 120
    assert daily_performance(curve([0.] * 120))["sharpe_ratio"] is None
    assert daily_performance(curve([.01] * 120))["t_statistic"] is None


@pytest.mark.parametrize("strategy", ["pullback", "volume_breakout", "momentum"])
def test_new_strategies_are_prefix_causal_and_publish_reproducible_evidence(strategy):
    bars, spec = make_bars(), StrategySpec(strategy=strategy, direction="BOTH", availability_mode="HISTORICAL_CLOSE")
    assert features(bars, spec)[:300] == features(bars[:300], spec)
    report = run_backtest(bars, spec)
    assert report["screening"]["status"] == "OBSERVE_ONLY"
    assert not report["screening"]["sample_passed"]  # Still show short OOS windows.
    assert report["selection"]["rules"][0]["id"] == strategy
    assert daily_performance(report["validation_equity_curve"])["sharpe_ratio"] == report["validation"]["sharpe_ratio"]
