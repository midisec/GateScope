"""The same frozen plan must settle identically in replay and forward shadows."""
import copy
import json

import pytest

from gatescope.quant.artifacts import report_digest
from gatescope.quant.structure_trade import (
    advance_case, make_plan, market_context, normalize_policy, plan_from_notice,
    reprice_case, start_case, summarize_cases,
)


def candle(ts, opened=100., high=105., low=95., close=100., *, step=3600, volume=100.):
    return dict(ts=ts, open=opened, high=high, low=low, close=close, volume=volume, step_seconds=step)


def source(phase="CONFIRMED", side=1):
    edge = {"from": {"time": 0, "price": 100.}, "to": {"time": 0, "price": 100.}}
    return {"id": "candidate-1", "evidence_id": "evidence-1", "detector_version": "structure-study-3.2",
            "pattern": "double_bottom" if side > 0 else "double_top", "name": "test shape", "phase": phase,
            "signal_direction": "bullish" if side > 0 else "bearish", "observed_time": 3600,
            "deadline_time": 100000, "confidence": .8,
            "levels": {"trigger": edge, "buffer": 0., "closes": 2, "invalid_side": -side,
                       "invalid_price": 90. if side > 0 else 110., "hard": False},
            "forecast": {"id": "forecast-1", "kind": "CONDITIONAL_MOVE", "issued_at": 3600,
                         "target": {"price": 120. if side > 0 else 80.}, "source": "THEORY_ONLY"}}


def plan(phase="CONFIRMED", side=1, **policy):
    return make_plan(source(phase, side), candle(0), {
        "fee_bps": 0, "slippage_bps": 0, "funding_bps_per_8h": 0, **policy,
    })


@pytest.mark.parametrize("field,value", [("fee_bps", -1), ("fee_bps", float("nan")),
    ("slippage_bps", True), ("funding_bps_per_8h", 101), ("timeframe_seconds", 0),
    ("max_holding_bars", False), ("entry_delay_seconds", -1), ("stop_mode", "OPTIMISTIC")])
def test_invalid_policy_rejected(field, value):
    with pytest.raises(ValueError):
        normalize_policy({field: value})


def test_daily_ten_bar_delay_is_a_supported_research_policy():
    from gatescope.quant.backtest import StrategySpec
    from gatescope.quant.structure_research import policy_from_spec
    policy = normalize_policy(policy_from_spec(StrategySpec(entry_delay_bars=10), 86400))
    assert policy["entry_delay_seconds"] == 10 * 86400


def test_plan_is_frozen_from_tg_notice_and_cannot_authorize_trading():
    event = source()
    original = copy.deepcopy(event)
    value = make_plan(event, candle(0))
    assert value["status"] == "READY" and value["stop_price"] == 90 and value["target_price"] == 120
    assert not value["auto_order"] and not value["notice"]["entry"]["executable"]
    assert event == original
    snapshot = {k: v for k, v in value.items() if k != "digest"}
    assert value["digest"] == report_digest(snapshot)
    event["forecast"]["target"]["price"] = 500
    assert value["target_price"] == 120 and value["event"]["forecast"]["target"]["price"] == 120


def test_missing_or_wrong_side_plan_is_recorded_not_fabricated():
    value = plan_from_notice({"phase": "CONFIRMED"})
    case = start_case(value)
    assert case["state"] == "UNAVAILABLE" and case["net_return"] is None
    event = source()
    event["forecast"]["target"]["price"] = 80
    assert make_plan(event, candle(0))["status"] == "UNAVAILABLE"


def test_target_then_costs_and_normalized_r():
    value = plan(fee_bps=5, slippage_bps=5, funding_bps_per_8h=1)
    case = advance_case(start_case(value), candle(3600))
    assert case["state"] == "OPEN" and case["entry_price"] == pytest.approx(100.05)
    case = advance_case(case, candle(7200, 110, 122, 108, 119))
    assert case["state"] == "TARGET" and case["exit_price"] == pytest.approx(119.94)
    assert case["holding_seconds"] == 7200
    assert case["funding"] == pytest.approx(.0001 * 2 / 8)
    assert case["net_return"] == pytest.approx(case["gross_return"] - case["fees"] - case["funding"])
    assert case["costs_return"] == pytest.approx(case["slippage_return"] + case["fees"] + case["funding"])
    assert case["raw_gross_return"] == pytest.approx(.2)
    assert case["net_r"] == pytest.approx(case["net_return"] / ((100.05 - 90) / 100.05))
    assert reprice_case(case, 2)["net_return"] < case["net_return"]
    assert case["plan"] == value


@pytest.mark.parametrize("side", [1, -1])
def test_same_bar_ambiguity_is_stop_first_for_both_sides(side):
    case = advance_case(start_case(plan(side=side)), candle(3600, 100, 125, 75, 100))
    assert case["state"] == "STOP" and case["ambiguous"] is True
    assert case["net_return"] == pytest.approx(-.1)


def test_open_gap_order_is_known_and_does_not_invent_favorable_fill():
    case = advance_case(start_case(plan()), candle(3600))
    stopped = advance_case(case, candle(7200, 80, 100, 70, 95))
    assert stopped["state"] == "STOP" and stopped["raw_exit_price"] == 80
    target = advance_case(case, candle(7200, 130, 140, 80, 100))
    assert target["state"] == "TARGET" and target["raw_exit_price"] == 120
    assert not target["ambiguous"]


def test_gap_outside_plan_before_entry_is_a_missed_case_not_a_trade():
    case = advance_case(start_case(plan()), candle(3600, 130, 140, 125, 135))
    assert case["state"] == "MISSED" and case["entry_time"] is None and case["net_return"] is None


def test_delivery_clock_cannot_use_an_already_open_candle():
    case = start_case(plan(), available_at=3601, source="FORWARD_SHADOW")
    case = advance_case(case, candle(3600, high=125))
    assert case["state"] == "WAITING_ENTRY" and case["entry_time"] is None
    case = advance_case(case, candle(7200))
    assert case["entry_time"] == 7200 and case["state"] == "OPEN"


def test_minute_execution_holds_for_original_hour_budget_not_minutes():
    case = start_case(plan(max_holding_bars=1), available_at=3601)
    for ts in range(3660, 7200, 60):
        case = advance_case(case, candle(ts, step=60))
        assert case["state"] == "OPEN"
    case = advance_case(case, candle(7200, step=60))
    assert case["state"] == "TIMEOUT" and case["holding_seconds"] == 3600


def test_expired_delivery_cannot_extend_original_deadline():
    value = plan()
    case = advance_case(start_case(value, available_at=110000), candle(111600))
    assert case["state"] == "MISSED" and case["reason"] == "SIGNAL_AVAILABLE_AFTER_DEADLINE"
    assert start_case(value, available_at=110000)["state"] == "MISSED"


def test_gap_censors_and_repeated_bar_does_not_double_count():
    case = advance_case(start_case(plan()), candle(3600))
    assert advance_case(case, candle(3600, high=130)) == case
    censored = advance_case(case, candle(10800, high=130))
    assert censored["state"] == "CENSORED" and censored["net_return"] is None
    assert case["state"] == "OPEN"


def test_checkpoint_restart_matches_uninterrupted_replay_and_forward_resolution():
    bars = [candle(3600), candle(7200, 105, 109, 102, 107), candle(10800, 110, 125, 105, 120)]
    uninterrupted = start_case(plan())
    restarted = start_case(plan(), available_at=3600, source="FORWARD_SHADOW")
    for b in bars:
        uninterrupted = advance_case(uninterrupted, b)
        restarted = advance_case(json.loads(json.dumps(restarted)), b)
    for key in ("state", "entry_time", "entry_price", "exit_time", "exit_price", "net_return", "net_r"):
        assert restarted[key] == uninterrupted[key]


def test_formed_waits_for_two_original_closes_then_next_open():
    case = start_case(plan("FORMED"))
    case = advance_case(case, candle(3600, high=105, close=102))
    assert case["state"] == "WAITING_CONFIRMATION"
    case = advance_case(case, candle(7200, 102, 107, 100, 104))
    assert case["state"] == "WAITING_ENTRY" and case["entry_time"] is None
    case = advance_case(case, candle(10800, 104, 108, 101, 105))
    assert case["state"] == "OPEN" and case["entry_time"] == 10800


def test_formed_fails_and_lower_timeframe_does_not_fake_confirmation():
    failed = advance_case(start_case(plan("FORMED")), candle(3600, 100, 105, 85, 89))
    assert failed["state"] == "MISSED" and failed["reason"] == "PRE_ENTRY_INVALIDATION"
    wrong_tf = advance_case(start_case(plan("FORMED")), candle(3600, step=60))
    assert wrong_tf["state"] == "CENSORED" and wrong_tf["reason"] == "WRONG_CONFIRMATION_TIMEFRAME"


def test_structure_close_stop_is_distinct_from_price_touch_stop():
    case = advance_case(start_case(plan(stop_mode="STRUCTURE_INVALIDATION")), candle(3600, 100, 105, 85, 100))
    assert case["state"] == "OPEN"
    case = advance_case(case, candle(7200, 100, 105, 85, 88))
    assert case["state"] == "STOP" and case["raw_exit_price"] == 88
    assert case["reason"] == "STRUCTURE_CLOSE_INVALIDATION"
    wrong_tf = advance_case(start_case(plan(stop_mode="STRUCTURE_INVALIDATION")), candle(3600, step=60))
    assert wrong_tf["state"] == "CENSORED" and wrong_tf["reason"] == "ORIGINAL_CLOSE_TIMEFRAME_REQUIRED"


def test_summary_includes_losses_but_not_missing_or_pending_results():
    won = advance_case(start_case(plan()), candle(3600, high=125))
    lost = advance_case(start_case(plan()), candle(3600, low=85))
    unknown = start_case(plan_from_notice({}))
    pending = advance_case(start_case(plan()), candle(3600))
    summary = summarize_cases([won, lost, unknown, pending])
    assert summary["candidate_count"] == 4 and summary["sample_count"] == 2
    assert summary["net_win_rate"] == .5 and summary["stop_first_rate"] == .5
    assert summary["mean_net_return"] == pytest.approx(.05)
    assert summary["pending_count"] == 1 and summary["interval"] is None
    assert not summary["real_execution"] and not summary["thresholds_enforced"]
    empty = summarize_cases([])
    assert empty["net_win_rate"] is None and empty["mean_net_return"] is None


def test_higher_timeframe_uses_only_completed_aligned_candles_and_gaps_warm_up():
    bars = [candle(i * 3600, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(240)]
    result = market_context(bars)
    assert result["trend"] == "UP" and result["higher_timeframe"]["trend"] == "UP"
    partial = market_context(bars[:239])
    assert partial["higher_timeframe"]["closed_through"] == 236 * 3600
    assert partial["higher_timeframe"]["closed_through"] <= partial["as_of"]
    changed = copy.deepcopy(bars[:239])
    changed[-1].update(close=50000, high=50000)
    assert market_context(changed)["higher_timeframe"] == partial["higher_timeframe"]
    gap = market_context(bars[:200] + bars[201:])
    assert gap["source_bars"] == 39 and gap["trend"] == "UNKNOWN"


def test_future_forecast_not_admitted_to_frozen_plan():
    event = source()
    event["forecast"]["issued_at"] = event["observed_time"] + 1
    value = make_plan(event, candle(0))
    assert value["status"] == "UNAVAILABLE" and "FORECAST_TIME_INVALID" in value["reason_codes"]
