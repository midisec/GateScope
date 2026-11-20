"""Current read-only screens retain the lifecycle's fixed wave boundaries."""

import copy

import pytest

from gatescope.quant.structure_lifecycle import resolve_rule
from gatescope.quant.structure_screening import usable


def _record(*limits, phase="FORMING", scale=1):
    return {
        "phase": phase,
        "observed_time": 10000,
        "deadline_time": 20000,
        "deadline_index": 100,
        "latest": {
            "signal_direction": "bullish",
            "levels": {
                "invalid_side": -1,
                "invalid_price": 50 * scale,
                "buffer": scale,
                "hard_limits": list(limits),
            },
        },
    }


def _bar(*, high=120, low=100, close=110):
    return {"ts": 10800, "high": high, "low": low, "close": close}


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("inclusive", [None, True, False])
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("scale", [1e-300, 1, 1e300])
def test_screen_and_lifecycle_agree_on_exact_wick_boundaries(side, inclusive, offset, scale):
    limit = {"side": side, "price": 110 * scale}
    if inclusive is not None:
        limit["inclusive"] = inclusive
    record = _record(limit, scale=scale)
    wick = (110 + side * offset) * scale
    bar = _bar(high=wick if side > 0 else 120 * scale, low=wick if side < 0 else 100 * scale)
    bar["close"] = (bar["high"] + bar["low"]) / 2
    before = copy.deepcopy((record, bar))
    invalid = offset > 0 or (offset == 0 and inclusive is not False)
    assert resolve_rule(record, bar, 1) == (("INVALIDATED", "INVALIDATION") if invalid else None)
    assert usable(record, bar, 11000, 3600) is not invalid
    assert (record, bar) == before


@pytest.mark.parametrize("phase", ["FORMING", "FORMED", "CONFIRMED"])
def test_fixed_boundaries_veto_each_active_phase_without_confirming_or_mutating(phase):
    record = _record({"side": 1, "price": 119, "inclusive": False}, phase=phase)
    bar = _bar()
    before = copy.deepcopy((record, bar))
    assert not usable(record, bar, 11000, 3600)
    assert (record, bar) == before


def test_multiple_limits_are_all_applied_independently_of_soft_close_buffer():
    limits = [{"side": -1, "price": 100}, {"side": 1, "price": 120, "inclusive": False}]
    for ordered in (limits, list(reversed(limits))):
        record = _record(*ordered)
        assert usable(record, _bar(high=120, low=101), 11000, 3600)
        # Recovering by the close does not undo a hard wick violation.
        assert not usable(record, _bar(high=120, low=100), 11000, 3600)
        assert not usable(record, _bar(high=121, low=101), 11000, 3600)
        assert not usable(record, _bar(high=121, low=99), 11000, 3600)


@pytest.mark.parametrize("bad", [None, {}, "", 1, True, (), [None], [1], [[]], [{}]])
def test_malformed_limit_arrays_and_entries_fail_closed(bad):
    record = _record()
    record["latest"]["levels"]["hard_limits"] = bad
    assert not usable(record, _bar(), 11000, 3600)


@pytest.mark.parametrize("bad", [None, True, False, "121", 0, -1, float("nan"), float("inf"), -float("inf"), 10**1000])
def test_invalid_boundary_prices_fail_closed(bad):
    assert not usable(_record({"side": 1, "price": bad}), _bar(), 11000, 3600)


@pytest.mark.parametrize("bad", [None, True, False, "1", 0, 2, -2, float("nan"), float("inf")])
def test_invalid_sides_fail_closed(bad):
    assert not usable(_record({"side": bad, "price": 121}), _bar(), 11000, 3600)


@pytest.mark.parametrize("bad", [None, 0, 1, "false", [], {}])
def test_inclusive_flag_must_be_a_boolean_when_present(bad):
    assert not usable(_record({"side": 1, "price": 121, "inclusive": bad}), _bar(), 11000, 3600)


@pytest.mark.parametrize("field", ["high", "low"])
@pytest.mark.parametrize("bad", [None, True, "110", 0, -1, float("nan"), float("inf"), -float("inf"), 10**1000])
def test_invalid_wick_values_fail_closed(field, bad):
    bar = _bar()
    bar[field] = bad
    limit = {"side": 1 if field == "high" else -1, "price": 121 if field == "high" else 99}
    assert not usable(_record(limit), bar, 11000, 3600)


@pytest.mark.parametrize("bar", [None, {}, {"low": 100, "close": 110, "ts": 10800}])
def test_missing_required_wicks_fail_closed(bar):
    assert not usable(_record({"side": 1, "price": 121}), bar, 11000, 3600)


@pytest.mark.parametrize("side,unused", [(1, "low"), (-1, "high")])
def test_extra_rule_only_validates_its_required_wick(side, unused):
    bar = _bar()
    del bar[unused]
    record = _record({"side": side, "price": 121 if side > 0 else 99})
    assert usable(record, bar, 11000, 3600)


def test_legacy_levels_and_screen_thresholds_remain_unchanged():
    record = _record()
    assert usable(record, _bar(), 11000, 3600)
    del record["latest"]["levels"]["hard_limits"]
    assert usable(record, _bar(), 11000, 3600)
    assert not usable(record, _bar(low=49, close=49), 11000, 3600)
    assert not usable(record, _bar(), 20000, 3600)
    record["phase"] = "CONFIRMED"
    assert usable(record, _bar(), 17200, 3600)
    assert not usable(record, _bar(), 17201, 3600)


def test_intrabar_wicks_may_veto_but_cannot_change_lifecycle():
    record = _record({"side": 1, "price": 121, "inclusive": False})
    bar = {**_bar(high=122), "is_final": False}
    before = copy.deepcopy(record)
    assert not usable(record, bar, 11000, 3600)
    assert record == before
