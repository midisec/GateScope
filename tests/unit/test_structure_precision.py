"""Closed-prefix and target geometry regressions, not a win-rate backtest."""
import copy

import pytest

from gatescope.quant.pattern_study import strict_impulse
from gatescope.quant.structure_detection import VERSION, proposals, refine, value_at
from gatescope.quant.structure_forecast import forecast
from gatescope.quant.structure_lifecycle import resolve_rule
from tests.unit.test_structure_forecast import wave
from tests.unit.test_structure_rules import pivot_case


@pytest.mark.parametrize("mutation,reason", [
    ({"is_final": False}, "UNCONFIRMED_CANDLE"),
    ({"close": float("nan")}, "INVALID_OHLC"),
    ({"high": float("inf")}, "INVALID_OHLC"),
    ({"low": 99999}, "INVALID_OHLC"),
    ({"open": -1}, "INVALID_OHLC"),
    ({"volume": -1}, "INVALID_VOLUME"),
    ({"ts": True}, "INVALID_CANDLE_ORDER"),
])
def test_detector_rejects_bad_input_before_normalization(mutation, reason):
    bars, raw = pivot_case("double_top")
    bars[-1].update(mutation)
    diagnostics = {}
    with pytest.raises(ValueError, match=f"STRUCTURE_{reason}"):
        proposals(bars, diagnostics=diagnostics)
    assert diagnostics["input_error"] == reason
    assert refine(raw, bars) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_causal_prefix_is_not_silently_sorted_or_deduplicated(reverse):
    bars, _ = pivot_case("double_top")
    if reverse:
        bars[-1], bars[-2] = bars[-2], bars[-1]
    else:
        bars[-1]["ts"] = bars[-2]["ts"]
    with pytest.raises(ValueError, match="STRUCTURE_INVALID_CANDLE_ORDER"):
        proposals(bars)


@pytest.mark.parametrize("bullish", [True, False])
@pytest.mark.parametrize("scale", [1e-8, 1., 1e8])
@pytest.mark.parametrize("prices", [[100,120,115,125,121], [100,120,115,121], [100,110,105,150,120]])
def test_legal_wave_targets_survive_non_fibonacci_proportions(prices, bullish, scale):
    values = [(v if bullish else 300-v)*scale for v in prices]
    event = wave(values)
    event["pattern"] = "elliott_impulse_bull" if bullish else "elliott_impulse_bear"
    result = forecast(event, {}, 1000, 2000, values[-1])
    assert result["guideline_fit"] == "STRUCTURAL_FALLBACK"
    target = result["target"]
    assert target and target["lower"] <= target["price"] <= target["upper"]
    for price in (target["lower"], target["price"], target["upper"]):
        assert strict_impulse([*values, price], bullish, guidelines=False)
    assert "不代表命中概率" in result["condition"]
    assert result["history_count"] == 0


@pytest.mark.parametrize("key", ["head_shoulders", "inverse_head_shoulders"])
def test_sloped_neckline_height_and_trigger_use_the_actual_candle_coordinate(key):
    bars, raw = pivot_case(key)
    event = refine(raw, bars)
    before = copy.deepcopy(event)
    issued = bars[-1]["ts"] + 3600
    result = forecast(event, {}, issued, issued+86400, bars[-1]["close"])
    head = next(p for p in event["points"] if p["label"] == "H")
    neckline = event["levels"]["trigger"]
    height = abs(head["price"]-value_at(neckline, head["time"]))
    reference = value_at(neckline, bars[-1]["ts"])
    side = -1 if key == "head_shoulders" else 1
    assert result["target"]["price"] == pytest.approx(reference+side*height)
    assert result["confirmation"][0]["price"] == pytest.approx(reference+side*event["levels"]["buffer"])
    assert result["confirmation_basis_time"] == bars[-1]["ts"]
    assert result["issued_at"] == issued
    assert event == before


def test_detector_semantic_change_has_a_separate_version():
    assert VERSION == "structure-study-3.3"


def test_only_executed_geometry_checks_are_advertised():
    bars, raw = pivot_case("double_top")
    event = refine(raw, bars)
    assert "真实 OHLC 锚点" in event["recognition"]["checks"]
    assert "已收盘输入契约与 OHLC 一致性" in event["recognition"]["checks"]
    assert event["recognition"]["rules_version"] == "geometry-3.3"


@pytest.mark.parametrize("bullish", [True, False])
def test_refined_short_third_wave_exposes_and_enforces_the_same_fifth_limit(bullish):
    key = "elliott_impulse_bull" if bullish else "elliott_impulse_bear"
    bars, raw = pivot_case(key)
    prices = [100.,120.,115.,125.,121.]
    prices = prices if bullish else [300-p for p in prices]
    raw.update(phase="FORMING", confidence=None, points=raw["points"][:5], lines=[])
    for point, price in zip(raw["points"], prices, strict=True):
        point["price"] = price
        bar = next(b for b in bars if b["ts"] == point["time"])
        bar.update(open=price, close=price, high=price, low=price)
    last_index = next(i for i,b in enumerate(bars) if b["ts"] == raw["points"][-1]["time"])
    bars = bars[:last_index+6]
    event = refine(raw, bars)
    assert event is not None
    limit = event["levels"]["hard_limits"][0]
    assert limit == {"price":131. if bullish else 169., "side":1 if bullish else -1,
                     "inclusive":False, "rule":"WAVE3_NOT_SHORTEST"}
    line = next(line for line in event["lines"] if line["role"] == "invalidation")
    assert line["from"]["price"] == line["to"]["price"] == limit["price"]
    record = {"latest":event, "phase":"FORMING", "deadline_index":999}
    bound = limit["price"]
    observed = {"ts":bars[-1]["ts"]+3600, "high":bound if bullish else 175.,
                "low":125. if bullish else bound, "close":128. if bullish else 172.}
    assert resolve_rule(record, observed, 100) is None  # equality is legal
    observed["high" if bullish else "low"] += .1 * limit["side"]
    assert resolve_rule(record, observed, 100) == ("INVALIDATED", "INVALIDATION")


@pytest.mark.parametrize("bullish", [True, False])
def test_refined_cycle_retains_origin_as_a_model_path_constraint(bullish):
    key = "elliott_cycle_bull" if bullish else "elliott_cycle_bear"
    bars, raw = pivot_case(key)
    raw.update(phase="FORMING", confidence=None, points=raw["points"][:8], lines=[])
    last_index = next(i for i,b in enumerate(bars) if b["ts"] == raw["points"][-1]["time"])
    event = refine(raw, bars[:last_index+6])
    assert event is not None
    limit = event["levels"]["hard_limits"][0]
    assert limit["price"] == raw["points"][0]["price"]
    assert limit["side"] == (-1 if bullish else 1)
    assert limit["rule"] == "CYCLE_RETAINS_ORIGIN"
