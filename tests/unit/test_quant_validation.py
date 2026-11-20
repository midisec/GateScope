from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gatescope.quant import (
    CircularBlockBootstrapPolicy,
    EventInterval,
    TemporalSample,
    WalkForwardFold,
    circular_block_bootstrap_mean,
    effective_independent_blocks,
    event_uniqueness,
    holm_adjusted_p_values,
    purged_embargo_split,
    required_iid_blocks_for_ev,
    validate_outer_walk_forward,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_uniqueness_is_one_for_fully_overlapping_events_and_n_for_disjoint_events() -> None:
    overlapping = [
        EventInterval(f"same-{index}", "correlated-cluster", BASE, BASE + timedelta(hours=1))
        for index in range(100)
    ]
    disjoint = [
        EventInterval(
            f"apart-{index}",
            "correlated-cluster",
            BASE + timedelta(hours=index),
            BASE + timedelta(hours=index + 1),
        )
        for index in range(100)
    ]

    same_result = event_uniqueness(overlapping)
    apart_result = event_uniqueness(disjoint)

    assert same_result.unique_count == pytest.approx(1.0)
    assert all(weight == pytest.approx(0.01) for _, weight in same_result.weights)
    assert apart_result.unique_count == pytest.approx(100.0)


def test_dependency_groups_do_not_create_false_cross_group_concurrency() -> None:
    result = event_uniqueness([
        EventInterval("btc", "cluster-a", BASE, BASE + timedelta(hours=1)),
        EventInterval("unrelated", "cluster-b", BASE, BASE + timedelta(hours=1)),
    ])
    assert result.unique_count == pytest.approx(2.0)


def test_independent_block_count_uses_design_effect_once_and_rejects_unstable_acf() -> None:
    result = effective_independent_blocks(
        [1.0] * 8,
        [1.0, 0.8, 0.6, 0.4, -0.4, -0.6, -0.8, -1.0],
        maximum_lag=1,
    )
    assert result.design_effect > 1
    assert result.independent_blocks == int(8 / result.design_effect)
    with pytest.raises(ValueError, match="not estimable"):
        effective_independent_blocks([1.0] * 8, [0.5] * 8, maximum_lag=2)


def test_circular_block_bootstrap_is_deterministic_and_method_bound() -> None:
    values = [0.04, 0.01, 0.03, -0.01, 0.02, 0.01, 0.05, -0.005]
    policy = CircularBlockBootstrapPolicy("cbb-v1", block_length=3, resamples=2_000, seed=7)
    first = circular_block_bootstrap_mean(values, policy)
    repeated = circular_block_bootstrap_mean(values, policy)
    changed = circular_block_bootstrap_mean(
        values,
        CircularBlockBootstrapPolicy("cbb-v2", block_length=3, resamples=2_000, seed=7),
    )

    assert first.mean_lcb > 0
    assert 0 < first.one_sided_p_value <= 1
    assert first == repeated
    assert first.artifact_digest != changed.artifact_digest


def test_holm_is_monotone_and_requires_the_complete_registered_family() -> None:
    adjusted = holm_adjusted_p_values(
        {"trial-c": 0.04, "trial-a": 0.01, "trial-b": 0.03},
        registered_family_size=3,
    )
    by_id = {item.hypothesis_id: item.holm_adjusted_p_value for item in adjusted}
    assert by_id == pytest.approx({"trial-a": 0.03, "trial-b": 0.06, "trial-c": 0.06})
    with pytest.raises(ValueError, match="all registered hypotheses"):
        holm_adjusted_p_values({"trial-a": 0.01}, registered_family_size=2)


def test_ev_power_floor_matches_the_registered_one_sided_formula() -> None:
    required = required_iid_blocks_for_ev(
        alpha_per_hypothesis=0.05,
        power=0.80,
        training_sigma=0.02,
        minimum_economic_effect=0.005,
    )
    assert required == 99


def _fold(index: int) -> WalkForwardFold:
    offset = timedelta(days=100 * index)
    return WalkForwardFold(
        f"fold-{index}",
        BASE + offset,
        BASE + offset + timedelta(days=40),
        BASE + offset + timedelta(days=45),
        BASE + offset + timedelta(days=60),
        BASE + offset + timedelta(days=65),
        BASE + offset + timedelta(days=90),
    )


def test_purged_split_removes_cross_boundary_labels_and_keeps_segments_disjoint() -> None:
    fold = _fold(0)
    samples = [
        TemporalSample("train", BASE, BASE + timedelta(days=1), BASE + timedelta(days=2)),
        TemporalSample(
            "cross-train", BASE + timedelta(days=38), BASE + timedelta(days=39),
            BASE + timedelta(days=41),
        ),
        TemporalSample(
            "calibration", BASE + timedelta(days=45), BASE + timedelta(days=46),
            BASE + timedelta(days=47),
        ),
        TemporalSample(
            "cross-test", BASE + timedelta(days=59), BASE + timedelta(days=59),
            BASE + timedelta(days=66),
        ),
        TemporalSample(
            "test", BASE + timedelta(days=65), BASE + timedelta(days=66),
            BASE + timedelta(days=67),
        ),
    ]

    result = purged_embargo_split(samples, fold, maximum_holding_period=timedelta(days=5))

    assert result.train_ids == ("train",)
    assert result.calibration_ids == ("calibration",)
    assert result.test_ids == ("test",)
    assert result.purged_ids == ("cross-train", "cross-test")


def test_outer_plan_requires_five_ordered_folds_and_holdout_embargo() -> None:
    folds = [_fold(index) for index in range(5)]
    validate_outer_walk_forward(
        folds,
        sealed_holdout_start=folds[-1].test_end + timedelta(days=5),
        maximum_holding_period=timedelta(days=5),
    )
    with pytest.raises(ValueError, match="at least five"):
        validate_outer_walk_forward(
            folds[:4],
            sealed_holdout_start=folds[-1].test_end + timedelta(days=5),
            maximum_holding_period=timedelta(days=5),
        )
    with pytest.raises(ValueError, match="holdout embargo"):
        validate_outer_walk_forward(
            folds,
            sealed_holdout_start=folds[-1].test_end + timedelta(days=4),
            maximum_holding_period=timedelta(days=5),
        )
