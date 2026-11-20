"""Distribution-free lower bounds for PAPER research evidence.

The guarantees here are conditional on pre-registered, independent, bounded and
stationary outcome blocks. They are not claims that markets remain stationary.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal


@dataclass(frozen=True)
class OutcomeBlock:
    block_id: str
    available_at: datetime
    net_return: float

    def __post_init__(self) -> None:
        if not self.block_id or len(self.block_id) > 128:
            raise ValueError("outcome block_id must contain 1-128 characters")
        if self.available_at.tzinfo is None:
            raise ValueError("outcome available_at must include a timezone")
        if not math.isfinite(self.net_return):
            raise ValueError("outcome net_return must be finite")


@dataclass(frozen=True)
class ProbabilityPolicy:
    """Pre-registered bounds and family-wise error budget."""

    policy_version: str
    return_floor: float
    return_ceiling: float
    break_even_probability_upper: float
    minimum_blocks: int = 100
    family_alpha: float = 0.05
    tested_hypotheses: int = 1
    minimum_expected_return_lcb: float = 0.0

    def __post_init__(self) -> None:
        if not self.policy_version or len(self.policy_version) > 80:
            raise ValueError("policy_version must contain 1-80 characters")
        if not math.isfinite(self.return_floor) or not math.isfinite(self.return_ceiling):
            raise ValueError("return bounds must be finite")
        if self.return_floor >= self.return_ceiling:
            raise ValueError("return_floor must be lower than return_ceiling")
        if not 0 <= self.break_even_probability_upper <= 1:
            raise ValueError("break-even probability upper bound must be in [0,1]")
        if not 20 <= self.minimum_blocks <= 1_000_000:
            raise ValueError("minimum_blocks must be between 20 and 1,000,000")
        if not 0 < self.family_alpha <= 0.1:
            raise ValueError("family_alpha must be in (0,0.1]")
        if not 1 <= self.tested_hypotheses <= 1_000_000:
            raise ValueError("tested_hypotheses must be between 1 and 1,000,000")
        if not math.isfinite(self.minimum_expected_return_lcb):
            raise ValueError("minimum_expected_return_lcb must be finite")

    @property
    def alpha_per_hypothesis(self) -> float:
        return self.family_alpha / self.tested_hypotheses


@dataclass(frozen=True)
class GodDecision:
    decision: Literal["PAPER_ELIGIBLE", "ABSTAIN"]
    evidence_tier: Literal["PAPER", "RESEARCH"]
    reason_codes: tuple[str, ...]
    independent_blocks: int
    win_probability_estimate: float
    win_probability_lcb: float
    mean_net_return: float
    expected_net_return_lcb: float
    alpha_per_hypothesis: float
    decision_at: datetime
    policy_version: str
    assumptions: tuple[str, ...]
    evidence_digest: str


class GodEvidenceEvaluator:
    """Creates PAPER-only evidence; this component has no order port."""

    ASSUMPTIONS = (
        "outcome blocks are independent at the registered block width",
        "net returns include fees, spread, slippage and funding",
        "each net return lies inside the pre-registered bounded support",
        "the deployment distribution remains represented by the sealed holdout",
        "Bonferroni alpha allocation covers every tested research hypothesis",
    )

    def evaluate(
        self,
        samples: Iterable[OutcomeBlock],
        policy: ProbabilityPolicy,
        *,
        decision_at: datetime,
        net_of_costs: bool,
        sealed_holdout: bool,
        source_fresh: bool,
        in_distribution: bool,
        external_features_licensed: bool,
    ) -> GodDecision:
        if decision_at.tzinfo is None:
            raise ValueError("decision_at must include a timezone")
        cutoff = decision_at.astimezone(timezone.utc)
        blocks = tuple(sorted(samples, key=lambda item: (item.available_at, item.block_id)))
        if len({sample.block_id for sample in blocks}) != len(blocks):
            raise ValueError("outcome blocks must have unique block_id values")
        if any(sample.available_at.astimezone(timezone.utc) > cutoff for sample in blocks):
            raise ValueError("future outcome data is not available at decision_at")
        if any(
            sample.net_return < policy.return_floor or sample.net_return > policy.return_ceiling
            for sample in blocks
        ):
            raise ValueError("outcome lies outside the pre-registered return support")

        count = len(blocks)
        mean_return = sum(sample.net_return for sample in blocks) / count if count else 0.0
        wins = sum(sample.net_return > 0 for sample in blocks)
        win_probability = wins / count if count else 0.0
        alpha = policy.alpha_per_hypothesis
        if count:
            radius_unit = math.sqrt(math.log(1.0 / alpha) / (2.0 * count))
            win_lcb = max(0.0, win_probability - radius_unit)
            expected_lcb = mean_return - (
                (policy.return_ceiling - policy.return_floor) * radius_unit
            )
        else:
            win_lcb = 0.0
            expected_lcb = policy.return_floor

        reasons: list[str] = []
        if count < policy.minimum_blocks:
            reasons.append("INSUFFICIENT_INDEPENDENT_BLOCKS")
        if not net_of_costs:
            reasons.append("RETURNS_NOT_NET_OF_COSTS")
        if not sealed_holdout:
            reasons.append("HOLDOUT_NOT_SEALED")
        if not source_fresh:
            reasons.append("SOURCE_STALE")
        if not in_distribution:
            reasons.append("OUT_OF_DISTRIBUTION")
        if not external_features_licensed:
            reasons.append("EXTERNAL_FEATURE_LICENSE_BLOCKED")
        if win_lcb <= policy.break_even_probability_upper:
            reasons.append("WIN_PROBABILITY_LCB_NOT_ABOVE_BREAK_EVEN")
        if expected_lcb <= policy.minimum_expected_return_lcb:
            reasons.append("EXPECTED_RETURN_LCB_NOT_POSITIVE")

        eligible = not reasons
        canonical = {
            "alpha_per_hypothesis": _stable(alpha),
            "decision": "PAPER_ELIGIBLE" if eligible else "ABSTAIN",
            "decision_at": cutoff.isoformat(),
            "expected_net_return_lcb": _stable(expected_lcb),
            "independent_blocks": count,
            "mean_net_return": _stable(mean_return),
            "policy_version": policy.policy_version,
            "reason_codes": reasons,
            "sample_manifest": [
                {
                    "available_at": sample.available_at.astimezone(timezone.utc).isoformat(),
                    "block_id": sample.block_id,
                    "net_return": _stable(sample.net_return),
                }
                for sample in blocks
            ],
            "win_probability_estimate": _stable(win_probability),
            "win_probability_lcb": _stable(win_lcb),
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return GodDecision(
            decision="PAPER_ELIGIBLE" if eligible else "ABSTAIN",
            evidence_tier="PAPER" if eligible else "RESEARCH",
            reason_codes=tuple(reasons),
            independent_blocks=count,
            win_probability_estimate=win_probability,
            win_probability_lcb=win_lcb,
            mean_net_return=mean_return,
            expected_net_return_lcb=expected_lcb,
            alpha_per_hypothesis=alpha,
            decision_at=cutoff,
            policy_version=policy.policy_version,
            assumptions=self.ASSUMPTIONS,
            evidence_digest=digest,
        )


def _stable(value: float) -> str:
    return format(value, ".17g")
