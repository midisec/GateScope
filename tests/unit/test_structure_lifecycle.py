import copy
import json
import math

import pytest

from gatescope.quant.structure_detection import VERSION, identity, proposals
from gatescope.quant.structure_lifecycle import observe, completion_summary, historical_reference, probability
from tests.unit.test_pattern_study import candles


def candidate(bars, phase="FORMING", identifier="a", expiry=8):
    points = [
        {"time": bars[i]["ts"], "price": p, "label": label} for i, p, label in ((2, 105.0, "1"), (8, 95.0, "N"))
    ]
    return {
        "id": identifier,
        "pattern": "double_top",
        "name": "双顶",
        "group": "反转",
        "phase": phase,
        "confidence": None if phase == "FORMING" else 0.8,
        "signal_direction": "neutral" if phase == "FORMING" else "bearish",
        "points": points,
        "lines": [],
        "start_time": points[0]["time"],
        "end_time": bars[-1]["ts"],
        "levels": {
            "expiry_bars": expiry,
            "closes": 2,
            "buffer": 0.2,
            "invalid_side": 1,
            "invalid_price": 106.0,
            "trigger": {"from": points[1], "to": points[1]},
        },
    }


def replay(bars, detector, through=None):
    state = {"step_seconds": 3600}
    journal = []
    for i in range(19, through or len(bars)):
        journal.extend(observe(bars, i, state, detector=detector, snapshot_at="2026-09-10T00:00:00+00:00"))
    return state, journal


def test_disappearance_expires_and_failure_never_resurrects():
    bars = candles(38)
    state, journal = replay(bars, lambda p: [candidate(p)] if len(p) in (20, 35) else [])
    r = state["records"]["a"]
    assert r["phase"] == "EXPIRED" and r["resolved_index"] == 27
    lifecycle = [j for j in journal if j["record"]["reason"] != "FORECAST_OBSERVED"]
    assert len(lifecycle) == 2 and journal[0]["record"]["phase"] == "FORMING"
    rows, _ = completion_summary(state)
    row = next(
        r for r in rows if (r["pattern"], r["phase"], r["score_bucket"]) == ("double_top", "FORMING", "ALL")
    )
    assert row["sample_count"] == 1 and row["successes"] == 0 and row["EXPIRED"] == 1
    assert json.loads(json.dumps(state)) == state


def test_progress_frozen_origin_confirmation_two_closes_and_invalid_first():
    bars = candles(30)
    bars[22]["close"] = 94.0
    bars[23]["close"] = 94.9
    bars[24]["close"] = 94.0
    bars[25]["close"] = 94.0
    def detector(p):
        return [candidate(p, "FORMED" if len(p) >= 22 else "FORMING")]
    state, journal = replay(bars, detector)
    r = state["records"]["a"]
    assert r["phase"] == "CONFIRMED" and r["resolved_index"] == 25
    assert r["first"]["phase"] == "FORMING" and r["deadline_index"] == 27
    assert [j["phase"] for j in journal if j["record"]["reason"] != "FORECAST_OBSERVED"] == [
        "FORMING",
        "FORMED",
        "CONFIRMED",
    ]
    bars[24]["close"] = 107.0
    state, _ = replay(bars, detector)
    assert state["records"]["a"]["phase"] == "INVALIDATED"


def test_gap_censors_not_failure_and_does_not_change_expected_period():
    bars = candles(30)
    for bar in bars[22:]:
        bar["ts"] += 3600
    sizes = []

    def detector(p):
        sizes.append(len(p))
        return [candidate(p)] if len(p) == 20 else []

    state, journal = replay(bars, detector)
    assert state["records"]["a"]["phase"] == "CENSORED"
    assert state["step_seconds"] == 3600 and sizes[3:5] == [1, 2]
    rows, training = completion_summary(state)
    assert not training and sum(r["sample_count"] for r in rows) == 0


def test_nonoverlap_sampling_and_prequential_prediction_exclude_same_bar():
    bars = candles(32)
    key = "double_top:FORMING:UNSCORED"
    state = {"step_seconds": 3600, "training": {key: [20, 12]}}
    for i in range(19, 30):
        observe(bars, i, state, detector=lambda p: [candidate(p, identifier=str(len(p)), expiry=2)])
    records = state["records"]
    assert records["20"]["stages"]["FORMING"]["sampled"]
    assert not records["21"]["stages"]["FORMING"]["sampled"]
    assert not records["22"]["stages"]["FORMING"]["sampled"]
    assert records["23"]["stages"]["FORMING"]["prediction"]["sample_count"] == 21
    assert records["22"]["stages"]["FORMING"]["prediction"]["sample_count"] == 20
    assert records["20"]["stages"]["FORMING"]["prediction"]["probability"] == 0.6


def test_history_reference_strict_time_model_and_self_exclusion():
    e = candidate(candles())
    samples = [["double_top", "FORMING", "UNSCORED", 100, 200, int(i < 13), str(i)] for i in range(20)]
    report = {"version": VERSION, "to_time": 999, "training_samples": samples}
    assert historical_reference(e, report, 200)["sample_count"] == 0
    reference = historical_reference(e, report, 201)
    assert reference["sample_count"] == 20 and reference["rate"] == 0.65 and reference["interval"]
    assert historical_reference({**e, "id": "0"}, report, 201)["sample_count"] == 19
    assert historical_reference(e, {**report, "version": "old"}, 201)["sample_count"] == 0
    assert probability(0, 0)["rate"] is None and probability(0, 20)["rate"] == 0


def test_prefix_causality_and_stable_identity_without_decimal_rounding():
    bars = candles(40)
    changed = copy.deepcopy(bars)
    for b in changed[25:]:
        b["close"] *= 5
    def detector(p):
        return [candidate(p)]
    assert replay(bars, detector, 25) == replay(changed, detector, 25)
    e = candidate(bars)
    original = identity(e)
    e["points"].append({"time": bars[30]["ts"], "price": 105.0, "label": "2"})
    assert identity(e) == original
    e["points"][0]["price"] += 1e-10
    assert identity(e) != original


def test_real_detector_exact_ohlc_anchors_and_no_future_times():
    bars = candles(175)
    for i, b in enumerate(bars):
        p = 100 + 9 * math.sin(i / 8)
        b.update(open=p, close=p, high=p + 1, low=p - 1)
    events = proposals(bars)
    assert events
    by_time = {b["ts"]: b for b in bars}
    for e in events:
        for p in e["points"]:
            assert p["price"] == pytest.approx(by_time[p["time"]][p["basis"].lower()])
        assert e["phase"] != "CONFIRMED"
        assert e["levels"]["buffer"] > 0


def test_volatility_normalized_detection_respects_price_scale():
    bars = candles(175)
    for i, b in enumerate(bars):
        p = 100 + 9 * math.sin(i / 8)
        b.update(open=p, close=p, high=p + 1, low=p - 1)
    scaled = copy.deepcopy(bars)
    for b in scaled:
        for k in ("open", "high", "low", "close"):
            b[k] *= 1e-5
    a = sorted(proposals(bars), key=lambda e: (e["pattern"], e["start_time"]))
    b = sorted(proposals(scaled), key=lambda e: (e["pattern"], e["start_time"]))
    assert [(e["pattern"], e["phase"]) for e in a] == [(e["pattern"], e["phase"]) for e in b]
    for one, two in zip(a, b, strict=False):
        assert one["confidence"] == pytest.approx(two["confidence"])
        assert one["levels"]["buffer"] * 1e-5 == pytest.approx(two["levels"]["buffer"])


def test_small_percentage_abc_can_progress_under_same_atr_rules():
    bars = candles(50)
    vertices = [(0, 99.8), (5, 100.0), (15, 99.6), (25, 99.8), (35, 99.4), (49, 99.75)]
    for (a, pa), (b, pb) in zip(vertices, vertices[1:], strict=False):
        for i in range(a, b + 1):
            p = pa + (pb - pa) * (i - a) / (b - a)
            bars[i].update(open=p, close=p, high=p + 0.001, low=p - 0.001)
    partial = next(
        e
        for e in proposals(bars[:31])
        if e["pattern"] == "elliott_correction_bull" and e["phase"] == "FORMING"
    )
    complete = next(
        e
        for e in proposals(bars[:40])
        if e["pattern"] == "elliott_correction_bull" and e["phase"] == "FORMED"
    )
    assert partial["id"] == complete["id"]
    assert (
        abs(partial["points"][1]["price"] - partial["points"][0]["price"]) / partial["points"][0]["price"]
        < 0.025
    )
    state, _ = replay(bars, proposals)
    record = state["records"][partial["id"]]
    assert record["phase"] == "CONFIRMED"
    assert set(record["stages"]) == {"FORMING", "FORMED"}
