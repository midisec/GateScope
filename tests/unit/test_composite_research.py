from copy import deepcopy
from datetime import timedelta
from dataclasses import replace

import pytest

from gatescope.quant.backtest import StrategySpec, features, run_backtest
from gatescope.quant.research_rules import PatternReplay, combine_rules, rule_catalog
from tests.unit.test_research_backtest import make_bars


def test_single_multi_any_all_conflicts_and_direction():
    assert combine_rules({"a": 1, "b": 0}, ["a", "b"], "ANY", "BOTH") == (1, ["a"], False)
    assert combine_rules({"a": 1, "b": 0}, ["a", "b"], "ALL", "BOTH")[0] == 0
    assert combine_rules({"a": -1, "b": -1}, ["a", "b"], "ALL", "BOTH") == (-1, ["a", "b"], False)
    assert combine_rules({"a": 1, "b": -1}, ["a", "b"], "ANY", "LONG") == (0, [], True)
    assert combine_rules({"a": -1}, ["a"], "ANY", "LONG")[0] == 0


def test_selection_schema_rejects_empty_unknown_duplicates_and_impossible_all():
    assert len(rule_catalog()) == 26
    for ids in ([], ["unknown"], ["trend", "trend"], ["pattern:elliott_impulse_bull"]):
        with pytest.raises(ValueError):
            StrategySpec(strategy="composite", rule_ids=ids)
    for ids in (["trend", "reversion"], ["pattern:double_top", "pattern:double_bottom"]):
        with pytest.raises(ValueError, match="互斥"):
            StrategySpec(strategy="composite", rule_ids=ids, match_mode="ALL")


def test_recorded_vs_historical_is_explicit_and_never_mutates_input():
    original = make_bars(500)
    bars = [replace(b, available_at=original[-1].close_time + timedelta(days=2), payload_hash="") for b in original]
    strict = run_backtest(bars, StrategySpec(direction="BOTH"))
    reconstructed = run_backtest(bars, StrategySpec(direction="BOTH", availability_mode="HISTORICAL_CLOSE"))
    assert strict["full"]["metrics"]["trade_count"] == 0
    assert strict["full"]["diagnostics"]["signal_bars"] > 0
    assert strict["full"]["diagnostics"]["expired_signals"] > 0
    assert reconstructed["full"]["metrics"]["trade_count"] > 0
    assert reconstructed["input_bars"] == strict["input_bars"]
    assert reconstructed["availability"]["assumed"]
    assert reconstructed["evidence"]["decision"] == "RESEARCH_ONLY"
    assert "HISTORICAL_AVAILABILITY_ASSUMED" in reconstructed["quality_flags"]
    assert strict["artifact_digest"] != reconstructed["artifact_digest"]


def test_single_rule_compatibility_regime_filter_and_prefix_causality():
    bars = make_bars(320)
    plain = features(bars, StrategySpec(direction="BOTH"))
    spec = StrategySpec(strategy="composite", rule_ids=["trend"], direction="BOTH")
    combo = features(bars, spec)
    assert [s["side"] for s in plain] == [s["side"] for s in combo]
    assert combo[:220] == features(bars[:220], spec)
    filtered = features(bars, spec.model_copy(update={"regimes": ["UP"]}))
    assert all(not s["side"] or s["regime"] == "UP" for s in filtered)


def test_pattern_replay_freezes_first_confirmation_and_rejects_future():
    bars = make_bars(30)
    def scanner(candles, **kwargs):
        assert kwargs["confirmed_only"] and kwargs["pivot_order"] == 4
        return {"events": [{"pattern": "double_bottom", "name": "双底", "direction": "bullish", "confirmed": True,
            "confidence": .8, "start_time": candles[0]["ts"], "end_time": candles[18]["ts"],
            "points": [{"time": candles[2]["ts"], "price": 100, "label": "1"}], "lines": []}]}
    replay = PatternReplay(["double_bottom"], .65, scanner=scanner)
    events = []
    for index, bar in enumerate(bars):
        events += replay.append(bar, index, bar.close_time.isoformat())
    assert len(events) == 1 and events[0]["observed_index"] == 19
    frozen = deepcopy(events[0])
    replay.append(make_bars(31)[-1], 30, bars[-1].close_time.isoformat())
    assert events[0] == frozen
    def future(candles, **kwargs):
        result = scanner(candles, **kwargs)
        result["events"][0]["end_time"] = candles[-1]["ts"] + 3600
        return result
    replay = PatternReplay(["double_bottom"], .65, scanner=future)
    with pytest.raises(ValueError, match="future"):
        for i, bar in enumerate(bars):
            replay.append(bar, i, bar.close_time.isoformat())


def test_pattern_rule_real_scanner_prefix_and_frozen_attribution():
    bars = make_bars(320)
    spec = StrategySpec(strategy="composite", rule_ids=["trend", "pattern:double_bottom"], direction="BOTH", availability_mode="HISTORICAL_CLOSE", max_holding_bars=24)
    full = features(bars, spec)
    assert full[:240] == features(bars[:240], spec)
    report = run_backtest(bars, spec)
    assert report["selection"]["rules"][1]["name"] == "双底"
    assert report["rule_attribution"][0]["trade_count"] > 0
    ids = {e["evidence_id"] for e in report["pattern_events"]}
    for trade in report["full"]["trades"]:
        assert trade["matched_rules"]
        assert set(trade["pattern_refs"]) <= ids


def test_pattern_only_strategy_trades_with_frozen_confirmation_evidence():
    from tests.unit.test_structure_research import make_structure_bars
    report = run_backtest(make_structure_bars(500), StrategySpec(
        strategy="composite", rule_ids=["pattern:double_bottom"],
        availability_mode="HISTORICAL_CLOSE",
    ))
    events = {e["evidence_id"]: e for e in report["pattern_events"]}
    assert events and report["full"]["trades"]
    for trade in report["full"]["trades"]:
        assert trade["matched_rules"] == ["pattern:double_bottom"]
        assert trade["pattern_refs"]
        assert set(trade["pattern_refs"]) <= events.keys()
