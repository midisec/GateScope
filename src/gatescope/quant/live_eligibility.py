"""Fail-closed aggregation of independently computed LIVE eligibility evidence.

This module verifies a sealed upstream artifact against a pre-registered policy.
It does not estimate bootstrap, DSR or PBO statistics and cannot upgrade PAPER
samples by itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass(frozen=True)
class LiveEligibilityPolicy:
    policy_version: str
    minimum_independent_blocks: int = 300
    maximum_holm_adjusted_p: float = 0.05
    minimum_dsr_probability: float = 0.95
    maximum_pbo_probability: float = 0.10
    maximum_regime_profit_contribution: float = 0.70
    minimum_regime_blocks: int = 100
    minimum_event_ev_lcb: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= len(self.policy_version) <= 80:
            raise ValueError("live eligibility policy version is invalid")
        if self.minimum_independent_blocks < 300 or self.minimum_regime_blocks < 20:
            raise ValueError("live eligibility sample floors are too low")
        probabilities = (
            self.maximum_holm_adjusted_p,
            self.minimum_dsr_probability,
            self.maximum_pbo_probability,
            self.maximum_regime_profit_contribution,
        )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
            raise ValueError("policy probabilities must be finite in [0,1]")
        if not math.isfinite(self.minimum_event_ev_lcb):
            raise ValueError("event EV threshold must be finite")


@dataclass(frozen=True)
class LiveEligibilityArtifact:
    strategy_version_ref: str
    source_artifact_digest: str
    computed_at: datetime
    independent_blocks: int
    required_power_blocks: int
    holm_adjusted_p_value: float
    policy_value_lcb: float
    deflated_sharpe_probability: float
    pbo_probability: float
    base_cost_oos_ev: float
    double_cost_oos_ev: float
    event_ev_robust_lcb: float
    maximum_regime_profit_contribution: float
    minimum_allowed_regime_blocks: int
    leave_one_regime_out_ev: tuple[float, ...]
    trial_registry_complete: bool
    nested_walk_forward_complete: bool
    sealed_holdout_consumed_once: bool
    calibration_passed: bool
    maximum_drawdown_passed: bool
    data_quality_passed: bool
    out_of_distribution_passed: bool
    cost_model_passed: bool
    oos_sharpe_ratio: float | None = None
    oos_max_drawdown: float | None = None
    oos_win_rate: float | None = None
    oos_t_statistic: float | None = None
    oos_daily_observations: int = 0
    oos_trade_count: int = 0

    def __post_init__(self) -> None:
        if not 1 <= len(self.strategy_version_ref) <= 120:
            raise ValueError("strategy version reference is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.source_artifact_digest) is None:
            raise ValueError("source artifact digest is invalid")
        if self.computed_at.tzinfo is None:
            raise ValueError("artifact time must include a timezone")
        if self.independent_blocks < 0 or self.required_power_blocks < 1:
            raise ValueError("artifact block counts are invalid")
        if self.minimum_allowed_regime_blocks < 0:
            raise ValueError("regime block count is invalid")
        probabilities = (
            self.holm_adjusted_p_value,
            self.deflated_sharpe_probability,
            self.pbo_probability,
            self.maximum_regime_profit_contribution,
        )
        reals = (
            self.policy_value_lcb,
            self.base_cost_oos_ev,
            self.double_cost_oos_ev,
            self.event_ev_robust_lcb,
            *self.leave_one_regime_out_ev,
        )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
            raise ValueError("artifact probabilities must be finite in [0,1]")
        if any(not math.isfinite(value) for value in reals):
            raise ValueError("artifact statistics must be finite")


@dataclass(frozen=True)
class LiveEligibilityDecision:
    decision: Literal["CERTIFY", "ABSTAIN"]
    evidence_tier: Literal["LIVE_ELIGIBLE", "PAPER"]
    reason_codes: tuple[str, ...]
    gate_report: tuple[dict[str, Any], ...]
    strategy_version_ref: str
    source_artifact_digest: str
    artifact_computed_at: datetime
    policy_version: str
    decided_at: datetime
    assumptions: tuple[str, ...]
    certification_digest: str


class LiveEligibilityGateEvaluator:
    """Reject the whole artifact when any pre-registered gate fails."""

    ASSUMPTIONS = (
        "source artifact was produced by the registered immutable research pipeline",
        "block bootstrap, DSR and PBO implementations were independently reproduced",
        "all trials including failed and abandoned variants remain in the family registry",
        "returns are net of fill-to-fill fees, spread, impact, slippage and funding",
        "certification does not imply future stationarity or guarantee profit",
    )

    def evaluate(
        self,
        artifact: LiveEligibilityArtifact,
        policy: LiveEligibilityPolicy,
        *,
        decided_at: datetime,
    ) -> LiveEligibilityDecision:
        if decided_at.tzinfo is None:
            raise ValueError("decision time must include a timezone")
        decided = decided_at.astimezone(timezone.utc)
        if artifact.computed_at.astimezone(timezone.utc) > decided:
            raise ValueError("future certification artifact is unavailable at decision time")
        gates: list[dict[str, Any]] = []

        def gate(code: str, passed: bool, value: Any, threshold: Any) -> None:
            gates.append({
                "gate_code": code,
                "result": "PASS" if passed else "FAIL",
                "value": value,
                "threshold": threshold,
            })

        required = max(policy.minimum_independent_blocks, artifact.required_power_blocks)
        gate("INDEPENDENT_BLOCK_POWER", artifact.independent_blocks >= required,
             artifact.independent_blocks, required)
        gate("HOLM_FWER", artifact.holm_adjusted_p_value <= policy.maximum_holm_adjusted_p,
             _stable(artifact.holm_adjusted_p_value), _stable(policy.maximum_holm_adjusted_p))
        gate("POLICY_VALUE_LCB", artifact.policy_value_lcb > 0,
             _stable(artifact.policy_value_lcb), ">0")
        gate("DEFLATED_SHARPE", artifact.deflated_sharpe_probability >= policy.minimum_dsr_probability,
             _stable(artifact.deflated_sharpe_probability), _stable(policy.minimum_dsr_probability))
        gate("BACKTEST_OVERFITTING", artifact.pbo_probability < policy.maximum_pbo_probability,
             _stable(artifact.pbo_probability), f"<{_stable(policy.maximum_pbo_probability)}")
        gate("BASE_COST_OOS_EV", artifact.base_cost_oos_ev > 0,
             _stable(artifact.base_cost_oos_ev), ">0")
        gate("DOUBLE_COST_OOS_EV", artifact.double_cost_oos_ev > 0,
             _stable(artifact.double_cost_oos_ev), ">0")
        gate("EVENT_EV_ROBUST_LCB", artifact.event_ev_robust_lcb > policy.minimum_event_ev_lcb,
             _stable(artifact.event_ev_robust_lcb), f">{_stable(policy.minimum_event_ev_lcb)}")
        gate("REGIME_CONCENTRATION",
             artifact.maximum_regime_profit_contribution <= policy.maximum_regime_profit_contribution,
             _stable(artifact.maximum_regime_profit_contribution),
             _stable(policy.maximum_regime_profit_contribution))
        gate("REGIME_BLOCKS", artifact.minimum_allowed_regime_blocks >= policy.minimum_regime_blocks,
             artifact.minimum_allowed_regime_blocks, policy.minimum_regime_blocks)
        gate("LEAVE_ONE_REGIME_OUT", bool(artifact.leave_one_regime_out_ev)
             and all(value > 0 for value in artifact.leave_one_regime_out_ev),
             [_stable(value) for value in artifact.leave_one_regime_out_ev], "all >0 and non-empty")
        for code, passed in (
            ("TRIAL_REGISTRY_COMPLETE", artifact.trial_registry_complete),
            ("NESTED_WALK_FORWARD", artifact.nested_walk_forward_complete),
            ("SEALED_HOLDOUT_CONSUMED_ONCE", artifact.sealed_holdout_consumed_once),
            ("CALIBRATION", artifact.calibration_passed),
            ("MAXIMUM_DRAWDOWN", artifact.maximum_drawdown_passed),
            ("DATA_QUALITY", artifact.data_quality_passed),
            ("OUT_OF_DISTRIBUTION", artifact.out_of_distribution_passed),
            ("COST_MODEL", artifact.cost_model_passed),
        ):
            gate(code, passed, passed, True)

        # The four user-specified performance floors were withdrawn. Their raw
        # values remain bound into the artifact digest, never fabricated PASSes.
        # Independent evidence integrity/quality and execution authorization are
        # separate controls, not replaced by a research shortlist flag.
        reasons = tuple(item["gate_code"] for item in gates if item["result"] != "PASS")
        certified = not reasons
        canonical = {
            "artifact": _artifact_payload(artifact),
            "assumptions": self.ASSUMPTIONS,
            "decided_at": decided.isoformat(),
            "decision": "CERTIFY" if certified else "ABSTAIN",
            "evidence_tier": "LIVE_ELIGIBLE" if certified else "PAPER",
            "gate_report": gates,
            "policy": _policy_payload(policy),
            "reason_codes": reasons,
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        return LiveEligibilityDecision(
            decision="CERTIFY" if certified else "ABSTAIN",
            evidence_tier="LIVE_ELIGIBLE" if certified else "PAPER",
            reason_codes=reasons,
            gate_report=tuple(gates),
            strategy_version_ref=artifact.strategy_version_ref,
            source_artifact_digest=artifact.source_artifact_digest,
            artifact_computed_at=artifact.computed_at.astimezone(timezone.utc),
            policy_version=policy.policy_version,
            decided_at=decided,
            assumptions=self.ASSUMPTIONS,
            certification_digest=digest,
        )


def _artifact_payload(value: LiveEligibilityArtifact) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.__dict__.items():
        if isinstance(item, datetime):
            result[key] = item.astimezone(timezone.utc).isoformat()
        elif isinstance(item, float):
            result[key] = _stable(item)
        elif isinstance(item, tuple):
            result[key] = [_stable(number) for number in item]
        else:
            result[key] = item
    return result


def _policy_payload(value: LiveEligibilityPolicy) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.__dict__.items():
        result[key] = _stable(item) if isinstance(item, float) else item
    return result


def _stable(value: float) -> str:
    return format(value, ".17g")
