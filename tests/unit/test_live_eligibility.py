from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gatescope.quant import (
    LiveEligibilityArtifact,
    LiveEligibilityGateEvaluator,
    LiveEligibilityPolicy,
)


NOW = datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc)


def _artifact(**changes: Any) -> LiveEligibilityArtifact:
    artifact = LiveEligibilityArtifact(
        strategy_version_ref="momentum-v7",
        source_artifact_digest="a" * 64,
        computed_at=NOW - timedelta(minutes=1),
        independent_blocks=500,
        required_power_blocks=420,
        holm_adjusted_p_value=0.02,
        policy_value_lcb=0.001,
        deflated_sharpe_probability=0.97,
        pbo_probability=0.08,
        base_cost_oos_ev=0.003,
        double_cost_oos_ev=0.001,
        event_ev_robust_lcb=0.0005,
        maximum_regime_profit_contribution=0.55,
        minimum_allowed_regime_blocks=130,
        leave_one_regime_out_ev=(0.001, 0.002, 0.0003),
        trial_registry_complete=True,
        nested_walk_forward_complete=True,
        sealed_holdout_consumed_once=True,
        calibration_passed=True,
        maximum_drawdown_passed=True,
        data_quality_passed=True,
        out_of_distribution_passed=True,
        cost_model_passed=True,
        oos_sharpe_ratio=1.8, oos_max_drawdown=.10, oos_win_rate=.60,
        oos_t_statistic=2.5, oos_daily_observations=365, oos_trade_count=100,
    )
    return replace(artifact, **changes)


def test_nineteen_independent_evidence_checks_remain_required() -> None:
    decision = LiveEligibilityGateEvaluator().evaluate(
        _artifact(), LiveEligibilityPolicy("live-gates-v1"), decided_at=NOW,
    )

    assert decision.decision == "CERTIFY"
    assert decision.evidence_tier == "LIVE_ELIGIBLE"
    assert decision.reason_codes == ()
    assert len(decision.gate_report) == 19
    assert all(item["result"] == "PASS" for item in decision.gate_report)
    assert len(decision.certification_digest) == 64
    assert decision.artifact_computed_at == NOW - timedelta(minutes=1)
    assert "guarantee profit" in decision.assumptions[-1]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"independent_blocks": 419}, "INDEPENDENT_BLOCK_POWER"),
        ({"pbo_probability": 0.10}, "BACKTEST_OVERFITTING"),
        ({"event_ev_robust_lcb": 0.0}, "EVENT_EV_ROBUST_LCB"),
        ({"leave_one_regime_out_ev": ()}, "LEAVE_ONE_REGIME_OUT"),
        ({"nested_walk_forward_complete": False}, "NESTED_WALK_FORWARD"),
        ({"cost_model_passed": False}, "COST_MODEL"),
    ],
)
def test_any_failed_gate_abstains(changes: dict[str, object], reason: str) -> None:
    decision = LiveEligibilityGateEvaluator().evaluate(
        _artifact(**changes), LiveEligibilityPolicy("live-gates-v1"), decided_at=NOW,
    )

    assert decision.decision == "ABSTAIN"
    assert decision.evidence_tier == "PAPER"
    assert reason in decision.reason_codes


def test_future_artifact_is_rejected() -> None:
    with pytest.raises(ValueError, match="future certification artifact"):
        LiveEligibilityGateEvaluator().evaluate(
            _artifact(computed_at=NOW + timedelta(seconds=1)),
            LiveEligibilityPolicy("live-gates-v1"),
            decided_at=NOW,
        )


def test_withdrawn_performance_floors_do_not_block_otherwise_valid_evidence():
    artifact = _artifact(oos_sharpe_ratio=-1., oos_max_drawdown=.5,
        oos_win_rate=.1, oos_t_statistic=-2., oos_daily_observations=1, oos_trade_count=1)
    decision = LiveEligibilityGateEvaluator().evaluate(
        artifact, LiveEligibilityPolicy("performance-observation-2"), decided_at=NOW)
    assert decision.decision == "CERTIFY"
    assert not any(item["gate_code"].startswith("OOS_") for item in decision.gate_report)
    changed = LiveEligibilityGateEvaluator().evaluate(
        replace(artifact, oos_sharpe_ratio=None), LiveEligibilityPolicy("performance-observation-2"), decided_at=NOW)
    assert changed.decision == "CERTIFY"
    assert changed.certification_digest != decision.certification_digest


def test_digest_is_deterministic_and_policy_bound() -> None:
    evaluator = LiveEligibilityGateEvaluator()
    first = evaluator.evaluate(_artifact(), LiveEligibilityPolicy("live-gates-v1"), decided_at=NOW)
    repeated = evaluator.evaluate(
        _artifact(), LiveEligibilityPolicy("live-gates-v1"), decided_at=NOW,
    )
    changed_policy = evaluator.evaluate(
        _artifact(), LiveEligibilityPolicy("live-gates-v2"), decided_at=NOW,
    )

    assert first.certification_digest == repeated.certification_digest
    assert first.certification_digest != changed_policy.certification_digest
