import copy
import json

import pytest

from gatescope.quant.structure_forecast import forecast, issue, assess, learn_formed_leg
from gatescope.adapters.annual_structure_study import advance_returns, compact
from tests.unit.test_structure_lifecycle import candidate, replay
from tests.unit.test_pattern_study import candles


def wave(prices):
    e = candidate(candles(40))
    e.update(
        pattern="elliott_impulse_bull",
        id="wave",
        points=[{"price": p, "time": i * 60, "label": str(i)} for i, p in enumerate(prices)],
    )
    e["levels"]["atr"] = 1
    return e


def test_waves_targets_obey_overlap_third_not_shortest_and_no_fabricated_pivots():
    e = wave([100, 110, 105, 125, 116])
    before = copy.deepcopy(e)
    f = forecast(e, {}, 1000, 2000, 117)
    assert f["target"]["lower"] > 125 and e == before
    e = wave([100, 120, 115, 125, 121])  # third is shorter than first: fifth cannot exceed third.
    f = forecast(e, {}, 1000, 2000, 123)
    assert f["guideline_fit"] == "STRUCTURAL_FALLBACK"
    assert 125 < f["target"]["lower"] <= f["target"]["price"] <= f["target"]["upper"] <= 131
    e = wave([100, 110, 105, 125])
    f = forecast(e, {}, 1000, 2000, 122)
    assert f["target"]["lower"] > 110


def test_history_adjustment_excludes_future_self_and_out_of_rules_samples():
    e = wave([100, 110, 105])
    cell = "elliott_impulse_bull:FORMING:3"
    training = [[900, 2.0, str(i)] for i in range(25)]
    state = {
        "forecast_training": {
            cell: training + [[1000, 2.5, "future"], [900, 2.5, "wave"], [900, 20.0, "invalid"]]
        }
    }
    f = forecast(e, state, 1000, 2000, 110)
    assert f["history_count"] == 25 and f["source"] == "PRIOR_COMPLETED_LEGS_SHRUNK"
    assert 121.18 < f["target"]["price"] < 125
    assert f == forecast(e, {"forecast_training": {cell: training}}, 1000, 2000, 110)


def test_first_prediction_survives_progress_and_formed_snapshot():
    bars = candles(30)
    state, journal = replay(bars, lambda p: [candidate(p, "FORMED" if len(p) >= 22 else "FORMING")])
    r = state["records"]["a"]
    assert (
        r["latest"]["initial_forecast"] == r["first"]["forecast"] == journal[0]["record"]["first"]["forecast"]
    )
    assert r["latest"]["pre_formation_forecast"]["issued_at"] < r["latest"]["forecast"]["issued_at"]
    assert r["latest"]["forecast"]["kind"] == "CONDITIONAL_MOVE"


def test_same_bar_invalid_touch_is_ambiguous_and_touch_before_issue_is_excluded():
    e = candidate(candles())
    r = {"first": copy.deepcopy(e), "latest": e, "deadline_time": 1000}
    issue(r, {}, 500, 100)
    assert not assess(r, {"ts": 499, "high": 106, "low": 104}, 500)
    assert assess(r, {"ts": 500, "high": 106, "low": 104}, 501, ("INVALIDATED", "INVALIDATION"))
    assert r["forecast_outcome"]["status"] == "AMBIGUOUS"


def test_stream_returns_next_open_and_missing_bars_not_guessed():
    state = {"step_seconds": 60, "facts": [["double_top", "FORMED", 0, -1, "e"]]}
    rows = advance_returns(state, {"ts": 0, "open": 100, "high": 101, "low": 99, "close": 100}, 0)
    assert len(rows) == 4 and all(r["status"] == "PENDING" for r in rows)
    for i in range(1, 6):
        rows = advance_returns(state, {"ts": i * 60, "open": 100, "high": 102, "low": 90, "close": 95}, i)
    assert rows[0]["horizon"] == 5 and rows[0]["net_return"] == pytest.approx(0.049)
    assert rows[0]["entry_time"] == 60 and rows[0]["exit_time"] == 360
    rows = advance_returns(state, {"ts": 420, "open": 100, "high": 102, "low": 90, "close": 95}, 6)
    assert len(rows) == 3 and all(r["status"] == "CENSORED" for r in rows)


def test_each_wave_leg_learns_its_own_earlier_forecast_not_only_wave_three():
    e = wave([100, 110, 105])
    r = {
        "id": e["id"],
        "first": copy.deepcopy(e),
        "latest": e,
        "deadline_time": 5000,
        "stages": {"FORMING": {"sampled": True}},
    }
    state = {}
    issue(r, state, 500, 110)
    for ts, prices in ((600, [100, 110, 105, 125]), (800, [100, 110, 105, 125, 117])):
        next_event = wave(prices)
        next_event["points"][-1]["time"] = ts
        learn_formed_leg(r, next_event, state, ts + 60)
        r["latest"] = next_event
        issue(r, state, ts + 60, prices[-1])
    assert [p[0] for p in state["pending_forecast_training"]] == [
        "elliott_impulse_bull:FORMING:3",
        "elliott_impulse_bull:FORMING:4",
    ]
    assert r["first"]["forecast"]["issued_at"] == 500


def test_year_size_hot_state_is_bounded_without_deleting_durable_outcomes():
    state = {"records": {}, "sampling": {}, "last_index": 0}
    for i in range(17000):
        state["records"][str(i)] = {"phase": "EXPIRED", "last_index": i}
        state["last_index"] = i
        compact(state)
    assert len(state["records"]) == 481 and len(json.dumps(state)) < 40000
