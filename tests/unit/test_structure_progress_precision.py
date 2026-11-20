"""Same-bar observations must not bypass newly knowable path constraints."""
import copy

import pytest

from gatescope.quant.structure_detection import known_points, proposals
from gatescope.quant.structure_lifecycle import completion_summary, observe, resolve_rule


def _bars(count=25):
    return [{"ts": i * 60, "open": 115., "high": 115.2, "low": 114.8,
             "close": 115., "volume": 1.} for i in range(count)]

def _wave(bars, *, count=3, phase="FORMING", boundary=100., hard_limits=None):
    prices = [100., 110., 105., 125., 116., 132.]
    points = [{"time": bars[i]["ts"], "price": p, "label": str(n)}
              for n, (i, p) in enumerate(zip((2, 5, 8, 11, 14, 17), prices[:count], strict=False))]
    levels = {"expiry_bars": 96, "closes": 2, "buffer": .15, "atr": 1.,
              "invalid_side": -1, "invalid_price": boundary, "hard": True}
    if hard_limits is not None:
        levels["hard_limits"] = hard_limits
    if phase == "FORMED":
        levels["trigger"] = {"from": {"time": 0, "price": 128.},
                             "to": {"time": bars[-1]["ts"], "price": 128.}}
    return {"id": "wave", "pattern": "elliott_impulse_bull", "name": "上升五浪",
            "group": "波浪理论", "phase": phase, "confidence": None if phase == "FORMING" else .8,
            "signal_direction": "neutral" if phase == "FORMING" else "bearish",
            "points": points, "lines": [], "start_time": points[0]["time"],
            "end_time": bars[-1]["ts"], "levels": levels}


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("inclusive", [None, True, False])
def test_extra_hard_limits_use_wicks_and_honor_exact_equality(side, inclusive):
    bars = _bars()
    limit = {"price": 115., "side": side}
    if inclusive is not None:
        limit["inclusive"] = inclusive
    r = {"latest": _wave(bars, hard_limits=[limit]), "phase": "FORMING", "deadline_index": 100}
    bar = {"ts": 1500, "high": 115. if side > 0 else 116.,
           "low": 115. if side < 0 else 114., "close": 114. if side > 0 else 116.}
    expected = None if inclusive is False else ("INVALIDATED", "INVALIDATION")
    assert resolve_rule(r, bar, 25) == expected
    bar["high" if side > 0 else "low"] += side * .01
    assert resolve_rule(r, bar, 25) == ("INVALIDATED", "INVALIDATION")


def test_two_sided_hard_limits_and_existing_soft_rule_remain_independent():
    e = _wave(_bars(), hard_limits=[{"price": 90., "side": -1}, {"price": 130., "side": 1}])
    e["levels"].update(invalid_side=1, invalid_price=120., hard=False)
    r = {"latest": e, "phase": "FORMING", "deadline_index": 100}
    assert resolve_rule(r, {"ts": 1500, "high": 125., "low": 110., "close": 115.}, 25) is None
    for bar in ({"ts": 1500, "high": 130., "low": 110., "close": 115.},
                {"ts": 1500, "high": 125., "low": 90., "close": 115.}):
        assert resolve_rule(r, bar, 25) == ("INVALIDATED", "INVALIDATION")


@pytest.mark.parametrize("phase,count", [("FORMING", 3), ("FORMED", 6)])
def test_already_invalid_admission_is_journaled_without_backfilled_prediction(phase, count):
    bars = _bars()
    bars[19].update(high=130., low=109., close=115.)
    event = _wave(bars[:20], count=count, phase=phase, hard_limits=[{"price": 110., "side": -1}])
    state = {"step_seconds": 60}
    journal = observe(bars, 19, state, detector=lambda _: [copy.deepcopy(event)])
    record = state["records"]["wave"]
    assert [j["record"]["reason"] for j in journal] == ["ADMITTED", "INVALIDATION"]
    assert [j["phase"] for j in journal] == [phase, "INVALIDATED"]
    assert record["resolved_time"] == 1200 and record["resolved_index"] == 19
    assert not state["active"]
    assert record["first"] == journal[0]["record"]["first"]
    assert journal[0]["record"]["phase"] == phase
    assert record["first"]["forecast"]["issued_at"] == 1200
    assert record["forecast_outcome"]["status"] not in {"TOUCHED", "AMBIGUOUS", "NOT_REACHED"}
    rows, samples = completion_summary(state)
    row = next(row for row in rows if (row["pattern"], row["phase"], row["score_bucket"])
               == ("elliott_impulse_bull", phase, "ALL"))
    assert (row["sample_count"], row["successes"], row["INVALIDATED"]) == (1, 0, 1)
    assert len(samples) == 1 and samples[0][5] == 0
    # Failure survives recovery and a detector re-emitting the same identity.
    assert observe(bars, 20, state, detector=lambda _: [copy.deepcopy(event)]) == []
    assert state["records"]["wave"]["sequence"] == 2
    assert list(state["training"].values()) == [[1, 0]]


@pytest.mark.parametrize("phase,count", [("FORMING", 4), ("FORMED", 6)])
def test_progress_boundary_and_initial_target_touch_same_bar_are_ambiguous(phase, count):
    bars = _bars()
    state = {"step_seconds": 60}
    original = observe(bars, 19, state, detector=lambda p: [_wave(p)])
    frozen = copy.deepcopy(original)
    bars[20].update(high=122., low=109., close=115.)
    journal = observe(bars, 20, state, detector=lambda p: [
        _wave(p, count=count, phase=phase, hard_limits=[{"price": 110., "side": -1}])])
    record = state["records"]["wave"]
    assert [j["record"]["reason"] for j in journal] == ["PROGRESS" if phase == "FORMING" else "FORMED", "INVALIDATION"]
    assert record["phase"] == "INVALIDATED" and record["resolved_time"] == 1260
    assert record["forecast_outcome"]["status"] == "AMBIGUOUS"
    assert journal[-1]["record"]["latest"]["forecast_outcome"]["status"] == "AMBIGUOUS"
    assert original == frozen and record["first"] == frozen[0]["record"]["first"]
    assert record["latest"]["forecast"]["issued_at"] == 1260
    assert record["first"]["forecast"]["issued_at"] == 1200
    assert record["latest"]["initial_forecast"] == record["first"]["forecast"]
    assert not state["active"]
    _, samples = completion_summary(state)
    assert len(samples) == (2 if phase == "FORMED" else 1)
    assert all(sample[5] == 0 for sample in samples)
    assert len(state["pending_training"]) == len(samples)


def test_touch_on_an_earlier_bar_is_not_reclassified_by_later_progress_failure():
    bars = _bars()
    state = {"step_seconds": 60}
    observe(bars, 19, state, detector=lambda p: [_wave(p)])
    bars[20].update(high=122., low=115., close=120.)
    observe(bars, 20, state, detector=lambda _: [])
    touched = copy.deepcopy(state["records"]["wave"]["forecast_outcome"])
    assert touched["status"] == "TOUCHED" and touched["observed_time"] == 1260
    bars[21].update(high=114., low=109., close=113.)
    observe(bars, 21, state, detector=lambda p: [_wave(p, count=4, boundary=110.)])
    assert state["records"]["wave"]["phase"] == "INVALIDATED"
    assert state["records"]["wave"]["forecast_outcome"] == touched


def test_existing_boundary_failure_cannot_be_replaced_by_a_looser_new_geometry():
    bars = _bars()
    state = {"step_seconds": 60}
    observe(bars, 19, state, detector=lambda p: [_wave(p)])
    bars[20].update(high=122., low=99., close=115.)
    journal = observe(bars, 20, state, detector=lambda p: [_wave(p, count=4, boundary=90.)])
    assert [j["phase"] for j in journal] == ["INVALIDATED"]
    assert len(known_points(state["records"]["wave"]["latest"])) == 3
    assert len(state["pending_training"]) == 1


def test_real_detector_wave_three_confirmation_cannot_hide_wave_four_overlap():
    bars = _bars(45)
    vertices = [(0, 105.), (5, 100.), (15, 110.), (25, 105.), (35, 125.), (39, 109.), (44, 115.)]
    for (a, pa), (b, pb) in zip(vertices, vertices[1:], strict=False):
        for i in range(a, b + 1):
            price = pa + (pb - pa) * (i - a) / (b - a)
            bars[i].update(open=price, close=price, high=price + .01, low=price - .01)
    state = {"step_seconds": 60}
    journal = []
    for index in range(19, 41):
        journal.extend(observe(bars, index, state, detector=proposals))
    record = next(r for r in state["records"].values() if r["pattern"] == "elliott_impulse_bull")
    assert record["phase"] == "INVALIDATED" and record["resolved_index"] == 39
    assert record["latest"]["levels"]["invalid_price"] == pytest.approx(110.01)
    assert bars[39]["low"] == pytest.approx(108.99)
    transitions = [j for j in journal if j["candidate_id"] == record["id"] and j["observed_time"] == 2400]
    assert [j["record"]["reason"] for j in transitions] == ["PROGRESS", "INVALIDATION"]
    assert record["first"] == next(j for j in journal if j["candidate_id"] == record["id"])["record"]["first"]
