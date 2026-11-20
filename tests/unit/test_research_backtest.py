from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math

import pytest

from gatescope.domain.market import Bar
from gatescope.quant.backtest import StrategySpec, features, run_backtest, simulate


def make_bars(count=500, delay=0):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(count):
        price = 100 + i * .03 + 5 * math.sin(i / 10)
        opened = start + timedelta(hours=i)
        bars.append(Bar("GATE", "FUTURES_USDT", "BTC", "1h", opened, opened + timedelta(hours=1),
                        Decimal(str(price)), Decimal(str(price + 1)), Decimal(str(price - 1)),
                        Decimal(str(price + .2)), Decimal(10), True, 1,
                        opened + timedelta(hours=1, seconds=delay)))
    return bars


def test_future_prices_do_not_change_prefix_signals():
    bars = make_bars()
    spec = StrategySpec()
    assert features(bars, spec)[:250] == features(bars[:250], spec)


def test_delayed_data_cannot_fill_at_next_open_and_costs_balance():
    bars = make_bars(8, delay=2)
    signals = [{"side": 0, "values": {"atr.atr": 1.0}} for _ in bars]
    signals[0]["side"] = 1
    spec = StrategySpec(max_holding_bars=1)
    result = simulate(bars, signals, spec, 0, len(bars))
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == bars[2].open_time.isoformat()
    assert datetime.fromisoformat(trade["signal_available_at"]) <= datetime.fromisoformat(trade["entry_time"])
    assert result["metrics"]["net_pnl"] == pytest.approx(trade["net_pnl"])
    assert trade["fees"] > 0 and trade["funding"] > 0


def test_same_bar_stop_and_target_uses_stop():
    bars = make_bars(5)
    b = bars[1]
    bars[1] = Bar(b.venue, b.market, b.instrument_id, b.timeframe, b.open_time, b.close_time,
                  b.open, Decimal(150), Decimal(50), b.close, b.volume, True, 1, b.available_at)
    signals = [{"side": 0, "values": {"atr.atr": 1.0}} for _ in bars]
    signals[0]["side"] = 1
    report = simulate(bars, signals, StrategySpec(), 0, len(bars))
    assert report["trades"][0]["exit_reason"] == "STOP"
    assert report["trades"][0]["net_pnl"] < 0


def test_gap_rejected_and_report_is_deterministic_and_uncertified():
    bars = make_bars()
    spec = StrategySpec(strategy="breakout", direction="BOTH")
    a = run_backtest(bars, spec)
    b = run_backtest(bars, spec)
    assert a["artifact_digest"] == b["artifact_digest"]
    assert len(a["input_bars"]) == len(bars)
    assert a["input_bars"][0]["payload_hash"] == bars[0].payload_hash
    assert a["input_bars"][0]["open"] == str(bars[0].open)
    assert a["evidence"]["decision"] == "RESEARCH_ONLY"
    assert a["evidence"]["sealed_holdout"] is False
    assert a["full"]["metrics"]["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in a["full"]["trades"]))
    with pytest.raises(ValueError, match="不连续"):
        run_backtest(bars[:250] + bars[251:], spec)


def test_flat_prices_do_not_manufacture_trade_probability():
    bars = make_bars()
    flat = [Bar(b.venue, b.market, b.instrument_id, b.timeframe, b.open_time, b.close_time,
                Decimal(100), Decimal(100), Decimal(100), Decimal(100), b.volume,
                True, 1, b.available_at) for b in bars]
    report = run_backtest(flat, StrategySpec())
    assert report["full"]["metrics"]["trade_count"] == 0
    assert report["full"]["metrics"]["win_rate"] is None
    assert report["evidence"]["bootstrap_mean_lcb"] is None


def test_expired_signal_does_not_open_late():
    bars = make_bars(8, delay=7201)
    signals = [{"side": 0, "values": {"atr.atr": 1.0}} for _ in bars]
    signals[0]["side"] = 1
    report = simulate(bars, signals, StrategySpec(), 0, len(bars))
    assert report["trades"] == []


def test_repeated_trials_are_recorded_and_limit_statistical_claims():
    report = run_backtest(make_bars(), StrategySpec(), tested_hypotheses=51)
    assert report["evidence"]["tested_hypotheses"] == 51
    assert report["evidence"]["bootstrap_mean_lcb"] is None
    assert report["evidence"]["positive_validation_evidence"] is False
