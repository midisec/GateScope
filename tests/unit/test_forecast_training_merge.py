"""Late replay outcomes improve NEW forecasts without changing old forecasts."""

from copy import deepcopy

from gatescope.adapters.postgres_pattern_studies import merge_forecast_training
from gatescope.quant.structure_forecast import forecast
from tests.unit.test_structure_forecast import wave


CELL = "elliott_impulse_bull:FORMING:3"


def test_late_training_merges_even_after_history_seeded_and_preserves_originals():
    state = {
        "history_seeded": True,
        "forecast_training": {},
        "records": {"old": {"first": {"forecast": {"price": 123}}}},
    }
    original = deepcopy(state["records"])
    merge_forecast_training(state, None, 1000)
    replay = {CELL: [[900, 2.0, str(i)] for i in range(25)]}
    frozen = deepcopy(replay)
    merge_forecast_training(state, replay, 1000)
    result = forecast(wave([100, 110, 105]), state, 1000, 2000, 110)
    assert result["history_count"] == 25 and result["source"] == "PRIOR_COMPLETED_LEGS_SHRUNK"
    assert state["records"] == original and replay == frozen
    merge_forecast_training(state, replay, 1000)
    assert len(state["forecast_training"][CELL]) == 25


def test_same_bar_and_future_outcomes_cannot_train_current_forecast():
    state = {}
    replay = {
        CELL: [[900, 2.0, str(i)] for i in range(25)] + [[1000, 2.2, "same-close"], [1001, 2.3, "future"]]
    }
    merge_forecast_training(state, replay, 1000)
    assert len(state["forecast_training"][CELL]) == 26
    assert forecast(wave([100, 110, 105]), state, 1000, 2000, 110)["history_count"] == 25
    merge_forecast_training(state, replay, 1001)
    assert forecast(wave([100, 110, 105]), state, 1001, 2000, 110)["history_count"] == 26


def test_recorded_sample_wins_collision_and_retains_other_live_cells():
    state = {"forecast_training": {CELL: [[100, 1.9, "recorded"]], "other": [[100, 2.0, "live-only"]]}}
    replay = {CELL: [[100, 2.5, "recorded"], [101, 2.0, "recorded"], [102, 2.2, "replay-only"]]}
    merge_forecast_training(state, replay, 200)
    assert state["forecast_training"][CELL] == [
        [100, 1.9, "recorded"],
        [101, 2.0, "recorded"],
        [102, 2.2, "replay-only"],
    ]
    assert state["forecast_training"]["other"] == [[100, 2.0, "live-only"]]


def test_completed_leg_cache_remains_bounded_and_drops_invalid_replay_rows():
    state = {"forecast_training": {CELL: [[1, 1.2, "old"]]}}
    replay = {CELL: [[i, 2.0, str(i)] for i in range(2, 302)]}
    merge_forecast_training(state, replay, 1000)
    assert len(state["forecast_training"][CELL]) == 256
    assert state["forecast_training"][CELL][0][0] == 46
    before = deepcopy(state)
    merge_forecast_training(
        state,
        {
            CELL: [
                None,
                [],
                ["yesterday", 2.0, "a"],
                [999, float("nan"), "a"],
                [999, True, "a"],
                [True, 2.0, "a"],
            ]
        },
        1000,
    )
    assert state == before
