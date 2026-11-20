import pytest

from gatescope.quant.structure_screening import evaluate, historical_stats, sample_capacity


@pytest.mark.parametrize(
    "step,maximum,reachable", [(14400, 109, True), (28800, 54, False), (86400, 18, False)]
)
def test_yearly_nonoverlap_capacity_is_explicit(step, maximum, reachable):
    capacity = sample_capacity(step)
    assert capacity["maximum_independent_samples"] == maximum
    assert capacity["sample_requirement_reachable"] is reachable


def test_cutoff_reduces_capacity_without_lowering_confidence_requirement():
    assert sample_capacity(14400, 365 * 86400 - 60 * 14400)["maximum_independent_samples"] == 106
    assert sample_capacity(86400, 0)["maximum_independent_samples"] == 0
    stats = historical_stats(18, 18, 0.01, 2, timeframe_seconds=86400)
    result = evaluate(
        {"phase": "FORMED", "latest": {"confidence": 0.95, "signal_direction": "bullish"}}, stats
    )
    assert not result["qualified"]
    assert "HISTORY_CAPACITY" in result["reasons"]
    assert "SAMPLES" in result["reasons"]
    assert stats["sample_count"] == 18
    assert stats["interval"] is None


def test_three_year_daily_history_still_does_not_invent_a_hundred_samples():
    assert sample_capacity(86400, 3 * 365 * 86400)["maximum_independent_samples"] == 54
    assert not sample_capacity(86400, 3 * 365 * 86400)["sample_requirement_reachable"]


@pytest.mark.parametrize("step,window", [(0, 86400), (60, -1)])
def test_invalid_window_is_rejected(step, window):
    with pytest.raises(ValueError):
        sample_capacity(step, window)
