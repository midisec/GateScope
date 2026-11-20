from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import apps.api.formal_app as formal
from gatescope.adapters.postgres_mcp import McpClient, McpNotFound, McpToken, NewMcpToken
from gatescope.auth import CSRF_COOKIE
from gatescope.domain.identity import Role
from gatescope.formal_auth import FormalInvalidCredentials
from gatescope.submission_crypto import RsaOaepSubmissionDecryptor, encrypt_submission_for_test


@pytest.fixture(scope="module")
def submission_key():
    return rsa.generate_private_key(public_exponent=65_537, key_size=3072)


@pytest.fixture
def control(monkeypatch, submission_key):
    """Exercise actual API routing/security with narrowly mocked persistence."""
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused:unused@localhost/unused")
    for key in ("GATESCOPE_IDENTITY_KEK", "GATESCOPE_RECOVERY_PEPPER", "GATESCOPE_CURSOR_SIGNING_KEY"):
        monkeypatch.setenv(key, encoded)
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEY_VERSION", "mcp-control-test")
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://trade.example")
    for key in ("GATESCOPE_TELEGRAM_BOT_USERNAME", "GATESCOPE_TELEGRAM_WEBHOOK_ID",
                "GATESCOPE_TELEGRAM_WEBHOOK_SECRET", "GATESCOPE_TELEGRAM_ALLOWED_USER_IDS",
                "GATESCOPE_COINGLASS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for helper in ("_optional_credential_encryptor", "_optional_telegram_config_encryptor",
                   "_optional_coinglass_config_encryptor"):
        monkeypatch.setattr(formal, helper, lambda: None)
    monkeypatch.setattr(formal, "PostgresPool", lambda _: Mock())
    monkeypatch.setattr(formal, "_optional_submission_decryptor", lambda: RsaOaepSubmissionDecryptor(
        submission_key, submission_key.public_key(), "mcp-control-test",
    ))
    now = datetime.now(timezone.utc)
    session = SimpleNamespace(
        tenant_id=uuid4(), user_id=uuid4(), session_id=uuid4(), role=Role.OWNER,
        recent_auth_at=now, mfa_verified_at=now, fully_authenticated=True,
        auth_strength="MFA", expires_at=now + timedelta(hours=12), username="owner",
    )
    identity = Mock()
    identity.session.return_value = session
    identity.valid_csrf.side_effect = lambda _, cookie, header: cookie == header == "csrf-fixture"
    identity.reauthenticate_password.return_value = session
    monkeypatch.setattr(formal, "FormalIdentityService", lambda *args: identity)
    client_record = McpClient(uuid4(), "Analysis client", "ACTIVE", 1, now, now)
    token = McpToken(uuid4(), client_record.id, "Read-only", "gsmcp_abcd...0000",
                     ("mcp:connect", "market:read"), now + timedelta(hours=1), None, None, now)
    repository = Mock()
    repository.create_client.return_value = client_record
    repository.list_clients.return_value = [client_record]
    repository.list_tokens.return_value = [token]
    repository.create_token.return_value = NewMcpToken(token, "gsmcp_" + "t" * 48)
    repository.revoke_client.return_value = client_record
    repository.revoke_token.return_value = token
    monkeypatch.setattr(formal, "PostgresMcpRepository", lambda _: repository)
    with TestClient(formal.create_formal_app(), base_url="https://trade.example") as browser:
        browser.cookies.set(CSRF_COOKIE, "csrf-fixture")
        yield SimpleNamespace(browser=browser, identity=identity, session=session, repository=repository,
                              client_record=client_record, token=token, key=submission_key,
                              headers={"X-CSRF-Token": "csrf-fixture"})


def reauth_body(control, *, password="fixture-password", user_id=None):
    envelope = encrypt_submission_for_test(
        control.key.public_key(), key_version="mcp-control-test",
        purpose=f"user:{user_id or control.session.user_id}:reauthenticate",
        request_id=uuid4().hex, issued_at=int(time.time() * 1000), payload={"password": password},
    )
    return {"encrypted_submission": envelope.model_dump()}


def token_body():
    return {"label": "Read-only", "scopes": ["mcp:connect", "market:read"],
            "ttl_hours": 1, "mfa_code": "123456"}


def test_owner_can_create_list_issue_and_revoke_own_mcp(control):
    browser, session, repo = control.browser, control.session, control.repository
    response = browser.post("/api/v2/mcp/clients", headers=control.headers, json={"name": "Analysis client"})
    assert response.status_code == 201
    assert response.json()["id"] == str(control.client_record.id)
    repo.create_client.assert_called_once_with(session.tenant_id, session.user_id, name="Analysis client")
    client_url = f"/api/v2/mcp/clients/{control.client_record.id}"
    issued = browser.post(f"{client_url}/tokens", headers=control.headers, json=token_body())
    assert issued.status_code == 201 and issued.json()["shown_once"]
    assert issued.headers["cache-control"] == "no-store"
    assert repo.create_token.call_args.args == (session.tenant_id, session.user_id, control.client_record.id)
    control.identity.verify_session_mfa.assert_called_once()
    listed = browser.get("/api/v2/mcp/clients")
    assert listed.status_code == 200 and len(listed.json()["items"][0]["tokens"]) == 1
    assert issued.json()["token"] not in listed.text
    repo.list_clients.assert_called_once_with(session.tenant_id, session.user_id)
    revoked = browser.post(f"{client_url}/tokens/{control.token.id}:revoke",
                           headers=control.headers, json={"code": "234567"})
    assert revoked.status_code == 200
    repo.revoke_token.assert_called_once_with(session.tenant_id, session.user_id, control.client_record.id, control.token.id)
    assert browser.post(f"{client_url}:revoke", headers=control.headers, json={"code": "345678"}).status_code == 200
    repo.revoke_client.assert_called_once_with(session.tenant_id, session.user_id, control.client_record.id)
    assert control.identity.verify_session_mfa.call_count == 3


def test_expired_owner_can_reauthenticate_without_rotating_session_or_mfa(control):
    session = control.session
    session.recent_auth_at -= timedelta(minutes=11)
    original = (session.session_id, session.mfa_verified_at, session.auth_strength)
    expired = control.browser.post("/api/v2/mcp/clients", headers=control.headers, json={"name": "Analysis client"})
    assert expired.status_code == 403 and expired.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
    control.repository.create_client.assert_not_called()

    def refresh(current, password, *, trace_id):
        assert current is session and password == "fixture-password"
        current.recent_auth_at = datetime.now(timezone.utc)
        return current

    control.identity.reauthenticate_password.side_effect = refresh
    confirmed = control.browser.post("/auth/reauthenticate", headers=control.headers, json=reauth_body(control))
    assert confirmed.status_code == 200 and confirmed.json()["reauthenticated"]
    assert confirmed.headers["cache-control"] == "no-store"
    assert "set-cookie" not in confirmed.headers
    assert original == (session.session_id, session.mfa_verified_at, session.auth_strength)
    assert control.browser.post("/api/v2/mcp/clients", headers=control.headers,
                                json={"name": "Analysis client"}).status_code == 201


@pytest.mark.parametrize("role", [Role.VIEWER, Role.RESEARCHER, Role.OPERATOR])
def test_other_roles_are_not_granted_mcp_credential_management(control, role):
    control.session.role = role
    result = control.browser.post("/api/v2/mcp/clients", headers=control.headers, json={"name": "Analysis client"})
    assert result.status_code == 403 and result.json()["error"]["code"] == "ROLE_FORBIDDEN"
    assert control.browser.get("/api/v2/mcp/clients").status_code == 403
    control.repository.create_client.assert_not_called()


@pytest.mark.parametrize("fully_authenticated,expected", [(None, 401), (False, 403)])
def test_mcp_and_reauthentication_do_not_bypass_login_or_mfa(control, fully_authenticated, expected):
    if fully_authenticated is None:
        control.identity.session.return_value = None
    else:
        control.session.fully_authenticated = False
    for path, body in (("/api/v2/mcp/clients", {"name": "Analysis client"}),
                       ("/auth/reauthenticate", reauth_body(control))):
        assert control.browser.post(path, headers=control.headers, json=body).status_code == expected
    control.identity.reauthenticate_password.assert_not_called()
    control.repository.create_client.assert_not_called()


def test_mcp_and_reauthentication_require_csrf(control):
    for path, body in (("/api/v2/mcp/clients", {"name": "Analysis client"}),
                       ("/auth/reauthenticate", reauth_body(control)),
                       (f"/api/v2/mcp/clients/{control.client_record.id}/tokens", token_body())):
        response = control.browser.post(path, json=body)
        assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_REJECTED"
    control.identity.reauthenticate_password.assert_not_called()
    control.repository.create_client.assert_not_called()
    control.repository.create_token.assert_not_called()


def test_mfa_failure_cannot_issue_tokens(control):
    control.identity.verify_session_mfa.side_effect = FormalInvalidCredentials("bad code")
    response = control.browser.post(f"/api/v2/mcp/clients/{control.client_record.id}/tokens",
                                    headers=control.headers, json=token_body())
    assert response.status_code == 401 and response.json()["error"]["code"] == "INVALID_MFA_CODE"
    control.repository.create_token.assert_not_called()


def test_foreign_client_identifier_is_scoped_to_authenticated_owner(control):
    foreign = uuid4()
    control.repository.create_token.side_effect = McpNotFound("private database details")
    result = control.browser.post(f"/api/v2/mcp/clients/{foreign}/tokens",
                                  headers=control.headers, json=token_body())
    assert result.status_code == 404 and "private database" not in result.text
    assert control.repository.create_token.call_args.args == (control.session.tenant_id, control.session.user_id, foreign)


def test_public_reauthentication_rejects_plaintext_and_wrong_user_envelope(control):
    plaintext = control.browser.post("/auth/reauthenticate", headers=control.headers,
                                     json={"password": "fixture-password"})
    assert plaintext.status_code == 403 and plaintext.json()["error"]["code"] == "ENCRYPTED_SUBMISSION_REQUIRED"
    wrong_owner = control.browser.post("/auth/reauthenticate", headers=control.headers,
                                       json=reauth_body(control, user_id=uuid4()))
    assert wrong_owner.status_code == 422 and wrong_owner.json()["error"]["code"] == "SUBMISSION_PURPOSE_MISMATCH"
    control.identity.reauthenticate_password.assert_not_called()


def test_reauthentication_envelope_is_single_use(control):
    body = reauth_body(control)
    assert control.browser.post("/auth/reauthenticate", headers=control.headers, json=body).status_code == 200
    replayed = control.browser.post("/auth/reauthenticate", headers=control.headers, json=body)
    assert replayed.status_code == 409 and replayed.json()["error"]["code"] == "SUBMISSION_REPLAYED"
    control.identity.reauthenticate_password.assert_called_once()


def test_reauthentication_failure_budget_is_user_scoped(control):
    control.identity.reauthenticate_password.side_effect = FormalInvalidCredentials("private password hash")
    for _ in range(5):
        response = control.browser.post("/auth/reauthenticate", headers=control.headers,
                                        json=reauth_body(control, password="wrong-password-fixture"))
        assert response.status_code == 401 and "private password" not in response.text
        control.session.session_id = uuid4()  # New sessions cannot reset this user's budget.
    limited = control.browser.post("/auth/reauthenticate", headers=control.headers, json=reauth_body(control))
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "REAUTH_RATE_LIMITED"
    assert int(limited.headers["retry-after"]) > 0
    assert control.identity.reauthenticate_password.call_count == 5
