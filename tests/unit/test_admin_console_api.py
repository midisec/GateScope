"""HTTP auth/MFA/CSRF and pagination contracts for the tenant admin API."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.admin_console import build_admin_console_router
from gatescope.application.admin_console import AdminConsoleError
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter

ORIGIN = "https://admin.example"
BASE = "/api/v2/admin"


@pytest.fixture
def http(monkeypatch):
    identity, service = Mock(), Mock()
    identity.session.return_value = None
    identity.valid_csrf.side_effect = lambda _session, cookie, header: cookie == header == "test-csrf"
    for method in ("overview", "users", "resources", "audit", "revoke_sessions"):
        getattr(service, method).return_value = {"scope": {"type": "CURRENT_TENANT"}}
    monkeypatch.setattr("apps.api.admin_console.AdminConsoleService", lambda _pool, _identity: service)
    app = FastAPI()
    app.include_router(build_admin_console_router(None, identity))
    app.add_middleware(SecurityMiddleware, auth=identity, public_origin=ORIGIN,
                       cookie_secure=True, limiter=SlidingWindowLimiter())
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, identity, service


def login(client, identity, *, role="OWNER", full=True, recent=True):
    now = datetime.now(timezone.utc)
    actor = SimpleNamespace(tenant_id=uuid4(), user_id=uuid4(), session_id=uuid4(), role=role,
        fully_authenticated=full, recent_auth_at=now if recent else now-timedelta(hours=1))
    identity.session.return_value = actor
    client.cookies.set("gatescope_csrf", "test-csrf")
    return actor


@pytest.mark.parametrize("path", ["overview", "users", "resources", "audit"])
@pytest.mark.parametrize("state,status,code", [("anonymous", 401, "AUTH_REQUIRED"), ("partial", 403, "MFA_REQUIRED"), ("viewer", 403, "ADMIN_OWNER_REQUIRED")])
def test_all_admin_reads_require_complete_current_tenant_owner(http, path, state, status, code):
    client, identity, service = http
    if state != "anonymous":
        login(client, identity, full=state != "partial", role="VIEWER" if state == "viewer" else "OWNER")
    response = client.get(f"{BASE}/{path}")
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert not service.mock_calls


def test_users_binds_actor_pagination_and_filters_without_accepting_tenant_override(http):
    client, identity, service = http
    actor = login(client, identity)
    response = client.get(BASE+"/users", params=dict(page=2, page_size=5, q="test", role="RESEARCHER", status="REVOKED", tenant_id=str(uuid4())))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    service.users.assert_called_once_with(actor, q="test", role="RESEARCHER", status="REVOKED", page=2, page_size=5)
    assert response.json()["trace_id"]


@pytest.mark.parametrize("query", ["page=0", "page=10001", "page_size=0", "page_size=101", "role=SUPERUSER", "status=UNKNOWN", "q="+"x"*81])
def test_invalid_queries_are_rejected_before_service(http, query):
    client, identity, service = http
    login(client, identity)
    assert client.get(BASE+"/users?"+query).status_code == 422
    service.users.assert_not_called()


def test_revoke_requires_csrf_and_recent_password_before_service(http):
    client, identity, service = http
    login(client, identity, recent=False)
    path = f"{BASE}/users/{uuid4()}/sessions:revoke"
    assert client.post(path).status_code == 403
    response = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
    service.revoke_sessions.assert_not_called()


def test_revoke_is_post_only_and_binds_current_actor_and_uuid(http):
    client, identity, service = http
    actor, target = login(client, identity), uuid4()
    path = f"{BASE}/users/{target}/sessions:revoke"
    assert client.get(path).status_code == 405
    response = client.post(path, headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 200
    assert service.revoke_sessions.call_args.args == (actor, target)
    assert service.revoke_sessions.call_args.kwargs["trace_id"]


@pytest.mark.parametrize("error,status,code", [(AdminConsoleError(404, "USER_NOT_FOUND", "该成员不属于当前工作区"), 404, "USER_NOT_FOUND"),
    (AdminConsoleError(409, "SELF_SESSION_REVOKE_FORBIDDEN", "不能退出自己"), 409, "SELF_SESSION_REVOKE_FORBIDDEN"),
    (RuntimeError("password=secret-value SELECT credential_json FROM internal_table"), 503, "ADMIN_STATS_UNAVAILABLE")])
def test_expected_and_unexpected_errors_never_leak_private_details(http, error, status, code):
    client, identity, service = http
    login(client, identity)
    service.revoke_sessions.side_effect = error
    response = client.post(f"{BASE}/users/{uuid4()}/sessions:revoke", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "secret-value" not in response.text and "internal_table" not in response.text
    if status == 503:
        assert response.headers["retry-after"] == "5"
