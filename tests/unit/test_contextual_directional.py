import pytest
from gatescope.quant.contextual_directional import CONTEXTS, filtered, rules, signal_context
from tests.unit.test_research_backtest import make_bars


def test_fixed_transfer_grid_and_prefix_causality():
    assert len(rules()) == 288
    bars = make_bars(1000)
    points, book = signal_context(bars)
    past, past_book = signal_context(bars[:850])
    assert points[:850] == past
    for name in CONTEXTS:
        assert filtered(points, book["macd_cross"], name)[:850] == filtered(
            past, past_book["macd_cross"], name
        )


def test_range_filter_cannot_convert_direction_or_manufacture_signal():
    points = [dict(values={"adx.adx": a}) for a in (19, 20, 30)]
    e = {"side": -1, "evidence_id": "original"}
    assert filtered(points, [e, e, None], "RANGE") == [e, None, None]
    with pytest.raises(ValueError):
        filtered(points, [e, e, e], "bogus")
