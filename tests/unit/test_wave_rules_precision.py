"""Hard wave rules and causal input guards, without Fibonacci admission gates."""
import copy
import math

import pytest

from gatescope.quant.pattern_study import forming_waves, strict_impulse


IMPULSE = [100.0, 120.0, 110.0, 160.0, 140.0, 175.0]


@pytest.mark.parametrize("length", [3, 4, 5, 6])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "120", None, 0, -1])
def test_impulse_rejects_non_prices_at_every_known_leg(length, bad):
    for index in range(length):
        values = IMPULSE[:length]
        values[index] = bad
        assert strict_impulse(values, True, guidelines=False) is False


@pytest.mark.parametrize("values", [[], [100], [100, 120], IMPULSE + [150], IMPULSE + [150, 160]])
def test_impulse_accepts_only_supported_partial_or_complete_length(values):
    assert strict_impulse(values, True, guidelines=False) is False


@pytest.mark.parametrize("direction", [None, 0, 1, "bullish", "bearish"])
def test_impulse_requires_unambiguous_boolean_direction(direction):
    assert strict_impulse(IMPULSE, direction, guidelines=False) is False


@pytest.mark.parametrize("scale", [1e-300, 1e-20, 1.0, 1e200, 1e306])
@pytest.mark.parametrize("length", [3, 4, 5, 6])
@pytest.mark.parametrize("bullish", [True, False])
def test_valid_partial_and_complete_impulses_survive_price_unit_changes(scale, length, bullish):
    values = [100, 120, 110, 142, 130, 150]
    if not bullish:
        values = [250 - value for value in values]
    assert strict_impulse([value * scale for value in values[:length]], bullish, guidelines=False)


@pytest.mark.parametrize("bullish", [True, False])
def test_hard_rules_keep_equal_third_and_unusual_but_valid_retracements(bullish):
    def valid(values):
        prices = values if bullish else [300 - price for price in values]
        return strict_impulse(prices, bullish, guidelines=False)
    assert valid([100, 120, 110, 130, 125, 145])  # Third tied, not uniquely shortest.
    assert valid([100, 110, 100.5, 125, 112, 130])  # 95% second retracement.
    assert valid([100, 110, 109.5, 130, 129, 145])  # Shallow second/fourth.
    for prices in ([100, 120, 100], [100, 120, 120], [100, 120, 110, 120],
                   [100, 120, 110, 142, 120], [100, 120, 110, 142, 142],
                   [100, 120, 110, 142, 130, 142], [100, 120, 110, 125, 122, 150]):
        assert not valid(prices)


def wave_candles():
    result = [{"ts": 1700000000 + i * 60, "open": 110.0, "high": 110.1,
               "low": 109.9, "close": 110.0, "volume": 10.0} for i in range(51)]
    vertices = [(0, 110), (5, 100), (15, 120), (25, 110), (35, 142), (45, 130), (50, 135)]
    for (start, a), (end, b) in zip(vertices, vertices[1:], strict=False):
        for index in range(start, end + 1):
            price = a + (b - a) * (index - start) / (end - start)
            result[index].update(open=price, high=price + .1, low=price - .1, close=price)
    return result


def impulses(rows):
    return [event for event in forming_waves(rows, precision_mode=True, preserve_candidates=True)
            if event["pattern"] == "elliott_impulse_bull"]


def test_fourth_pivot_is_unavailable_until_four_right_hand_closes_and_no_future_fifth():
    rows = wave_candles()
    original = copy.deepcopy(rows)
    before = impulses(rows[:49])
    after = impulses(rows[:50])
    assert before and after
    assert all(len(event["points"]) < 5 for event in before)
    assert any([point["label"] for point in event["points"]] == list("01234") for event in after)
    for event in after:
        assert not event["confirmed"] and event["phase"] == "FORMING"
        assert all(point["time"] <= rows[45]["ts"] for point in event["points"])
        assert all(point["label"] != "5" for point in event["points"])
    assert rows == original


def test_missing_optional_volume_does_not_erase_price_only_wave_geometry():
    rows = wave_candles()
    expected = impulses(rows)
    assert expected
    without_volume = [{key: value for key, value in row.items() if key != "volume"} for row in rows]
    assert impulses(without_volume) == expected


@pytest.mark.parametrize("vertices", [
    [(0, 105), (5, 100), (15, 110), (25, 100.5), (35, 125), (45, 112), (50, 116)],
    [(0, 105), (5, 100), (15, 110), (25, 109.5), (35, 130), (45, 129), (50, 132)],
])
def test_precision_forming_path_keeps_legal_non_fibonacci_candidates(vertices):
    rows = wave_candles()
    for (start, a), (end, b) in zip(vertices, vertices[1:], strict=False):
        for index in range(start, end + 1):
            price = a + (b - a) * (index - start) / (end - start)
            rows[index].update(open=price, close=price, high=price + .01, low=price - .01)
    assert any(len(event["points"]) == 5 for event in impulses(rows))


@pytest.mark.parametrize("defect", ["out_of_order", "duplicate_time", "future_time", "unclosed", "nan",
                                   "infinity", "zero_price", "invalid_ohlc", "negative_volume", "missing_ohlc"])
def test_forming_does_not_silently_sort_drop_or_reinterpret_invalid_prefix(defect):
    rows = wave_candles()
    if defect == "out_of_order":
        rows[-1], rows[-2] = rows[-2], rows[-1]
    elif defect == "duplicate_time":
        rows[-1]["ts"] = rows[-2]["ts"]
    elif defect == "future_time":
        rows[20]["ts"] = rows[-1]["ts"] + 60
    elif defect == "unclosed":
        rows[-1]["is_final"] = False
    elif defect == "nan":
        rows[20]["open"] = math.nan
    elif defect == "infinity":
        rows[20]["high"] = math.inf
    elif defect == "zero_price":
        rows[20]["low"] = 0
    elif defect == "invalid_ohlc":
        rows[20]["close"] = rows[20]["high"] + 1
    elif defect == "negative_volume":
        rows[20]["volume"] = -1
    else:
        del rows[20]["open"]
    assert forming_waves(rows, precision_mode=True, preserve_candidates=True) == []


def test_same_candle_high_and_low_cannot_be_two_sequential_known_waves(monkeypatch):
    from pattern_engine import Pivot
    rows = wave_candles()
    # OHLC cannot reveal whether this outside bar's low occurred before its high.
    pivots = [Pivot(5, "low", 100), Pivot(15, "high", 120), Pivot(15, "low", 110)]
    monkeypatch.setattr("pattern_engine.alternating_pivots", lambda context: pivots)
    assert not impulses(rows)
