from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from gatescope.formal_auth import _parse_session_token, _session_token


def test_session_locator_is_tenant_user_and_session_bound() -> None:
    tenant = UUID("10000000-0000-0000-0000-000000000001")
    user = UUID("10000000-0000-0000-0000-000000000002")
    session = UUID("10000000-0000-0000-0000-000000000003")
    token = _session_token(tenant, user, session, "s" * 43)
    assert _parse_session_token(token) == (tenant, user, session)
    assert _parse_session_token(token.replace(tenant.hex, "invalid", 1)) is None
    assert _parse_session_token("short") is None


def test_formal_identity_constructor_rejects_weak_secret_policy() -> None:
    from gatescope.formal_auth import FormalIdentityService
    from gatescope.security_secrets import AesGcmSecretProtector

    protector = AesGcmSecretProtector(b"k" * 32, "v1")
    with pytest.raises(ValueError, match="pepper"):
        FormalIdentityService(None, protector, b"weak")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ttl"):
        FormalIdentityService(None, protector, b"p" * 32, session_ttl=timedelta(minutes=1))  # type: ignore[arg-type]
