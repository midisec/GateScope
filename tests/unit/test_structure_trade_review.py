"""Independent causal/accounting regression checks from implementation review."""
import copy

import pytest

from gatescope.quant.structure_trade import advance_case, make_plan, market_context, reprice_case, start_case, summarize_cases
from gatescope.quant.structure_validation import time_block_comparison
from tests.unit.test_structure_trade import candle, plan, source


@pytest.mark.parametrize("opened,high,low,close,state,exit_price", [
    (80, 100, 10, 95, "STOP", 80),
    (130, 180, 10, 100, "TARGET", 120),
])
def test_known_open_gap_exit_does_not_charge_the_rest_of_the_bar_or_see_its_future(
    opened, high, low, close, state, exit_price,
):
    original = advance_case(start_case(plan(funding_bps_per_8h=1)), candle(3600))
    frozen = copy.deepcopy(original)
    result = advance_case(original, candle(7200, opened, high, low, close))
    assert result["state"] == state and result["raw_exit_price"] == exit_price
    assert result["exit_time"] == 7200  # The open is known, not an uncertain intrabar touch.
    assert result["holding_seconds"] == 3600
    assert result["funding"] == pytest.approx(.0001 / 8)
    assert result["mae"] >= min(frozen["mae"], (opened - 100) / 100)
    assert result["mfe"] <= max(frozen["mfe"], (opened - 100) / 100)
    assert original == frozen and result["plan"] == frozen["plan"]


def test_confirmation_on_final_permitted_close_cannot_leave_an_unfillable_pending_entry():
    event = source("FORMED")
    event["deadline_time"] = 10800
    case = start_case(make_plan(event, candle(0), {"fee_bps": 0, "slippage_bps": 0}))
    case = advance_case(case, candle(3600, high=105, close=102))
    assert case["state"] == "WAITING_CONFIRMATION"
    case = advance_case(case, candle(7200, 102, 107, 100, 104))
    assert case["state"] == "MISSED"
    assert case["entry_time"] is None and case["outcome_time"] == 10800


def test_touch_target_occurs_before_a_later_close_only_invalidation():
    case = advance_case(start_case(plan(stop_mode="STRUCTURE_INVALIDATION")), candle(3600))
    result = advance_case(case, candle(7200, 100, 125, 85, 88))
    assert result["state"] == "TARGET"
    assert result["raw_exit_price"] == 120
    assert result["ambiguous"] is False


@pytest.mark.parametrize("explicit_step", [True, False])
def test_last_candle_gap_never_extends_context_watermark_past_its_real_close(explicit_step):
    bars = [candle(i * 3600) for i in range(60)]
    bars[-1]["ts"] = 60 * 3600  # 59:00 is absent; the timeframe did not become 2h.
    if not explicit_step:
        for bar in bars:
            bar.pop("step_seconds")
    context = market_context(bars)
    assert context["as_of"] == 61 * 3600
    assert context["trend"] == "UNKNOWN"  # Don't bridge the missing observation.
    assert context["higher_timeframe"]["closed_through"] is None


@pytest.mark.parametrize("patch", [
    {"selected_wins": 1, "baseline_wins": 0},
    {"selected_wins": 0, "baseline_wins": 2},
    {"baseline_count": 1, "baseline_wins": 1, "baseline_sum": .01},
    {"selected_count": 0, "selected_wins": 0},
    {"selected_count": 0, "selected_wins": 0, "selected_sum": .02,
     "baseline_count": 0, "baseline_wins": 0, "baseline_sum": .02},
])
def test_paired_selected_cases_must_be_an_actual_subset_of_the_baseline(patch):
    row = {"day": 0, "selected_count": 1, "selected_sum": .02, "selected_wins": 1,
           "baseline_count": 2, "baseline_sum": .04, "baseline_wins": 2, **patch}
    with pytest.raises(ValueError, match="STRUCTURE_VALIDATION"):
        time_block_comparison([row])


@pytest.mark.parametrize("side", [1, -1])
def test_double_costs_hold_original_decisions_risk_and_immutable_evidence(side):
    original = advance_case(start_case(plan(side=side, fee_bps=5, slippage_bps=5,
                                           funding_bps_per_8h=1)), candle(3600))
    original = advance_case(original, candle(7200, 100, 125 if side == 1 else 105,
                                            95 if side == 1 else 75, 100))
    frozen = copy.deepcopy(original)
    stressed = reprice_case(original, 2)
    assert original == frozen
    assert stressed["state"] == original["state"] and stressed["plan"] == original["plan"]
    assert stressed["raw_entry_price"] == original["raw_entry_price"]
    assert stressed["raw_exit_price"] == original["raw_exit_price"]
    assert stressed["net_return"] < original["net_return"]
    assert stressed["costs_return"] == pytest.approx(stressed["slippage_return"] + stressed["fees"] + stressed["funding"])
    risk = abs(original["entry_price"] - original["plan"]["stop_price"]) / original["entry_price"]
    assert stressed["net_r"] == pytest.approx(stressed["net_return"] / risk)


def test_pending_unavailable_missed_and_censored_cases_do_not_inflate_win_denominator():
    win = advance_case(start_case(plan()), candle(3600, high=125))
    rows = [win, *[{"state": name, "net_return": None} for name in
                  ("OPEN", "WAITING_ENTRY", "WAITING_CONFIRMATION", "UNAVAILABLE", "MISSED", "CENSORED")]]
    summary = summarize_cases(rows)
    assert summary["sample_count"] == 1 and summary["candidate_count"] == 7
    assert summary["net_win_rate"] == 1 and summary["interval"] is None
    assert summary["pending_count"] == 3 and summary["censored_count"] == 1
