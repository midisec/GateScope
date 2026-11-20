from __future__ import annotations

import math

import pytest

from gatescope.quant import (
    CscvPolicy,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)


def test_dsr_reproduces_the_papers_numerical_example() -> None:
    # Paper inputs: annualized SR=2.5, cross-trial variance=0.5,
    # 250 observations/year, T=1250, skew=-3, Pearson kurtosis=10.
    result = deflated_sharpe_ratio(
        observed_sharpe=2.5 / math.sqrt(250),
        sample_count=1_250,
        return_skewness=-3,
        return_kurtosis=10,
        trial_sharpe_variance=0.5 / 250,
        independent_trials=88,
        method_version="bailey-lopez-de-prado-2014-v1",
    )
    fewer_trials = deflated_sharpe_ratio(
        observed_sharpe=2.5 / math.sqrt(250),
        sample_count=1_250,
        return_skewness=-3,
        return_kurtosis=10,
        trial_sharpe_variance=0.5 / 250,
        independent_trials=46,
        method_version="bailey-lopez-de-prado-2014-v1",
    )

    assert result.probability == pytest.approx(0.9101530147)
    assert fewer_trials.probability == pytest.approx(0.9505017069)
    assert result.expected_maximum_sharpe > fewer_trials.expected_maximum_sharpe


def test_dsr_rejects_incomplete_trial_family_and_invalid_variance_term() -> None:
    with pytest.raises(ValueError, match="at least two independent trials"):
        deflated_sharpe_ratio(
            observed_sharpe=1.0,
            sample_count=100,
            return_skewness=0,
            return_kurtosis=3,
            trial_sharpe_variance=0.1,
            independent_trials=1,
            method_version="dsr-v1",
        )
    with pytest.raises(ValueError, match="not positive"):
        deflated_sharpe_ratio(
            observed_sharpe=1.0,
            sample_count=100,
            return_skewness=10,
            return_kurtosis=1,
            trial_sharpe_variance=0.1,
            independent_trials=10,
            method_version="dsr-v1",
        )


def test_cscv_stable_leader_has_zero_pbo() -> None:
    matrix = [
        [0.03, 0.01, -0.01],
        [0.02, 0.00, -0.02],
    ] * 4
    result = cscv_probability_of_backtest_overfitting(
        matrix,
        ["stable", "flat", "loser"],
        CscvPolicy("cscv-v1", slices=4, minimum_slice_rows=2, performance_measure="MEAN"),
        registered_trial_count=3,
    )

    assert result.combinations == 6
    assert result.probability_of_backtest_overfitting == 0
    assert set(result.selected_trial_ids) == {"stable"}
    assert all(value > 0 for value in result.logits)


def test_cscv_detects_slice_specific_winners_as_fully_overfit() -> None:
    matrix = [
        [4.0, 0.0, 0.0, 0.0],
        [0.0, 3.0, 0.0, 0.0],
        [0.0, 0.0, 2.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    result = cscv_probability_of_backtest_overfitting(
        matrix,
        ["slice-a", "slice-b", "slice-c", "slice-d"],
        CscvPolicy("cscv-v1", slices=4, performance_measure="MEAN"),
        registered_trial_count=4,
    )

    assert result.probability_of_backtest_overfitting == 1
    assert all(value < 0 for value in result.logits)


def test_cscv_rejects_hidden_trials_missing_rows_and_ambiguous_is_winners() -> None:
    policy = CscvPolicy("cscv-v1", slices=4, performance_measure="MEAN")
    with pytest.raises(ValueError, match="complete registered trial family"):
        cscv_probability_of_backtest_overfitting(
            [[1.0, 0.0]] * 4, ["a", "b"], policy, registered_trial_count=3,
        )
    with pytest.raises(ValueError, match="aligned T x N"):
        cscv_probability_of_backtest_overfitting(
            [[1.0, 0.0], [1.0]] * 2, ["a", "b"], policy, registered_trial_count=2,
        )
    with pytest.raises(ValueError, match="optimum is tied"):
        cscv_probability_of_backtest_overfitting(
            [[1.0, 1.0]] * 4, ["a", "b"], policy, registered_trial_count=2,
        )
