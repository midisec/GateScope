from __future__ import annotations

import pytest

from gatescope.domain import AuthorizationMode, Decision, Environment, EvidenceTier, ModeCombination
from gatescope_executor import LiveTradingDisabled, build_private_adapter


@pytest.mark.parametrize(
    ("decision", "tier", "environment", "authorization"),
    [
        (Decision.ABSTAIN, EvidenceTier.RESEARCH, Environment.PAPER, AuthorizationMode.NONE),
        (Decision.ELIGIBLE, EvidenceTier.SHADOW, Environment.PAPER, AuthorizationMode.NONE),
        (Decision.ELIGIBLE, EvidenceTier.PAPER, Environment.PAPER, AuthorizationMode.MANUAL),
        (Decision.ELIGIBLE, EvidenceTier.LIVE_ELIGIBLE, Environment.TESTNET, AuthorizationMode.MANUAL),
        (Decision.ELIGIBLE, EvidenceTier.LIVE_ELIGIBLE, Environment.LIVE, AuthorizationMode.AUTO),
        (Decision.REJECT, EvidenceTier.LIVE_ELIGIBLE, Environment.LIVE, AuthorizationMode.NONE),
    ],
)
def test_legal_combinations(decision, tier, environment, authorization) -> None:
    value = ModeCombination(decision, tier, environment, authorization)
    assert value.environment is environment


@pytest.mark.parametrize(
    ("decision", "tier", "environment", "authorization"),
    [
        (Decision.REJECT, EvidenceTier.PAPER, Environment.PAPER, AuthorizationMode.MANUAL),
        (Decision.ELIGIBLE, EvidenceTier.RESEARCH, Environment.PAPER, AuthorizationMode.AUTO),
        (Decision.ELIGIBLE, EvidenceTier.PAPER, Environment.TESTNET, AuthorizationMode.MANUAL),
        (Decision.ELIGIBLE, EvidenceTier.LIVE_ELIGIBLE, Environment.LIVE, AuthorizationMode.NONE),
    ],
)
def test_illegal_combinations_fail(decision, tier, environment, authorization) -> None:
    with pytest.raises(ValueError):
        ModeCombination(decision, tier, environment, authorization)


def test_phase0_private_adapter_cannot_be_constructed() -> None:
    with pytest.raises(LiveTradingDisabled):
        build_private_adapter()
