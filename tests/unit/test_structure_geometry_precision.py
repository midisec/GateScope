"""Geometry counterexamples and preservation cases, without scanner heuristics."""
import copy
import math

import pytest

from gatescope.quant.structure_rules import audit


def rows(count=90):
    return [{"ts": 1700000000 + index * 3600, "open": 105., "high": 106.,
             "low": 104., "close": 105., "volume": 1.} for index in range(count)]


def point(candles, index, price, label):
    return {"time": candles[index]["ts"], "price": price, "label": label}


def reversal(pattern, *, forming=False):
    candles = rows()
    if "shoulders" in pattern:
        values, labels = [110, 100, 118, 101, 110], ["LS", "N1", "H", "N2", "RS"]
    elif pattern.startswith("triple"):
        values, labels = [110, 100, 110.1, 101, 110.2], ["1", "N1", "2", "N2", "3"]
    else:
        values, labels = [110, 100, 110.2], ["1", "N", "2"]
    if forming:
        values[-1], labels[-1] = 106, "2?"
    if pattern.endswith("bottom") or pattern == "inverse_head_shoulders":
        values = [240 - price for price in values]
    return candles, {"pattern": pattern, "phase": "FORMING" if forming else "FORMED",
                     "points": [point(candles, 20 + 10 * index, value, label)
                                for index, (value, label) in enumerate(zip(values, labels, strict=True))], "lines": []}


@pytest.mark.parametrize("pattern", ["double_top", "double_bottom", "triple_top", "triple_bottom",
                                     "head_shoulders", "inverse_head_shoulders"])
def test_reversal_requires_all_named_turns_before_calling_geometry_formed(pattern):
    candles, event = reversal(pattern)
    assert audit(event, candles, 1)[0] is None
    event["points"].pop()
    assert audit(event, candles, 1)[0] == "REVERSAL_ANCHOR_COUNT"


@pytest.mark.parametrize("pattern", ["double_top", "double_bottom"])
def test_provisional_second_turn_remains_forming_without_a_fabricated_peak(pattern):
    candles, event = reversal(pattern, forming=True)
    before = copy.deepcopy(event)
    assert audit(event, candles, 1)[0] is None
    assert event == before
    event["phase"] = "FORMED"
    assert audit(event, candles, 1)[0] == "REVERSAL_ANCHOR_COUNT"


@pytest.mark.parametrize("pattern", ["head_shoulders", "inverse_head_shoulders"])
def test_neckline_must_connect_the_intervening_turns_not_the_head(pattern):
    candles, event = reversal(pattern)
    event["points"][1]["label"], event["points"][2]["label"] = "H", "N1"
    assert audit(event, candles, 1)[0] == "NECKLINE_ANCHOR_ORDER"


@pytest.mark.parametrize("pattern", ["head_shoulders", "inverse_head_shoulders"])
def test_head_must_dominate_both_shoulders_but_asymmetry_is_not_a_new_cutoff(pattern):
    candles, event = reversal(pattern)
    sign = -1 if pattern.startswith("inverse") else 1
    event["points"][-1]["price"] += sign * 3
    assert audit(event, candles, 1)[0] is None
    event["points"][2]["price"] = event["points"][-1]["price"]
    assert audit(event, candles, 1)[0] == "HEAD_NOT_DOMINANT"


def boundary(pattern="uptrend_channel", *, unequal=False):
    candles = rows(100)
    # Same physical parallel lines: U(t)=110+.1*t, L(t)=100+.1*t.
    # Different endpoint spans must not change the named slope classification.
    left, right = (40, 90) if unequal else (10, 90)
    return candles, {"pattern": pattern, "region": True, "points": [], "lines": [
        {"from": point(candles, 10, 111, "U"), "to": point(candles, 90, 119, "U")},
        {"from": point(candles, left, 100 + .1 * left, "L"),
         "to": point(candles, right, 100 + .1 * right, "L")}]}


def test_boundary_audit_rejects_crossing_instead_of_only_claiming_it_checked():
    candles, event = boundary()
    event["lines"][0]["to"]["price"] = 108
    assert audit(event, candles, 1)[0] == "BOUNDARIES_CROSSED"


def test_duplicate_upper_boundary_cannot_silently_overwrite_the_first():
    candles, event = boundary()
    event["lines"].append(copy.deepcopy(event["lines"][0]))
    assert audit(event, candles, 1)[0] == "BOUNDARIES_MISSING"


@pytest.mark.parametrize("scale", [1e-9, 1., 1e8])
def test_same_parallel_geometry_is_classified_over_a_common_time_interval(scale):
    candles, event = boundary(unequal=True)
    for line in event["lines"]:
        for endpoint in ("from", "to"):
            line[endpoint]["price"] *= scale
    assert audit(event, candles, scale)[0] is None


@pytest.mark.parametrize("corruption", ["reverse", "partial_time", "disjoint", "future", "nan"])
def test_boundary_endpoints_must_define_finite_observed_overlapping_intervals(corruption):
    candles, event = boundary()
    if corruption == "reverse":
        event["lines"][0]["to"]["time"] = event["lines"][0]["from"]["time"]
    elif corruption == "partial_time":
        del event["lines"][0]["to"]["time"]
    elif corruption == "disjoint":
        event["lines"][1]["from"]["time"] = candles[91]["ts"]
        event["lines"][1]["to"]["time"] = candles[99]["ts"]
    elif corruption == "future":
        event["lines"][0]["to"]["time"] = candles[-1]["ts"] + 3600
    else:
        event["lines"][0]["to"]["price"] = math.nan
    assert audit(event, candles, 1)[0] == "BOUNDARY_GEOMETRY"


def cup(*, inverse=False, left=110., right=110., handle=107.):
    candles = rows()
    for index, row in enumerate(candles):
        if index <= 34:
            price = 100 + (left - 100) * ((index - 34) / 34) ** 2
        elif index <= 69:
            price = 100 + (right - 100) * ((index - 34) / 35) ** 2
        elif index <= 79:
            price = right + (handle - right) * (index - 69) / 10
        else:
            price = handle + (right - handle) * (index - 79) / 10
        if inverse:
            price = 240 - price
        row.update(open=price, close=price, high=price + .1, low=price - .1)
    return candles, {"pattern": "inverse_cup_handle" if inverse else "cup_handle", "lines": [],
                     "points": [point(candles, index, candles[index]["close"], label)
                                for index, label in ((0, "L"), (34, "B"), (69, "R"), (79, "H"), (89, "E"))]}


@pytest.mark.parametrize("inverse", [False, True])
def test_handle_upper_half_is_relative_to_its_right_rim_not_an_unrelated_average(inverse):
    candles, event = cup(inverse=inverse, left=112, right=110, handle=104.8)
    # Old averaged cup depth=11 admitted 5.2 retracement; actual right-side
    # cup depth=10 means this handle already lies below its upper half.
    assert audit(event, candles, 1)[0] == "CUP_HANDLE_DEPTH"


@pytest.mark.parametrize("inverse", [False, True])
def test_asymmetric_higher_right_rim_does_not_lose_valid_upper_half_handle(inverse):
    candles, event = cup(inverse=inverse, left=110, right=112, handle=106.1)
    assert audit(event, candles, 1)[0] is None


@pytest.mark.parametrize("scale", [1e-9, 1., 1e8])
@pytest.mark.parametrize("inverse", [False, True])
def test_cup_close_geometry_is_scale_invariant_and_does_not_treat_wicks_as_closes(scale, inverse):
    candles, event = cup(inverse=inverse)
    # The detector deliberately builds cups from closes. A wick beyond the
    # bowl/handle must not silently switch the model to high/low-based cups.
    candles[40].update(low=90, high=150)
    candles[72].update(low=90, high=150)
    for row in candles:
        for field in ("open", "high", "low", "close"):
            row[field] *= scale
    for item in event["points"]:
        item["price"] *= scale
    error, facts = audit(event, candles, scale)
    assert error is None
    assert facts["metrics"]["handle_depth_ratio"] == pytest.approx(.3)
    assert facts["metrics"]["handle_depth_basis"] == "RIGHT_RIM_TO_BOTTOM"


@pytest.mark.parametrize("index,reason", [(1, "CUP_BODY_EXTREME"), (3, "CUP_HANDLE_EXTREME")])
def test_cup_cannot_claim_a_lower_extreme_not_in_observed_closes(index, reason):
    candles, event = cup()
    event["points"][index]["price"] -= .1
    assert audit(event, candles, 1)[0] == reason


@pytest.mark.parametrize("inverse", [False, True])
@pytest.mark.parametrize("section,index,reason", [("body", 40, "CUP_BODY_EXTREME"),
                                                 ("handle", 72, "CUP_HANDLE_EXTREME")])
def test_cup_anchor_cannot_hide_a_deeper_observed_extreme(inverse, section, index, reason):
    candles, event = cup(inverse=inverse)
    assert audit(event, candles, 1)[0] is None
    value = 99 if section == "body" else 106
    if inverse:
        value = 240 - value
    candles[index].update(open=value, close=value, high=value + .1, low=value - .1)
    assert audit(event, candles, 1)[0] == reason


def test_audit_checks_do_not_claim_ohlc_closed_candle_or_touch_residual_validation():
    candles = rows()
    event = {"pattern": "bull_flag", "points": [], "lines": [
        {"from": {"label": label, "price": value}, "to": {"price": value - 2}}
        for label, value in (("U", 110), ("L", 100))]}
    error, facts = audit(event, candles, 1)
    assert error is None  # Retain the existing lightweight audit caller contract.
    assert facts["rules_version"] == "geometry-3.3"
    assert "真实 OHLC 锚点" not in facts["checks"]
    assert "仅使用已收盘数据" not in facts["checks"]
    assert not any("触点残差" in check for check in facts["checks"])
    assert any("时间" in warning for warning in facts["warnings"])
