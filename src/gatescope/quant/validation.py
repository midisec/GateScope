"""Leakage-resistant validation primitives for strategy research artifacts.

These functions calculate diagnostics only. They neither certify a strategy nor
have access to an exchange order port.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Mapping, Sequence


@dataclass(frozen=True)
class EventInterval:
    event_id: str
    dependency_group: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if not self.event_id or len(self.event_id) > 128:
            raise ValueError("event_id must contain 1-128 characters")
        if not self.dependency_group or len(self.dependency_group) > 128:
            raise ValueError("dependency_group must contain 1-128 characters")
        _require_aware(self.starts_at, "event starts_at")
        _require_aware(self.ends_at, "event ends_at")
        if _utc(self.ends_at) <= _utc(self.starts_at):
            raise ValueError("event interval must have positive duration")


@dataclass(frozen=True)
class EventUniqueness:
    weights: tuple[tuple[str, float], ...]
    unique_count: float


def event_uniqueness(intervals: Sequence[EventInterval]) -> EventUniqueness:
    """Integrate inverse concurrency over half-open event intervals.

    Only events in the same pre-registered dependency group contribute to each
    other's concurrency. The group must already include correlated assets and
    duplicate-timeframe representations chosen by the research policy.
    """

    ordered = tuple(sorted(intervals, key=lambda item: (_utc(item.starts_at), item.event_id)))
    if len({item.event_id for item in ordered}) != len(ordered):
        raise ValueError("event intervals must have unique event_id values")
    weights: list[tuple[str, float]] = []
    for current in ordered:
        start = _utc(current.starts_at)
        end = _utc(current.ends_at)
        peers = tuple(item for item in ordered if item.dependency_group == current.dependency_group)
        boundaries = sorted({
            boundary
            for peer in peers
            for boundary in (_utc(peer.starts_at), _utc(peer.ends_at))
            if start <= boundary <= end
        } | {start, end})
        inverse_concurrency_seconds = 0.0
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            if right <= left:
                continue
            concurrency = sum(
                _utc(peer.starts_at) < right and _utc(peer.ends_at) > left for peer in peers
            )
            if concurrency < 1:
                raise ValueError("event concurrency partition is invalid")
            inverse_concurrency_seconds += (right - left).total_seconds() / concurrency
        duration_seconds = (end - start).total_seconds()
        weights.append((current.event_id, inverse_concurrency_seconds / duration_seconds))
    return EventUniqueness(tuple(weights), sum(weight for _, weight in weights))


@dataclass(frozen=True)
class EffectiveSampleResult:
    unique_count: float
    design_effect: float
    independent_blocks: int
    autocorrelations: tuple[float, ...]
    estimator_version: str = "global-mean-biased-acf-v1"


def effective_independent_blocks(
    uniqueness_weights: Sequence[float],
    uniqueness_weighted_scores: Sequence[float],
    *,
    maximum_lag: int,
) -> EffectiveSampleResult:
    """Apply the registered residual design-effect estimator.

    ``uniqueness_weighted_scores`` must be time ordered and already transformed
    exactly as declared by the block-scheme version. Kish effective sample size
    is deliberately not used.
    """

    if len(uniqueness_weights) != len(uniqueness_weighted_scores):
        raise ValueError("weights and scores must have equal length")
    count = len(uniqueness_weighted_scores)
    if count < 3 or not 1 <= maximum_lag < count:
        raise ValueError("maximum_lag requires at least three ordered scores")
    if any(not math.isfinite(value) or not 0 < value <= 1 for value in uniqueness_weights):
        raise ValueError("uniqueness weights must be finite in (0,1]")
    if any(not math.isfinite(value) for value in uniqueness_weighted_scores):
        raise ValueError("weighted scores must be finite")
    mean = sum(uniqueness_weighted_scores) / count
    centered = tuple(value - mean for value in uniqueness_weighted_scores)
    denominator = sum(value * value for value in centered)
    if denominator <= 0:
        raise ValueError("residual design effect is not estimable for a constant score sequence")
    correlations = tuple(
        sum(centered[index] * centered[index - lag] for index in range(lag, count))
        / denominator
        for lag in range(1, maximum_lag + 1)
    )
    raw_effect = 1.0 + 2.0 * sum(
        (1.0 - lag / count) * correlation
        for lag, correlation in enumerate(correlations, start=1)
    )
    design_effect = max(1.0, raw_effect)
    unique_count = sum(uniqueness_weights)
    return EffectiveSampleResult(
        unique_count=unique_count,
        design_effect=design_effect,
        independent_blocks=math.floor(unique_count / design_effect),
        autocorrelations=correlations,
    )


@dataclass(frozen=True)
class CircularBlockBootstrapPolicy:
    method_version: str
    block_length: int
    resamples: int = 10_000
    alpha: float = 0.05
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.method_version or len(self.method_version) > 80:
            raise ValueError("bootstrap method_version must contain 1-80 characters")
        if self.block_length < 1:
            raise ValueError("bootstrap block_length must be positive")
        if not 1_000 <= self.resamples <= 1_000_000:
            raise ValueError("bootstrap resamples must be between 1,000 and 1,000,000")
        if not 0 < self.alpha <= 0.10:
            raise ValueError("bootstrap alpha must be in (0,0.10]")
        if not -(2**63) <= self.seed < 2**63:
            raise ValueError("bootstrap seed must be a signed 64-bit integer")


@dataclass(frozen=True)
class BlockBootstrapResult:
    observed_mean: float
    mean_lcb: float
    one_sided_p_value: float
    block_length: int
    resamples: int
    alpha: float
    method_version: str
    artifact_digest: str


def circular_block_bootstrap_mean(
    block_values: Sequence[float],
    policy: CircularBlockBootstrapPolicy,
) -> BlockBootstrapResult:
    """Return a deterministic percentile LCB and centered one-sided p-value."""

    values = tuple(float(value) for value in block_values)
    count = len(values)
    if count < 2 or policy.block_length > count:
        raise ValueError("bootstrap needs at least two values and block_length <= sample count")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("bootstrap values must be finite")
    observed = sum(values) / count
    centered = tuple(value - observed for value in values)
    generator = random.Random(policy.seed)
    sampled_means: list[float] = []
    centered_means: list[float] = []
    blocks_per_sample = math.ceil(count / policy.block_length)
    for _ in range(policy.resamples):
        indices: list[int] = []
        for _block in range(blocks_per_sample):
            start = generator.randrange(count)
            indices.extend((start + offset) % count for offset in range(policy.block_length))
        indices = indices[:count]
        sampled_means.append(sum(values[index] for index in indices) / count)
        centered_means.append(sum(centered[index] for index in indices) / count)
    sampled_means.sort()
    quantile_index = math.floor(policy.alpha * (policy.resamples - 1))
    lower_bound = sampled_means[quantile_index]
    exceedances = sum(value >= observed for value in centered_means)
    p_value = (exceedances + 1) / (policy.resamples + 1)
    canonical = {
        "alpha": _stable(policy.alpha),
        "block_length": policy.block_length,
        "method_version": policy.method_version,
        "resamples": policy.resamples,
        "seed": policy.seed,
        "values": [_stable(value) for value in values],
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return BlockBootstrapResult(
        observed_mean=observed,
        mean_lcb=lower_bound,
        one_sided_p_value=p_value,
        block_length=policy.block_length,
        resamples=policy.resamples,
        alpha=policy.alpha,
        method_version=policy.method_version,
        artifact_digest=digest,
    )


@dataclass(frozen=True)
class AdjustedHypothesis:
    hypothesis_id: str
    raw_p_value: float
    holm_adjusted_p_value: float


def holm_adjusted_p_values(
    p_values: Mapping[str, float],
    *,
    registered_family_size: int,
) -> tuple[AdjustedHypothesis, ...]:
    """Apply Holm's step-down FWER correction to the complete trial family."""

    if registered_family_size != len(p_values) or registered_family_size < 1:
        raise ValueError("all registered hypotheses, including failed trials, are required")
    if any(not key or len(key) > 128 for key in p_values):
        raise ValueError("hypothesis IDs must contain 1-128 characters")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in p_values.values()):
        raise ValueError("p-values must be finite in [0,1]")
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    adjusted: dict[str, float] = {}
    running_maximum = 0.0
    for rank, (hypothesis_id, raw) in enumerate(ordered):
        candidate = min(1.0, (registered_family_size - rank) * raw)
        running_maximum = max(running_maximum, candidate)
        adjusted[hypothesis_id] = running_maximum
    return tuple(
        AdjustedHypothesis(identifier, p_values[identifier], adjusted[identifier])
        for identifier in sorted(p_values)
    )


def required_iid_blocks_for_ev(
    *,
    alpha_per_hypothesis: float,
    power: float,
    training_sigma: float,
    minimum_economic_effect: float,
) -> int:
    """One-sided normal approximation used only as a pre-registered power floor."""

    if not 0 < alpha_per_hypothesis <= 0.10:
        raise ValueError("alpha_per_hypothesis must be in (0,0.10]")
    if not 0.5 < power < 1:
        raise ValueError("power must be in (0.5,1)")
    if not math.isfinite(training_sigma) or training_sigma <= 0:
        raise ValueError("training_sigma must be finite and positive")
    if not math.isfinite(minimum_economic_effect) or minimum_economic_effect <= 0:
        raise ValueError("minimum_economic_effect must be finite and positive")
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha_per_hypothesis)
    z_power = normal.inv_cdf(power)
    required = ((z_alpha + z_power) * training_sigma / minimum_economic_effect) ** 2
    return math.ceil(required)


@dataclass(frozen=True)
class TemporalSample:
    sample_id: str
    available_at: datetime
    label_starts_at: datetime
    label_ends_at: datetime

    def __post_init__(self) -> None:
        if not self.sample_id or len(self.sample_id) > 128:
            raise ValueError("sample_id must contain 1-128 characters")
        for value, label in (
            (self.available_at, "available_at"),
            (self.label_starts_at, "label_starts_at"),
            (self.label_ends_at, "label_ends_at"),
        ):
            _require_aware(value, label)
        if _utc(self.available_at) > _utc(self.label_starts_at):
            raise ValueError("sample is unavailable at its label start")
        if _utc(self.label_ends_at) <= _utc(self.label_starts_at):
            raise ValueError("sample label interval must have positive duration")


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: str
    train_start: datetime
    train_end: datetime
    calibration_start: datetime
    calibration_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        if not self.fold_id or len(self.fold_id) > 80:
            raise ValueError("fold_id must contain 1-80 characters")
        values = (
            self.train_start,
            self.train_end,
            self.calibration_start,
            self.calibration_end,
            self.test_start,
            self.test_end,
        )
        for value in values:
            _require_aware(value, "walk-forward boundary")
        normalized = tuple(_utc(value) for value in values)
        if any(left >= right for left, right in zip(normalized, normalized[1:], strict=False)):
            raise ValueError("walk-forward boundaries must be strictly increasing")


@dataclass(frozen=True)
class PurgedFold:
    fold_id: str
    train_ids: tuple[str, ...]
    calibration_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    purged_ids: tuple[str, ...]


def purged_embargo_split(
    samples: Sequence[TemporalSample],
    fold: WalkForwardFold,
    *,
    maximum_holding_period: timedelta,
) -> PurgedFold:
    """Partition one explicit fold and remove boundary-crossing labels.

    The gaps between train/calibration and calibration/test are treated as a
    conservative embargo and must each be at least the maximum holding period.
    """

    if maximum_holding_period <= timedelta(0):
        raise ValueError("maximum_holding_period must be positive")
    if _utc(fold.calibration_start) - _utc(fold.train_end) < maximum_holding_period:
        raise ValueError("train/calibration embargo is shorter than maximum holding period")
    if _utc(fold.test_start) - _utc(fold.calibration_end) < maximum_holding_period:
        raise ValueError("calibration/test embargo is shorter than maximum holding period")
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("temporal samples must have unique sample_id values")
    buckets: dict[str, list[str]] = {"train": [], "calibration": [], "test": [], "purged": []}
    segments = (
        ("train", _utc(fold.train_start), _utc(fold.train_end)),
        ("calibration", _utc(fold.calibration_start), _utc(fold.calibration_end)),
        ("test", _utc(fold.test_start), _utc(fold.test_end)),
    )
    for sample in sorted(samples, key=lambda item: (_utc(item.label_starts_at), item.sample_id)):
        label_start = _utc(sample.label_starts_at)
        label_end = _utc(sample.label_ends_at)
        assigned = False
        overlaps_research_window = False
        for name, segment_start, segment_end in segments:
            if label_start < segment_end and label_end > segment_start:
                overlaps_research_window = True
            if segment_start <= label_start and label_end <= segment_end:
                buckets[name].append(sample.sample_id)
                assigned = True
                break
        if not assigned and overlaps_research_window:
            buckets["purged"].append(sample.sample_id)
    return PurgedFold(
        fold_id=fold.fold_id,
        train_ids=tuple(buckets["train"]),
        calibration_ids=tuple(buckets["calibration"]),
        test_ids=tuple(buckets["test"]),
        purged_ids=tuple(buckets["purged"]),
    )


def validate_outer_walk_forward(
    folds: Sequence[WalkForwardFold],
    *,
    sealed_holdout_start: datetime,
    maximum_holding_period: timedelta,
) -> None:
    """Validate the minimum outer-fold count and untouched final holdout gap."""

    _require_aware(sealed_holdout_start, "sealed_holdout_start")
    if len(folds) < 5:
        raise ValueError("nested walk-forward requires at least five outer folds")
    if maximum_holding_period <= timedelta(0):
        raise ValueError("maximum_holding_period must be positive")
    ordered = sorted(folds, key=lambda item: _utc(item.test_start))
    if list(folds) != ordered:
        raise ValueError("outer folds must be ordered by test_start")
    if len({fold.fold_id for fold in folds}) != len(folds):
        raise ValueError("outer fold IDs must be unique")
    for previous, current in zip(folds, folds[1:], strict=False):
        if _utc(current.test_start) < _utc(previous.test_end):
            raise ValueError("outer test windows must not overlap")
    if _utc(sealed_holdout_start) - _utc(folds[-1].test_end) < maximum_holding_period:
        raise ValueError("sealed holdout embargo is shorter than maximum holding period")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _stable(value: float) -> str:
    return format(value, ".17g")
