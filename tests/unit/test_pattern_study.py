import copy
import json
import math

import pytest

from gatescope.quant.pattern_study import adapt_event, catalog, detect, digest, forming_waves, observe, strict_impulse, summarize


def candles(n=100):
    return [{"ts": 1700000000 + i * 3600, "open": 100., "high": 105., "low": 95., "close": 101., "volume": 100.} for i in range(n)]


def event(bars, *, confirmed=False):
    points = [{"time": bars[i]["ts"], "price": p, "label": str(i)} for i, p in ((2, 105.), (8, 95.), (14, 105.))]
    raw = {"pattern": "double_top", "name": "双顶", "direction": "bearish", "signal_direction": "bearish",
        "confidence": .8, "confirmed": confirmed, "start_time": points[0]["time"], "end_time": points[-1]["time"],
        "points": points, "lines": [{"from": a, "to": b, "style": "solid"} for a, b in zip(points, points[1:])]}
    if confirmed:
        raw["points"] = [*points, {"time": bars[-1]["ts"], "price": bars[-1]["close"], "label": "✓"}]
    return {**adapt_event(raw, bars), "evidence_id": digest(raw)}


def test_first_observation_not_pivot_and_phase_never_rewritten():
    bars = candles()
    state = {}
    detector = lambda prefix: [event(prefix, confirmed=len(prefix) >= 28)]
    observe(bars, 19, state, detector=detector)
    frozen = copy.deepcopy(state["facts"])
    for i in range(20, 40):
        observe(bars, i, state, detector=detector)
    assert len(state["facts"]) == 2
    assert state["facts"][0] == frozen[0]
    assert state["facts"][0][1:3] == ["FORMED", 19]
    assert state["facts"][1][1:3] == ["CONFIRMED", 27]
    assert json.loads(json.dumps(state)) == state  # Durable checkpoint roundtrip.


def test_future_suffix_does_not_change_prefix_observation():
    original = candles()
    alternate = copy.deepcopy(original)
    for bar in alternate[35:]:
        bar["close"] *= 3
    def replay(bars):
        state = {}
        for i in range(19, 35):
            observe(bars, i, state, detector=lambda p: [event(p, confirmed=len(p) > 26)])
        return state
    assert replay(original) == replay(alternate)


def test_strict_standard_impulse_rules_and_reversal_not_top_buy():
    assert strict_impulse([100, 120, 110, 142, 130, 150], True)
    assert strict_impulse([200, 180, 190, 158, 170, 150], False)
    assert not strict_impulse([100, 120, 110, 124, 121, 150], True)  # Third shortest.
    assert not strict_impulse([100, 120, 110, 142, 119, 150], True)  # Overlap.
    assert not strict_impulse([100, 120, 99], True)
    bars = candles(40)
    for b in bars:
        b["close"] = 149
    points = [{"time": bars[i * 4]["ts"], "price": p, "label": str(i)} for i, p in enumerate([100, 120, 110, 142, 130, 150])]
    raw = {**event(bars), "pattern": "elliott_impulse_bull", "direction": "bullish", "confirmed": True,
        "points": points, "start_time": points[0]["time"], "end_time": points[-1]["time"]}
    formed = adapt_event(raw, bars[:25])
    assert formed["phase"] == "FORMED" and formed["signal_direction"] == "bearish"
    bars[27]["close"] = 144
    confirmed = adapt_event(raw, bars[:30])
    assert confirmed["phase"] == "CONFIRMED"
    assert confirmed["points"][-1]["time"] == bars[27]["ts"]


def test_forward_returns_costs_short_denominator_and_exclusions():
    bars = candles(60)
    bars[20]["open"] = 100
    bars[24]["close"] = 90
    state = {"facts": [["double_top", "CONFIRMED", i, side, str(i)] for i, side in ((19, -1), (20, -1), (55, -1), (30, 0))]}
    report = summarize(bars, state, {"step_seconds": 3600})
    row = next(r for r in report["rows"] if r["pattern"] == "double_top" and r["phase"] == "CONFIRMED" and r["horizon"] == 5)
    assert row["sample_count"] == 1
    assert row["mean_net_return"] == pytest.approx(.099)
    assert row["pending"] == row["overlap_excluded"] == row["neutral_excluded"] == 1
    assert row["recent_samples"][0]["entry_time"] == bars[20]["ts"]
    assert row["recent_samples"][0]["observed_time"] == bars[19]["ts"] + 3600
    assert row["interval"] is None
    empty = next(r for r in report["rows"] if r["pattern"] == "elliott_cycle_bull")
    assert empty["win_rate"] is None and empty["sample_count"] == 0
    assert len(report["rows"]) == len(catalog()) * 2 * 4


def test_forming_is_not_counted_and_full_catalog_available():
    state = {}
    observe(candles(), 19, state, detector=lambda p: [{"phase": "FORMING"}])
    assert not state["facts"]
    assert len(catalog()) == 28 and sum(p["family"] == "wave" for p in catalog()) == 6


def test_actual_detector_geometry_uses_known_times_only():
    bars = candles(180)
    for i, b in enumerate(bars):
        b.update(open=100+9*math.sin(i/8), close=100+9*math.sin(i/8), high=101+9*math.sin(i/8), low=99+9*math.sin(i/8))
    events = detect(bars, partial=True)
    assert events
    times = {b["ts"] for b in bars}
    assert len({e["id"] for e in events}) == len(events)
    for e in events:
        for point in e["points"]:
            assert point["time"] in times
        for line in e["lines"]:
            assert line["from"]["time"] in times and line["to"]["time"] in times


def test_partial_impulse_only_labels_known_legs_and_no_predicted_target():
    bars = candles(51)
    vertices = [(0, 110), (5, 100), (15, 120), (25, 110), (35, 142), (45, 130), (50, 135)]
    for (a, pa), (b, pb) in zip(vertices, vertices[1:]):
        for i in range(a, b + 1):
            price = pa + (pb - pa) * (i - a) / (b - a)
            bars[i].update(open=price, close=price, high=price + .1, low=price - .1)
    wave = next(e for e in forming_waves(bars) if e["pattern"] == "elliott_impulse_bull")
    assert wave["phase"] == "FORMING" and wave["confidence"] is None
    assert [p["label"] for p in wave["points"]] == ["0", "1", "2", "3", "4"]
    assert max(p["time"] for p in wave["points"]) == bars[45]["ts"]
    assert all(line["style"] == "dashed" for line in wave["lines"])


def test_interim_report_does_not_evaluate_unreplayed_future():
    bars = candles(100)
    state = {"facts": [["double_top", "CONFIRMED", 24, -1, "frozen"]]}
    interim = summarize(bars[:27], state, {"step_seconds": 3600})
    row = next(r for r in interim["rows"] if r["pattern"] == "double_top" and r["phase"] == "CONFIRMED" and r["horizon"] == 5)
    assert row["pending"] == 1 and row["sample_count"] == 0 and row["win_rate"] is None
    assert interim["to_time"] == bars[26]["ts"] + 3600
