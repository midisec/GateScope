import copy
import json
from decimal import Decimal

import pytest

from gatescope.application.pattern_trade_notice import build_pattern_trade_notice, render_pattern_trade_notice


def edge(first_price, last_price=None, *, start=100, end=200):
    return {
        "from": {"time": start, "price": first_price},
        "to": {"time": end, "price": first_price if last_price is None else last_price},
    }


def event(*, phase="CONFIRMED", direction="bullish"):
    bull = direction == "bullish"
    return {
        "id": "a" * 64,
        "context_id": "b" * 64,
        "evidence_id": "c" * 64,
        "detector_version": "structure-study-3.2",
        "pattern": "double_bottom" if bull else "double_top",
        "name": "双底" if bull else "双顶",
        "signal_direction": direction,
        "phase": phase,
        "confidence": .81,
        "observed_time": 360,
        "levels": {
            "trigger": edge(99 if bull else 101),
            "invalid_price": 95 if bull else 105,
            "invalid_side": -1 if bull else 1,
            "buffer": 1,
            "closes": 2,
        },
        "forecast": {
            "id": "d" * 64,
            "kind": "CONDITIONAL_MOVE",
            "issued_at": 250,
            "source": "THEORY_ONLY",
            "target": {"price": 112 if bull else 88, "lower": 110 if bull else 88, "upper": 112 if bull else 90},
        },
    }


BAR = {"ts": 300, "close": 100.0}


@pytest.mark.parametrize("direction,stop,target,comparison", [
    ("bullish", 94, 112, "CLOSE_ABOVE"),
    ("bearish", 106, 88, "CLOSE_BELOW"),
])
def test_confirmed_plan_uses_frozen_direction_close_buffer_and_target(direction, stop, target, comparison):
    notice = build_pattern_trade_notice(event(direction=direction), BAR)
    assert notice["status"] == "READY"
    assert notice["entry"] == {"price": 100, "basis": "CONFIRMATION_CLOSE", "executable": False}
    assert notice["stop"]["price"] == stop
    assert notice["stop"]["mode"] == "CLOSE_INVALIDATION"
    assert notice["stop"]["exchange_order_placed"] is False
    assert notice["target"]["price"] == target
    assert notice["reward_risk_ratio"] == 2
    assert notice["confirmation"]["prices"][0]["comparison"] == comparison
    assert notice["auto_order"] is False
    assert "未扣费用" in render_pattern_trade_notice(notice)


def test_formed_plan_remains_waiting_even_when_close_passes_confirmation():
    notice = build_pattern_trade_notice(event(phase="FORMED"), {"ts": 300, "close": 101})
    assert notice["status"] == "WAIT_CONFIRMATION"
    assert notice["entry"]["basis"] == "OBSERVATION_CLOSE"
    assert notice["reward_risk_ratio"] is None
    text = render_pattern_trade_notice(notice)
    assert "非入场信号" in text and "连续 2 根收盘高于 100" in text
    assert "仍须等待突破确认" in text


def test_original_inputs_are_not_mutated_and_only_evidence_references_are_retained():
    source, bar = event(), {"ts": 300, "close": Decimal("100")}
    before = copy.deepcopy(source)
    notice = build_pattern_trade_notice(source, bar)
    assert source == before and bar["close"] == Decimal("100")
    assert json.loads(json.dumps(notice, allow_nan=False)) == notice
    assert notice["evidence"] == {
        "candidate_id": "a" * 64, "context_id": "b" * 64,
        "evidence_id": "c" * 64, "detector_version": "structure-study-3.2", "forecast_id": "d" * 64,
    }
    assert "levels" not in notice and "lines" not in notice


def test_sloped_invalidation_and_confirmation_use_event_candle_open_not_future_time():
    source = event()
    source["levels"]["invalid_edge"] = edge(90, 92)
    source["levels"]["trigger"] = edge(95, 96)
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["stop"]["boundary_price"] == 94
    assert notice["stop"]["price"] == 93
    assert notice["confirmation"]["prices"][0]["price"] == 98
    assert notice["stop"]["projected_at"] == 300


def test_hard_invalidation_uses_wick_rule_without_atr_buffer():
    source = event()
    source["levels"]["hard"] = True
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["stop"]["price"] == 95
    assert notice["stop"]["mode"] == "WICK_INVALIDATION"
    assert "触价失效" in render_pattern_trade_notice(notice)


@pytest.mark.parametrize("phase", ["FORMING", "INVALIDATED", "EXPIRED", "CENSORED", "UNKNOWN"])
def test_other_phases_never_offer_a_ready_plan(phase):
    notice = build_pattern_trade_notice(event(phase=phase), BAR)
    assert notice["status"] == "UNAVAILABLE"
    assert "PHASE_NOT_ACTIONABLE" in notice["reason_codes"]


def test_neutral_pattern_cannot_borrow_structural_direction_or_fabricate_stop():
    source = event(direction="neutral")
    source["direction"] = "bullish"
    source["levels"].pop("invalid_side")
    source["levels"].pop("trigger")
    source["levels"].update(upper=edge(105), lower=edge(95))
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["direction"] == "neutral" and notice["stop"] is None
    assert notice["status"] == "UNAVAILABLE" and notice["reward_risk_ratio"] is None
    assert [item["side"] for item in notice["confirmation"]["prices"]] == [1, -1]


@pytest.mark.parametrize("patch,reason", [
    ({"kind": "NEXT_PIVOT"}, "TARGET_UNAVAILABLE"),
    ({"issued_at": 361}, "FORECAST_TIME_INVALID"),
    ({"target": {"price": 99}}, "TARGET_NOT_PROFIT_SIDE"),
    ({"target": {"price": 100}}, "TARGET_NOT_PROFIT_SIDE"),
    ({"target": {"price": 112, "lower": 113, "upper": 110}}, "TARGET_UNAVAILABLE"),
    ({"target": None}, "TARGET_UNAVAILABLE"),
])
def test_invalid_or_next_pivot_forecasts_never_become_profit_targets(patch, reason):
    source = event()
    source["forecast"].update(patch)
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["status"] == "UNAVAILABLE" and notice["target"] is None
    assert reason in notice["reason_codes"] and notice["reward_risk_ratio"] is None


@pytest.mark.parametrize("patch", [{"invalid_price": 101}, {"invalid_price": 100, "buffer": 0}, {"invalid_side": 1}])
def test_invalid_risk_direction_or_zero_risk_does_not_create_a_trade(patch):
    source = event()
    source["levels"].update(patch)
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["status"] == "UNAVAILABLE" and notice["stop"] is None
    assert "STOP_DIRECTION_INVALID" in notice["reason_codes"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), True, 0, -1, "100", 10**1000])
def test_invalid_prices_are_json_safe_and_never_ready(invalid):
    source = event()
    source["levels"]["invalid_price"] = invalid
    source["forecast"]["target"]["price"] = invalid
    notice = build_pattern_trade_notice(source, {"ts": 300, "close": invalid})
    assert notice["status"] == "UNAVAILABLE"
    assert notice["entry"]["price"] is None and notice["stop"] is None and notice["target"] is None
    json.dumps(notice, allow_nan=False)


@pytest.mark.parametrize("timestamp", [None, True, -1, float("nan"), 300.5, 360, 999999999999999])
def test_missing_or_later_reference_candle_is_not_ready(timestamp):
    notice = build_pattern_trade_notice(event(), {"ts": timestamp, "close": 100})
    assert notice["status"] == "UNAVAILABLE"
    assert any(reason.startswith("REFERENCE_") for reason in notice["reason_codes"])


def test_future_anchors_and_missing_confirmation_are_not_invented():
    source = event()
    source["levels"]["invalid_edge"] = edge(90, 91, end=400)
    source["levels"].pop("trigger")
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["confirmation"] is None and notice["stop"] is None
    assert "CONFIRMATION_UNAVAILABLE" in notice["reason_codes"]


def test_geometry_is_not_probability_or_a_new_performance_gate():
    source = event()
    source["confidence"] = .01
    notice = build_pattern_trade_notice(source, BAR)
    text = render_pattern_trade_notice(notice)
    assert notice["status"] == "READY" and notice["geometry_score"] == .01
    assert notice["geometry_is_probability"] is False
    assert "几何匹配 1 分（非胜率 / 概率）" in text
    assert "不是准入门槛" in text and "尚未下单" in text
    assert "实际保护单需另行设置" in text and len(text) < 1024


def test_missing_data_is_a_short_unavailable_message_not_an_exception():
    notice = build_pattern_trade_notice({}, {})
    assert notice["status"] == "UNAVAILABLE"
    text = render_pattern_trade_notice(notice)
    assert "待核对" in text and "尚未下单" in text and len(text) < 1024
    json.dumps(notice, allow_nan=False)


@pytest.mark.parametrize("pattern,structure_direction,trade_direction", [
    ("elliott_impulse_bull", "bullish", "bearish"),
    ("elliott_impulse_bear", "bearish", "bullish"),
])
def test_completed_wave_uses_explicit_trade_direction_not_pattern_suffix(pattern, structure_direction, trade_direction):
    source = event(direction=trade_direction)
    source.update(pattern=pattern, direction=structure_direction)
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["pattern"] == pattern
    assert notice["direction"] == trade_direction
    assert notice["status"] == "READY"
    assert (notice["target"]["price"] > notice["entry"]["price"]) == (trade_direction == "bullish")


@pytest.mark.parametrize("direction", ["LONG", "bull", "up_trend", "bullish_signal", None])
def test_unknown_direction_aliases_do_not_silently_become_long_or_short(direction):
    source = event()
    source["signal_direction"] = direction
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["direction"] == "neutral" and notice["status"] == "UNAVAILABLE"
    assert "DIRECTION_UNAVAILABLE" in notice["reason_codes"]


def test_small_positive_reward_risk_is_reported_not_filtered_by_a_new_gate():
    source = event()
    source["forecast"]["target"] = {"price": 100.01}
    notice = build_pattern_trade_notice(source, BAR)
    assert notice["status"] == "READY"
    assert 0 < notice["reward_risk_ratio"] < .01
    assert "不是准入门槛" in render_pattern_trade_notice(notice)
