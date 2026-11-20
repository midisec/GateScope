"""Paper-faithful diagnostics for selection bias and backtest overfitting."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal, Sequence


EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class DeflatedSharpeResult:
    probability: float
    expected_maximum_sharpe: float
    test_statistic: float
    observed_sharpe: float
    sample_count: int
    independent_trials: int
    method_version: str
    artifact_digest: str


def deflated_sharpe_ratio(
    *,
    observed_sharpe: float,
    sample_count: int,
    return_skewness: float,
    return_kurtosis: float,
    trial_sharpe_variance: float,
    independent_trials: int,
    method_version: str,
) -> DeflatedSharpeResult:
    """Compute Bailey and López de Prado's DSR on per-observation Sharpe units.

    The observed Sharpe and cross-trial variance must use the same, non-
    annualized frequency. For the production policy that means net UTC daily
    NAV returns, including no-trade days as zero observations.
    """

    numeric = (
        observed_sharpe,
        return_skewness,
        return_kurtosis,
        trial_sharpe_variance,
    )
    if any(not math.isfinite(value) for value in numeric):
        raise ValueError("DSR inputs must be finite")
    if sample_count < 3:
        raise ValueError("DSR requires at least three return observations")
    if independent_trials < 2:
        raise ValueError("DSR extreme-value threshold requires at least two independent trials")
    if trial_sharpe_variance < 0:
        raise ValueError("trial Sharpe variance cannot be negative")
    if not method_version or len(method_version) > 80:
        raise ValueError("DSR method_version must contain 1-80 characters")

    normal = NormalDist()
    first_quantile = normal.inv_cdf(1.0 - 1.0 / independent_trials)
    second_quantile = normal.inv_cdf(1.0 - 1.0 / (independent_trials * math.e))
    expected_maximum = math.sqrt(trial_sharpe_variance) * (
        (1.0 - EULER_MASCHERONI) * first_quantile
        + EULER_MASCHERONI * second_quantile
    )
    variance_term = (
        1.0
        - return_skewness * observed_sharpe
        + ((return_kurtosis - 1.0) / 4.0) * observed_sharpe**2
    )
    if not math.isfinite(variance_term) or variance_term <= 0:
        raise ValueError("DSR non-normal variance correction is not positive")
    statistic = (
        (observed_sharpe - expected_maximum)
        * math.sqrt(sample_count - 1)
        / math.sqrt(variance_term)
    )
    probability = normal.cdf(statistic)
    canonical = {
        "independent_trials": independent_trials,
        "method_version": method_version,
        "observed_sharpe": _stable(observed_sharpe),
        "return_kurtosis": _stable(return_kurtosis),
        "return_skewness": _stable(return_skewness),
        "sample_count": sample_count,
        "trial_sharpe_variance": _stable(trial_sharpe_variance),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return DeflatedSharpeResult(
        probability=probability,
        expected_maximum_sharpe=expected_maximum,
        test_statistic=statistic,
        observed_sharpe=observed_sharpe,
        sample_count=sample_count,
        independent_trials=independent_trials,
        method_version=method_version,
        artifact_digest=digest,
    )


@dataclass(frozen=True)
class CscvPolicy:
    method_version: str
    slices: int = 16
    minimum_slice_rows: int = 1
    performance_measure: Literal["SHARPE", "MEAN"] = "SHARPE"
    maximum_combinations: int = 100_000

    def __post_init__(self) -> None:
        if not self.method_version or len(self.method_version) > 80:
            raise ValueError("CSCV method_version must contain 1-80 characters")
        if not 4 <= self.slices <= 30 or self.slices % 2:
            raise ValueError("CSCV slices must be an even integer between 4 and 30")
        if self.minimum_slice_rows < 1:
            raise ValueError("CSCV minimum_slice_rows must be positive")
        if not 1 <= self.maximum_combinations <= 1_000_000:
            raise ValueError("CSCV maximum_combinations is invalid")


@dataclass(frozen=True)
class CscvPboResult:
    probability_of_backtest_overfitting: float
    probability_of_oos_loss: float
    logits: tuple[float, ...]
    selected_trial_ids: tuple[str, ...]
    in_sample_performance: tuple[float, ...]
    out_of_sample_performance: tuple[float, ...]
    combinations: int
    slices: int
    trial_count: int
    method_version: str
    artifact_digest: str


def cscv_probability_of_backtest_overfitting(
    aligned_returns: Sequence[Sequence[float]],
    trial_ids: Sequence[str],
    policy: CscvPolicy,
    *,
    registered_trial_count: int,
) -> CscvPboResult:
    """Run Algorithm 2.3 CSCV over a complete synchronous return matrix."""

    matrix = tuple(tuple(float(value) for value in row) for row in aligned_returns)
    identifiers = tuple(trial_ids)
    if registered_trial_count != len(identifiers) or registered_trial_count < 2:
        raise ValueError("the complete registered trial family is required for CSCV")
    if len(set(identifiers)) != len(identifiers) or any(
        not identifier or len(identifier) > 128 for identifier in identifiers
    ):
        raise ValueError("CSCV trial IDs must be unique and contain 1-128 characters")
    if not matrix or any(len(row) != len(identifiers) for row in matrix):
        raise ValueError("CSCV requires a non-empty, aligned T x N matrix")
    if any(not math.isfinite(value) for row in matrix for value in row):
        raise ValueError("CSCV matrix cannot contain missing or non-finite values")
    row_count = len(matrix)
    if row_count % policy.slices:
        raise ValueError("CSCV rows must divide exactly into equal contiguous slices")
    slice_width = row_count // policy.slices
    if slice_width < policy.minimum_slice_rows:
        raise ValueError("CSCV slice is shorter than the registered minimum width")
    combination_count = math.comb(policy.slices, policy.slices // 2)
    if combination_count > policy.maximum_combinations:
        raise ValueError("CSCV combination count exceeds the registered resource ceiling")

    slice_rows = tuple(
        tuple(range(index * slice_width, (index + 1) * slice_width))
        for index in range(policy.slices)
    )
    logits: list[float] = []
    selected_ids: list[str] = []
    in_sample_values: list[float] = []
    out_of_sample_values: list[float] = []
    all_slices = frozenset(range(policy.slices))
    for in_sample_slices in itertools.combinations(range(policy.slices), policy.slices // 2):
        in_sample_set = frozenset(in_sample_slices)
        out_sample_set = all_slices - in_sample_set
        in_rows = tuple(row for index in in_sample_slices for row in slice_rows[index])
        out_rows = tuple(row for index in sorted(out_sample_set) for row in slice_rows[index])
        in_performance = tuple(
            _performance(matrix, in_rows, trial, policy.performance_measure)
            for trial in range(len(identifiers))
        )
        maximum = max(in_performance)
        winners = tuple(index for index, value in enumerate(in_performance) if value == maximum)
        if len(winners) != 1:
            raise ValueError("CSCV in-sample optimum is tied and cannot be selected deterministically")
        selected = winners[0]
        out_performance = tuple(
            _performance(matrix, out_rows, trial, policy.performance_measure)
            for trial in range(len(identifiers))
        )
        selected_oos = out_performance[selected]
        lower = sum(value < selected_oos for value in out_performance)
        equal_other = sum(value == selected_oos for value in out_performance) - 1
        average_rank = 1.0 + lower + equal_other / 2.0
        relative_rank = average_rank / (len(identifiers) + 1.0)
        logit = math.log(relative_rank / (1.0 - relative_rank))
        logits.append(logit)
        selected_ids.append(identifiers[selected])
        in_sample_values.append(in_performance[selected])
        out_of_sample_values.append(selected_oos)

    pbo = sum(value < 0 for value in logits) / combination_count
    probability_loss = sum(value < 0 for value in out_of_sample_values) / combination_count
    canonical = {
        "matrix": [[_stable(value) for value in row] for row in matrix],
        "maximum_combinations": policy.maximum_combinations,
        "method_version": policy.method_version,
        "minimum_slice_rows": policy.minimum_slice_rows,
        "performance_measure": policy.performance_measure,
        "registered_trial_count": registered_trial_count,
        "slices": policy.slices,
        "trial_ids": identifiers,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return CscvPboResult(
        probability_of_backtest_overfitting=pbo,
        probability_of_oos_loss=probability_loss,
        logits=tuple(logits),
        selected_trial_ids=tuple(selected_ids),
        in_sample_performance=tuple(in_sample_values),
        out_of_sample_performance=tuple(out_of_sample_values),
        combinations=combination_count,
        slices=policy.slices,
        trial_count=len(identifiers),
        method_version=policy.method_version,
        artifact_digest=digest,
    )


def _performance(
    matrix: tuple[tuple[float, ...], ...],
    rows: tuple[int, ...],
    trial: int,
    measure: Literal["SHARPE", "MEAN"],
) -> float:
    values = tuple(matrix[row][trial] for row in rows)
    mean = sum(values) / len(values)
    if measure == "MEAN":
        return mean
    if len(values) < 2:
        raise ValueError("CSCV Sharpe requires at least two observations per half sample")
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance == 0:
        raise ValueError("CSCV Sharpe is undefined for a zero-variance half sample")
    return mean / math.sqrt(variance)


def _stable(value: float) -> str:
    return format(value, ".17g")
