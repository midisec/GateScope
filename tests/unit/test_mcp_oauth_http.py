from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.mcp_oauth import CONSENT_PATH, OAUTH_COOKIE, build_mcp_oauth_router
from apps.mcp_server.main import GateScopeTokenVerifier, _principal, create_mcp_server
from gatescope.adapters.postgres_mcp import VerifiedMcpToken
from gatescope.adapters.postgres_oauth import OAuthError
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter, error_response

ORIGIN = "https://trade.example"
DISCOVERY_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)


class Storage:
    def __init__(self):
        self.calls = []

    def register_client(self, fields):
        self.calls.append(("register", fields))
        return {"client_id": "test-public-client", "token_endpoint_auth_method": "none"}

    def begin_authorization(self, query, nonce):
        if query.get("client_id") != "valid-client":
            raise OAuthError("invalid_client", "Unknown OAuth client", 401)
        self.calls.append(("begin", nonce))
        return "r" * 48

    def pending_authorization(self, handle, nonce, tenant, user):
        self.calls.append(("pending", handle, nonce, tenant, user))
        if len(nonce) < 40:
            raise OAuthError("invalid_request", "Authorization request is invalid or expired")
        return {"client_name": "ChatGPT", "scopes": ["mcp:connect"], "resource": ORIGIN + "/mcp",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "request_id": handle}

    def approve_authorization(self, *args):
        self.calls.append(("approve", *args))
        return "https://chatgpt.com/connector_platform_oauth_redirect?code=test&iss=" + ORIGIN

    def deny_authorization(self, *args):
        self.calls.append(("deny", *args))
        return "https://chatgpt.com/connector_platform_oauth_redirect?error=access_denied&iss=" + ORIGIN

    def exchange_code(self, **kwargs):
        self.calls.append(("exchange", kwargs))
        return {"access_token": "test-only-access", "token_type": "Bearer", "expires_in": 900}

    def refresh(self, **kwargs):
        self.calls.append(("refresh", kwargs))
        return {"access_token": "test-only-rotated", "token_type": "Bearer", "expires_in": 900}

    def revoke(self, **kwargs):
        self.calls.append(("revoke", kwargs))


class Identity:
    def __init__(self):
        self.current = None
        self.lookups = 0

    def session(self, token):
        self.lookups += 1
        return self.current

    def valid_csrf(self, session, cookie, header):
        return cookie == header == "csrf-for-test"


@pytest.fixture
def setup():
    storage, identity = Storage(), Identity()
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, auth=identity, public_origin=ORIGIN,
                       cookie_secure=True, limiter=SlidingWindowLimiter())

    def require(request):
        session = request.state.auth_session
        if session.role != "OWNER":
            return error_response(403, "ROLE_FORBIDDEN", "OWNER required", request.state.trace_id)
        if not session.recent:
            return error_response(403, "RECENT_AUTH_REQUIRED", "Confirm password", request.state.trace_id)
        return None

    app.include_router(build_mcp_oauth_router(None, public_origin=ORIGIN, cookie_secure=True,
                                             require_credential_write=require, repository=storage))

    @app.get(CONSENT_PATH)
    def shell():
        return {"shell": True}

    @app.get("/api/private")
    def private():
        return {"private": True}

    with TestClient(app, base_url=ORIGIN) as client:
        yield client, storage, identity


def sign_in(client, identity, *, full=True, recent=True, role="OWNER"):
    identity.current = SimpleNamespace(session_id=uuid4(), tenant_id=uuid4(), user_id=uuid4(),
                                       username="owner", role=role, fully_authenticated=full, recent=recent)
    client.cookies.set("gatescope_csrf", "csrf-for-test")
    client.cookies.set(OAUTH_COOKIE, "n" * 48)


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"])
def test_public_resource_discovery(setup, path):
    client, _, identity = setup
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["resource"] == ORIGIN + "/mcp"
    assert response.json()["authorization_servers"] == [ORIGIN]
    assert response.headers["cache-control"] == "no-store"
    assert identity.lookups == 0


def test_configured_transport_alias_only_reaches_repository_not_public_metadata(monkeypatch):
    alias = ("https://tunnel-service.gateway.unified-0.internal.api.openai.org/v1/mcp/"
             "tunnel_0123456789abcdef0123456789abcdef")
    constructed = []

    def repository(pool, **options):
        constructed.append(options)
        return Storage()

    monkeypatch.setattr("apps.api.mcp_oauth.PostgresOAuthRepository", repository)
    app = FastAPI()
    app.include_router(build_mcp_oauth_router(
        None, public_origin=ORIGIN, cookie_secure=True,
        require_credential_write=lambda request: None, resource_aliases=(alias,),
    ))
    assert constructed == [{"public_origin": ORIGIN, "resource_aliases": (alias,)}]
    with TestClient(app, base_url=ORIGIN) as client:
        prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
        authorization = client.get("/.well-known/oauth-authorization-server").json()
    assert prm["resource"] == ORIGIN + "/mcp"
    assert prm["authorization_servers"] == [ORIGIN]
    assert authorization["issuer"] == ORIGIN
    assert alias not in str(prm) + str(authorization)


def test_authorization_discovery_matches_oauth21_contract(setup):
    client, _, _ = setup
    result = client.get("/.well-known/oauth-authorization-server").json()
    assert result["issuer"] == ORIGIN
    assert result["code_challenge_methods_supported"] == ["S256"]
    assert result["authorization_response_iss_parameter_supported"] is True
    assert result["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert set(result["token_endpoint_auth_methods_supported"]) == {"none", "client_secret_post", "client_secret_basic"}
    assert "client_id_metadata_document_supported" not in result
    assert not {"openid", "email", "orders:write"} & set(result["scopes_supported"])
    assert "research:read" in result["scopes_supported"]
    assert {"paper:read", "paper:trade"} <= set(result["scopes_supported"])


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
@pytest.mark.parametrize("trailing_slash", [False, True])
@pytest.mark.parametrize("full_session", [None, False, True])
def test_discovery_get_head_options_exact_paths_ignore_all_browser_sessions(setup, path, trailing_slash, full_session):
    client, _, identity = setup
    if full_session is not None:
        sign_in(client, identity, full=full_session)
    target = path + ("/" if trailing_slash else "")
    metadata = client.get(target, follow_redirects=False)
    assert metadata.status_code == 200 and "location" not in metadata.headers
    assert metadata.headers["content-type"] == "application/json"
    assert metadata.headers["cache-control"] == "no-store"
    assert metadata.headers["access-control-allow-origin"] == "*"
    canonical = client.get(path)
    assert metadata.json() == canonical.json()
    if path == DISCOVERY_PATHS[0]:
        assert metadata.json()["issuer"] == ORIGIN
    else:
        assert metadata.json()["resource"] == ORIGIN + "/mcp"
        assert metadata.json()["authorization_servers"] == [ORIGIN]
    head = client.head(target, follow_redirects=False)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"] == metadata.headers["content-type"]
    assert head.headers["content-length"] == metadata.headers["content-length"]
    assert head.headers["cache-control"] == "no-store"
    assert head.headers["access-control-allow-origin"] == "*"
    preflight = client.options(target, headers={"Origin": "https://chatgpt.com",
                               "Access-Control-Request-Method": "HEAD"}, follow_redirects=False)
    assert preflight.status_code == 204 and preflight.content == b""
    assert "HEAD" in preflight.headers["access-control-allow-methods"].split(", ")
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert identity.lookups == 0


@pytest.mark.parametrize("path", ["/.well-known/oauth-authorization-server/mcp",
    "/mcp/.well-known/oauth-authorization-server", "/mcp/.well-known/oauth-protected-resource",
    "/.well-known/openid-configuration", "/.well-known/oauth-protected-resource/private",
    "/api/private"])
def test_discovery_compatibility_does_not_make_guessed_or_private_paths_public(setup, path):
    client, _, identity = setup
    for method in ("GET", "HEAD", "OPTIONS"):
        assert client.request(method, path, follow_redirects=False).status_code == 401
    sign_in(client, identity, full=False)
    assert client.get(path, follow_redirects=False).status_code == 403


def test_discovery_has_separate_budget_from_regular_anonymous_requests(setup):
    client, _, _ = setup
    for _ in range(80):
        assert client.get("/api/private").status_code == 401
    assert client.get("/api/private").status_code == 429
    for path in DISCOVERY_PATHS:
        assert client.get(path).status_code == 200
        assert client.head(path + "/").status_code == 200
        assert client.options(path).status_code == 204


def test_discovery_aliases_share_one_120_request_budget_without_starving_other_paths(setup):
    client, _, identity = setup
    paths = [variant for path in DISCOVERY_PATHS for variant in (path, path + "/")]
    for index in range(120):
        assert client.get(paths[index % len(paths)]).status_code == 200
    response = client.get(DISCOVERY_PATHS[0])
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert response.headers["cache-control"] == "no-store"
    assert identity.lookups == 0
    assert client.get("/api/private").status_code == 401


def test_authorize_sets_hardened_nonce_and_opaque_local_redirect(setup):
    client, storage, _ = setup
    response = client.get("/oauth/authorize", params={"client_id": "valid-client"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == CONSENT_PATH + "?request=" + "r" * 48
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "state" not in response.headers["location"]
    assert len(storage.calls[-1][1]) >= 40


def test_invalid_authorization_does_not_redirect_to_untrusted_client(setup):
    client, storage, _ = setup
    response = client.get("/oauth/authorize?client_id=bad&redirect_uri=https://evil.invalid", follow_redirects=False)
    assert response.status_code == 401 and response.json()["error"] == "invalid_client"
    assert "location" not in response.headers and not storage.calls


@pytest.mark.parametrize("full", [True, False])
def test_shell_preserves_path_but_consent_remains_authenticated(setup, full):
    client, _, identity = setup
    assert client.get(CONSENT_PATH + "?request=" + "r" * 48).status_code == 200
    assert client.get("/api/v2/mcp/oauth/consent").status_code == 401
    sign_in(client, identity, full=full)
    assert client.get(CONSENT_PATH).status_code == 200
    response = client.get("/api/v2/mcp/oauth/consent", params={"request": "r" * 48})
    assert response.status_code == (200 if full else 403)


def test_consent_csrf_recent_auth_and_explicit_approval(setup):
    client, storage, identity = setup
    sign_in(client, identity, recent=False)
    payload = {"request_id": "r" * 48, "decision": "approve"}
    assert client.post("/api/v2/mcp/oauth/consent", json=payload).status_code == 403
    headers = {"X-CSRF-Token": "csrf-for-test"}
    response = client.post("/api/v2/mcp/oauth/consent", json=payload, headers=headers)
    assert response.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
    assert not storage.calls
    identity.current.recent = True
    response = client.post("/api/v2/mcp/oauth/consent", json=payload, headers=headers)
    assert response.status_code == 200 and storage.calls[-1][0] == "approve"


def test_nonowner_cannot_approve_and_stale_session_can_decline(setup):
    client, storage, identity = setup
    sign_in(client, identity, role="VIEWER", recent=False)
    headers = {"X-CSRF-Token": "csrf-for-test"}
    response = client.post("/api/v2/mcp/oauth/consent", json={"request_id": "r" * 48, "decision": "approve"}, headers=headers)
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN" and not storage.calls
    identity.current.role = "OWNER"
    response = client.post("/api/v2/mcp/oauth/consent", json={"request_id": "r" * 48, "decision": "deny"}, headers=headers)
    assert response.status_code == 200 and storage.calls[-1][0] == "deny"


def test_explicit_consent_scope_subset_reaches_owner_bound_storage(setup):
    client, storage, identity = setup
    sign_in(client, identity)
    selected = ["mcp:connect", "research:read"]
    response = client.post("/api/v2/mcp/oauth/consent", headers={"X-CSRF-Token": "csrf-for-test"},
                           json={"request_id": "r" * 48, "decision": "approve", "approved_scopes": selected})
    assert response.status_code == 200
    assert storage.calls[-1] == ("approve", "r" * 48, "n" * 48,
                                 identity.current.tenant_id, identity.current.user_id, selected)


@pytest.mark.parametrize("selected", [None, [], "mcp:connect research:read", ["research:read"],
    ["mcp:connect", "alerts:read"], ["mcp:connect", "orders:write"], ["mcp:connect", {}],
    ["mcp:connect", "research:read", "research:read"], ["mcp:connect", "paper:trade"]])
def test_consent_rejects_malformed_or_non_oauth_scopes_before_storage(setup, selected):
    client, storage, identity = setup
    sign_in(client, identity)
    response = client.post("/api/v2/mcp/oauth/consent", headers={"X-CSRF-Token": "csrf-for-test"},
                           json={"request_id": "r" * 48, "decision": "approve", "approved_scopes": selected})
    assert response.status_code == 400 and not storage.calls


def test_deny_does_not_accept_selected_scopes(setup):
    client, storage, identity = setup
    sign_in(client, identity)
    response = client.post("/api/v2/mcp/oauth/consent", headers={"X-CSRF-Token": "csrf-for-test"},
                           json={"request_id": "r" * 48, "decision": "deny", "approved_scopes": ["mcp:connect"]})
    assert response.status_code == 400 and not storage.calls


def test_machine_endpoints_ignore_browser_session_and_allow_cors(setup):
    client, storage, identity = setup
    sign_in(client, identity, full=False)
    response = client.post("/oauth/token", data={"client_id": "public", "grant_type": "authorization_code",
                           "code": "code", "code_verifier": "v" * 43, "resource": ORIGIN + "/mcp"},
                           headers={"Origin": "https://chatgpt.com"})
    assert response.status_code == 200 and identity.lookups == 0
    assert response.headers["access-control-allow-origin"] == "*"
    assert storage.calls[-1][1]["auth_method"] == "none"
    assert client.post("/api/v2/mcp/oauth/consent", json={}).status_code == 403


@pytest.mark.parametrize("auth_method", ["client_secret_basic", "client_secret_post"])
def test_confidential_client_authentication(setup, auth_method):
    client, storage, _ = setup
    fields = {"grant_type": "refresh_token", "client_id": "client", "refresh_token": "r" * 48, "resource": ORIGIN + "/mcp"}
    headers = {}
    if auth_method == "client_secret_basic":
        headers["Authorization"] = "Basic " + base64.b64encode(b"client:secret").decode()
    else:
        fields["client_secret"] = "secret"
    assert client.post("/oauth/token", data=fields, headers=headers).status_code == 200
    assert storage.calls[-1][1]["auth_method"] == auth_method
    assert storage.calls[-1][1]["client_secret"] == "secret"


@pytest.mark.parametrize("body", ["client_id=a&client_id=b", "grant_type=client_credentials&client_id=a", "bad-form", "x=" + "a" * 17000])
def test_malformed_or_unsupported_token_request_rejected(setup, body):
    client, storage, _ = setup
    response = client.post("/oauth/token", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code in (400, 413) and not storage.calls
    assert isinstance(response.json()["error"], str)


def test_conflicting_client_auth_and_duplicate_json_rejected(setup):
    client, storage, _ = setup
    response = client.post("/oauth/token", data={"client_id": "other", "grant_type": "refresh_token"},
                           headers={"Authorization": "Basic " + base64.b64encode(b"client:secret").decode()})
    assert response.status_code == 401
    response = client.post("/oauth/register", content='{"client_name":"one","client_name":"two"}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and not storage.calls


def test_revocation_and_preflight_are_public_but_private_data_is_not(setup):
    client, storage, _ = setup
    assert client.options("/oauth/token").status_code == 204
    assert client.post("/oauth/revoke", data={"client_id": "client", "token": "token"}).status_code == 200
    assert storage.calls[-1][0] == "revoke"
    assert client.get("/api/private").status_code == 401


def test_dual_token_verifier_and_oauth_private_tool_block(monkeypatch):
    principal = VerifiedMcpToken(uuid4(), uuid4(), uuid4(), uuid4(), ("mcp:connect", "market:read"),
                                 datetime.now(timezone.utc) + timedelta(minutes=15))
    pat = SimpleNamespace(verify_token=lambda token: principal if token == "gsmcp_test" else None)
    oauth = SimpleNamespace(verify_access=lambda token: principal if token == "gsoat_test" else None)
    verifier = GateScopeTokenVerifier(pat, ORIGIN, oauth)
    oauth_access = anyio.run(verifier.verify_token, "gsoat_test")
    assert oauth_access.claims["aud"] == ORIGIN + "/mcp"
    assert oauth_access.claims["oauth_market_only"] is True
    monkeypatch.setattr("apps.mcp_server.main.get_access_token", lambda: oauth_access)
    assert _principal().id == principal.id
    with pytest.raises(PermissionError, match="market research"):
        _principal(market_only=False)
    pat_access = anyio.run(verifier.verify_token, "gsmcp_test")
    monkeypatch.setattr("apps.mcp_server.main.get_access_token", lambda: pat_access)
    assert _principal(market_only=False).id == principal.id
    assert anyio.run(verifier.verify_token, "gsoat_invalid") is None


def test_real_mcp_sdk_returns_resource_metadata_challenge(monkeypatch):
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused@127.0.0.1/unused")
    monkeypatch.setenv("GATESCOPE_MCP_PUBLIC_URL", ORIGIN + "/mcp")
    server = create_mcp_server()
    # No authenticated MCP session is started; no database is opened.
    client = TestClient(server.streamable_http_app(), base_url=ORIGIN)
    try:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert 'resource_metadata="' + ORIGIN + '/.well-known/oauth-protected-resource/mcp"' in challenge
    finally:
        client.close()
