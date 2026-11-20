from datetime import datetime, timedelta, timezone

import pytest

from gatescope.quant.reporting import drawdown_analysis, win_probability
from gatescope.quant.backtest import run_backtest, StrategySpec
from tests.unit.test_research_backtest import make_bars


def test_drawdown_uses_running_peak_not_initial_balance():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    curve = [{"time": (start + timedelta(hours=i)).isoformat(), "equity": v} for i, v in enumerate([100, 120, 90, 110, 130])]
    result = drawdown_analysis(curve)
    assert [p["drawdown"] for p in result["series"]] == pytest.approx([0, 0, -.25, -1 / 12, 0])
    assert result["summary"]["max_drawdown"] == .25
    assert result["summary"]["peak_time"] == curve[1]["time"]
    assert result["summary"]["trough_time"] == curve[2]["time"]
    assert result["summary"]["recovery_time"] == curve[4]["time"]
    assert result["summary"]["duration_seconds"] == 3 * 3600


def test_probability_empty_small_and_all_winning_samples_are_not_certainty():
    assert win_probability([])["win_rate"] is None
    assert win_probability([{ "net_pnl": 1 }])["interval"] is None
    interval = win_probability([{ "net_pnl": 1 }] * 20)["interval"]
    assert 0 < interval["lower"] < 1
    assert interval["upper"] <= 1
    losses = win_probability([{ "net_pnl": 0 }] * 20)
    assert losses["wins"] == 0 and losses["interval"]["upper"] > 0


def test_trial_adjustment_widens_probability_interval():
    trades = [{"net_pnl": 1}] * 50 + [{"net_pnl": -1}] * 50
    a, b = win_probability(trades), win_probability(trades, attempts=10)
    assert a["interval"]["lower"] == pytest.approx(.4038315, abs=1e-6)
    assert b["interval"]["lower"] < a["interval"]["lower"]
    assert b["interval"]["upper"] > a["interval"]["upper"]


def test_trade_and_curve_times_belong_to_frozen_candles():
    bars = make_bars(800)
    report = run_backtest(bars, StrategySpec(strategy="trend", direction="BOTH"))
    opens = {b.open_time.isoformat(): b for b in bars}
    assert report["full"]["trades"]
    for trade in report["full"]["trades"]:
        assert trade["entry_time"] in opens
        assert trade["exit_bar_open_time"] in opens
        assert trade["signal_bar_open_time"] in opens
        assert opens[trade["exit_bar_open_time"]].close_time.isoformat() == trade["exit_time"]
        assert trade["holding_seconds"] >= 3600
    assert report["drawdown"]["summary"]["max_drawdown"] == pytest.approx(report["full"]["metrics"]["max_drawdown"])
    assert report["probability"]["full"]["sample_count"] == report["full"]["metrics"]["trade_count"]
    assert report["dimensions"]["first_entry_time"] == report["full"]["trades"][0]["entry_time"]
    for p in report["full"]["equity_curve"]:
        assert p["bar_open_time"] in opens
