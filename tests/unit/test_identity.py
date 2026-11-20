from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag

from gatescope.domain.identity import (
    Action,
    AuthorizationContext,
    AuthorizationDenied,
    Role,
    authorize,
    authorize_tenant,
)
from gatescope.security_secrets import (
    AesGcmSecretProtector,
    generate_recovery_codes,
    generate_totp_secret,
    recovery_code_hash,
    totp_code,
    verify_totp,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TENANT = UUID("10000000-0000-0000-0000-000000000001")
USER = UUID("10000000-0000-0000-0000-000000000002")


def context(role: Role, *, channel: str = "WEB", age_minutes: int = 0) -> AuthorizationContext:
    verified = NOW - timedelta(minutes=age_minutes)
    return AuthorizationContext(TENANT, USER, role, verified, verified, channel)


def test_rbac_is_deny_by_default_and_tenant_bound() -> None:
    authorize(context(Role.VIEWER), Action.MARKET_READ, now=NOW)
    with pytest.raises(AuthorizationDenied):
        authorize(context(Role.VIEWER), Action.ALERT_WRITE, now=NOW)
    with pytest.raises(AuthorizationDenied):
        authorize_tenant(context(Role.OWNER), UUID("20000000-0000-0000-0000-000000000001"))


def test_dangerous_actions_require_recent_mfa_and_channel() -> None:
    authorize(context(Role.OWNER), Action.FLATTEN, now=NOW)
    with pytest.raises(AuthorizationDenied, match="recent authentication"):
        authorize(context(Role.OWNER, age_minutes=11), Action.FLATTEN, now=NOW)
    without_mfa = AuthorizationContext(TENANT, USER, Role.OWNER, NOW, None, "WEB")
    with pytest.raises(AuthorizationDenied, match="MFA"):
        authorize(without_mfa, Action.FLATTEN, now=NOW)
    with pytest.raises(AuthorizationDenied, match="Web"):
        authorize(context(Role.OWNER, channel="TELEGRAM"), Action.LIVE_RESUME, now=NOW)
    authorize(context(Role.OPERATOR, age_minutes=60), Action.SAFE_KILL, now=NOW)


def test_totp_matches_rfc_vector_and_replay_step_is_rejected() -> None:
    totp_fixture = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    code, _ = totp_code(totp_fixture, at=59, digits=8)
    assert code == "94287082"
    six_digit, six_step = totp_code(totp_fixture, at=59)
    assert verify_totp(totp_fixture, six_digit, at=59) == six_step
    assert verify_totp(totp_fixture, six_digit, at=59, last_accepted_step=six_step) is None
    assert len(generate_totp_secret()) == 32


def test_mfa_secret_encryption_is_context_bound() -> None:
    protector = AesGcmSecretProtector(b"k" * 32, "test-v1")
    protected = protector.protect(b"never-store-plaintext", context=b"tenant:user:method")
    assert b"never-store-plaintext" not in protected.ciphertext
    assert protector.reveal(protected, context=b"tenant:user:method") == b"never-store-plaintext"
    with pytest.raises(InvalidTag):
        protector.reveal(protected, context=b"another-user")


def test_recovery_codes_are_high_entropy_and_peppered() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert recovery_code_hash(codes[0], b"p" * 32) == recovery_code_hash(codes[0].lower(), b"p" * 32)
    assert recovery_code_hash(codes[0], b"p" * 32) != recovery_code_hash(codes[0], b"q" * 32)
