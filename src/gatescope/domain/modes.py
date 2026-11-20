"""The four orthogonal decision/execution axes frozen by the V2 design."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    ABSTAIN = "ABSTAIN"
    REJECT = "REJECT"
    ELIGIBLE = "ELIGIBLE"


class EvidenceTier(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"


class Environment(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class AuthorizationMode(str, Enum):
    NONE = "NONE"
    MANUAL = "MANUAL"
    AUTO = "AUTO"


@dataclass(frozen=True)
class ModeCombination:
    """Validated compatibility view; canonical facts stay in separate snapshots."""

    decision: Decision
    evidence_tier: EvidenceTier
    environment: Environment
    authorization_mode: AuthorizationMode

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.decision in {Decision.ABSTAIN, Decision.REJECT}:
            if self.authorization_mode is not AuthorizationMode.NONE:
                errors.append("ABSTAIN/REJECT requires authorization_mode=NONE")

        if self.evidence_tier in {EvidenceTier.RESEARCH, EvidenceTier.SHADOW}:
            if self.environment is not Environment.PAPER:
                errors.append("RESEARCH/SHADOW evidence cannot target TESTNET/LIVE")
            if self.authorization_mode is not AuthorizationMode.NONE:
                errors.append("RESEARCH/SHADOW evidence cannot authorize an order")

        if self.evidence_tier is EvidenceTier.PAPER and self.environment is not Environment.PAPER:
            errors.append("PAPER evidence can only run in PAPER")

        if self.environment in {Environment.TESTNET, Environment.LIVE}:
            if self.evidence_tier is not EvidenceTier.LIVE_ELIGIBLE:
                errors.append("TESTNET/LIVE requires LIVE_ELIGIBLE evidence")
            if self.decision is Decision.ELIGIBLE and self.authorization_mode is AuthorizationMode.NONE:
                errors.append("eligible TESTNET/LIVE execution requires MANUAL or AUTO authorization")

        if errors:
            raise ValueError("; ".join(errors))

