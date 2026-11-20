from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gatescope.quant import GodEvidenceEvaluator, OutcomeBlock, ProbabilityPolicy

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def policy(**overrides: object) -> ProbabilityPolicy:
    values = {
        "policy_version": "breakout-v1",
        "return_floor": -0.10,
        "return_ceiling": 0.10,
        "break_even_probability_upper": 0.50,
        "minimum_blocks": 100,
        "family_alpha": 0.05,
        "tested_hypotheses": 1,
        "minimum_expected_return_lcb": 0.0,
    }
    values.update(overrides)
    return ProbabilityPolicy(**values)  # type: ignore[arg-type]


def blocks(count: int, value: float = 0.04) -> list[OutcomeBlock]:
    return [
        OutcomeBlock(f"block-{index:04d}", NOW - timedelta(days=count - index), value)
        for index in range(count)
    ]


def evaluate(samples: list[OutcomeBlock], configured: ProbabilityPolicy | None = None):
    return GodEvidenceEvaluator().evaluate(
        samples,
        configured or policy(),
        decision_at=NOW,
        net_of_costs=True,
        sealed_holdout=True,
        source_fresh=True,
        in_distribution=True,
        external_features_licensed=True,
    )


def test_strong_bounded_holdout_can_only_reach_paper_tier() -> None:
    decision = evaluate(blocks(200))
    assert decision.decision == "PAPER_ELIGIBLE"
    assert decision.evidence_tier == "PAPER"
    assert decision.win_probability_lcb > 0.5
    assert decision.expected_net_return_lcb > 0
    assert decision.reason_codes == ()
    assert len(decision.evidence_digest) == 64
    assert any("independent" in item for item in decision.assumptions)


def test_insufficient_or_gross_only_evidence_abstains() -> None:
    decision = GodEvidenceEvaluator().evaluate(
        blocks(40),
        policy(),
        decision_at=NOW,
        net_of_costs=False,
        sealed_holdout=True,
        source_fresh=True,
        in_distribution=True,
        external_features_licensed=True,
    )
    assert decision.decision == "ABSTAIN"
    assert decision.evidence_tier == "RESEARCH"
    assert "INSUFFICIENT_INDEPENDENT_BLOCKS" in decision.reason_codes
    assert "RETURNS_NOT_NET_OF_COSTS" in decision.reason_codes


def test_future_or_out_of_support_observations_fail_closed() -> None:
    future = OutcomeBlock("future", NOW + timedelta(seconds=1), 0.01)
    with pytest.raises(ValueError, match="future"):
        evaluate([future])
    outside = OutcomeBlock("outside", NOW - timedelta(seconds=1), 0.11)
    with pytest.raises(ValueError, match="outside"):
        evaluate([outside])


def test_multiple_testing_allocation_is_bonferroni_and_more_conservative() -> None:
    sample = blocks(200)
    one = evaluate(sample, policy(tested_hypotheses=1))
    many = evaluate(sample, policy(tested_hypotheses=100))
    assert many.alpha_per_hypothesis == pytest.approx(0.0005)
    assert many.win_probability_lcb < one.win_probability_lcb
    assert many.expected_net_return_lcb < one.expected_net_return_lcb


def test_evidence_digest_is_order_invariant_but_policy_bound() -> None:
    sample = blocks(120)
    forward = evaluate(sample)
    reverse = evaluate(list(reversed(sample)))
    changed_policy = evaluate(sample, policy(policy_version="breakout-v2"))
    assert forward.evidence_digest == reverse.evidence_digest
    assert forward.evidence_digest != changed_policy.evidence_digest


def test_stale_ood_or_unlicensed_features_force_abstention() -> None:
    decision = GodEvidenceEvaluator().evaluate(
        blocks(200),
        policy(),
        decision_at=NOW,
        net_of_costs=True,
        sealed_holdout=True,
        source_fresh=False,
        in_distribution=False,
        external_features_licensed=False,
    )
    assert set(decision.reason_codes) >= {
        "SOURCE_STALE",
        "OUT_OF_DISTRIBUTION",
        "EXTERNAL_FEATURE_LICENSE_BLOCKED",
    }
