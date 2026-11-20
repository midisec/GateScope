from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from gatescope.adapters.postgres_oauth import OAuthError, PostgresOAuthRepository, STABLE_CALLBACK
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, seed_two_tenants

ORIGIN = "https://43.108.44.74"
RESOURCE = f"{ORIGIN}/mcp"
TUNNEL_ALIAS = "https://tunnel-service.gateway.unified-0.internal.api.openai.org/v1/mcp/tunnel_0123456789abcdef0123456789abcdef"


@pytest.fixture
def oauth(migrated_database: str):
    seed_two_tenants(migrated_database)
    api_pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    mcp_pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_mcp"))
    api_pool.open()
    mcp_pool.open()
    try:
        yield (PostgresOAuthRepository(api_pool, public_origin=ORIGIN),
               PostgresOAuthRepository(mcp_pool, public_origin=ORIGIN), migrated_database)
    finally:
        api_pool.close()
        mcp_pool.close()


def registration(repo: PostgresOAuthRepository, method: str = "none") -> dict:
    return repo.register_client({"redirect_uris": [STABLE_CALLBACK], "client_name": "ChatGPT test",
                                 "token_endpoint_auth_method": method})


def authorization(repo: PostgresOAuthRepository, client: dict) -> tuple[str, str, str, dict]:
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(32)
    query = {"client_id": client["client_id"], "response_type": "code", "redirect_uri": STABLE_CALLBACK,
             "resource": RESOURCE, "scope": "mcp:connect market:read analysis:read", "state": secrets.token_urlsafe(24),
             "code_challenge_method": "S256",
             "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()}
    handle = repo.begin_authorization(query, nonce)
    return handle, nonce, verifier, query


def issued(repo: PostgresOAuthRepository, method: str = "none") -> tuple[dict, dict, dict]:
    client = registration(repo, method)
    handle, nonce, verifier, query = authorization(repo, client)
    view = repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
    assert view["resource"] == RESOURCE
    assert view["scopes"] == ["mcp:connect", "market:read", "analysis:read"]
    redirect = repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    parsed = parse_qs(urlsplit(redirect).query)
    assert parsed["state"] == [query["state"]]
    assert parsed["iss"] == [ORIGIN]
    request = {"client_id": client["client_id"], "client_secret": client.get("client_secret"),
               "auth_method": method, "code": parsed["code"][0], "code_verifier": verifier,
               "redirect_uri": STABLE_CALLBACK, "resource": RESOURCE}
    return client, repo.exchange_code(**request), request


def test_oauth_real_workload_roles_pkce_rotation_replay_and_hash_storage(oauth):
    repo, verifier, database_url = oauth
    client, first, _ = issued(repo)
    principal = verifier.verify_access(first["access_token"])
    assert principal is not None
    assert principal.tenant_id == TENANT_A and principal.subject_user_id == USER_A
    assert first["expires_in"] == 900
    assert PostgresMcpRepository(verifier.pool).verify_token(first["access_token"]) is None
    wrong_audience = PostgresOAuthRepository(verifier.pool, public_origin="https://other.example")
    assert wrong_audience.verify_access(first["access_token"]) is None
    with psycopg.connect(database_url) as connection:
        persisted = connection.execute("SELECT token_hash FROM mcp_tokens WHERE id=%s", (principal.id,)).fetchone()[0]
        assert persisted == hashlib.sha256(first["access_token"].encode()).hexdigest()
        refresh_hash = connection.execute("SELECT token_hash FROM oauth_refresh_tokens").fetchone()[0]
        assert refresh_hash == hashlib.sha256(first["refresh_token"].encode()).hexdigest()
    second = repo.refresh(client_id=client["client_id"], refresh_token=first["refresh_token"], resource=RESOURCE,
                          scope="mcp:connect market:read")
    assert verifier.verify_access(second["access_token"]) is not None
    assert second["scope"] == "mcp:connect market:read"
    with pytest.raises(OAuthError) as escalation:
        repo.refresh(client_id=client["client_id"], refresh_token=second["refresh_token"], resource=RESOURCE,
                     scope="mcp:connect market:read analysis:read")
    assert escalation.value.error == "invalid_scope"
    with pytest.raises(OAuthError) as reused:
        repo.refresh(client_id=client["client_id"], refresh_token=first["refresh_token"], resource=RESOURCE)
    assert reused.value.error == "invalid_grant"
    assert verifier.verify_access(first["access_token"]) is None
    assert verifier.verify_access(second["access_token"]) is None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=second["refresh_token"], resource=RESOURCE)


def test_research_scope_requires_requested_and_selected_new_grant(oauth):
    repo, verifier, _ = oauth
    client = registration(repo)
    assert set(client["scope"].split()) == {"mcp:connect", "market:read", "analysis:read", "research:read", "paper:read", "paper:trade"}
    handle, nonce, secret, query = authorization(repo, client)
    with pytest.raises(OAuthError) as escalation:
        repo.approve_authorization(handle, nonce, TENANT_A, USER_A, ["mcp:connect", "research:read"])
    assert escalation.value.error == "invalid_scope"

    omitted = repo.begin_authorization({key: value for key, value in query.items() if key != "scope"}, nonce)
    assert repo.pending_authorization(omitted, nonce, TENANT_A, USER_A)["scopes"] == ["mcp:connect", "market:read", "analysis:read"]

    query["scope"] += " research:read"
    results = []
    for selected in [None, ["mcp:connect", "research:read"]]:
        handle = repo.begin_authorization(query, nonce)
        assert "research:read" in repo.pending_authorization(handle, nonce, TENANT_A, USER_A)["scopes"]
        with pytest.raises(OAuthError):
            repo.approve_authorization(handle, nonce, TENANT_B, USER_B, selected)
        callback = repo.approve_authorization(handle, nonce, TENANT_A, USER_A, selected)
        code = parse_qs(urlsplit(callback).query)["code"][0]
        token = repo.exchange_code(client_id=client["client_id"], code=code, code_verifier=secret,
                                   redirect_uri=STABLE_CALLBACK, resource=RESOURCE)
        principal = verifier.verify_access(token["access_token"])
        assert principal is not None and principal.subject_user_id == USER_A and principal.tenant_id == TENANT_A
        results.append(token)
    assert results[0]["scope"] == "mcp:connect market:read analysis:read"
    assert results[1]["scope"] == "mcp:connect research:read"
    with pytest.raises(OAuthError) as escalation:
        repo.refresh(client_id=client["client_id"], refresh_token=results[0]["refresh_token"],
                     resource=RESOURCE, scope=query["scope"])
    assert escalation.value.error == "invalid_scope"
    narrowed = repo.refresh(client_id=client["client_id"], refresh_token=results[1]["refresh_token"],
                            resource=RESOURCE, scope="mcp:connect")
    assert narrowed["scope"] == "mcp:connect"
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=narrowed["refresh_token"],
                     resource=RESOURCE, scope="mcp:connect research:read")


def test_client_explicit_scope_ceiling_stays_narrow_after_research_support(oauth):
    repo, _, _ = oauth
    client = repo.register_client({"redirect_uris": [STABLE_CALLBACK], "scope": "mcp:connect market:read analysis:read"})
    _, nonce, _, query = authorization(repo, client)
    query["scope"] += " research:read"
    with pytest.raises(OAuthError) as escalation:
        repo.begin_authorization(query, nonce)
    assert escalation.value.error == "invalid_scope"


def test_paper_scopes_are_explicit_owner_bound_and_refresh_never_escalates(oauth):
    repo, verifier, _ = oauth
    client = registration(repo)
    _, nonce, secret, query = authorization(repo, client)
    query["scope"] += " research:read paper:read paper:trade"
    results = []
    for selected in [None, ["mcp:connect", "paper:read"], ["mcp:connect", "paper:read", "paper:trade"]]:
        handle = repo.begin_authorization(query, nonce)
        # The browser first loads the consent page, which commits its owner binding.
        # An invalid approval alone correctly rolls back the whole transaction.
        repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
        with pytest.raises(OAuthError):
            repo.approve_authorization(handle, nonce, TENANT_A, USER_A, ["mcp:connect", "paper:trade"])
        with pytest.raises(OAuthError):
            repo.approve_authorization(handle, nonce, TENANT_B, USER_B, selected)
        callback = repo.approve_authorization(handle, nonce, TENANT_A, USER_A, selected)
        code = parse_qs(urlsplit(callback).query)["code"][0]
        token = repo.exchange_code(client_id=client["client_id"], code=code, code_verifier=secret,
                                   redirect_uri=STABLE_CALLBACK, resource=RESOURCE)
        identity = verifier.verify_access(token["access_token"])
        assert identity is not None and identity.subject_user_id == USER_A
        assert "research:read" not in identity.scopes
        results.append(token)
    assert results[0]["scope"] == "mcp:connect market:read analysis:read"
    assert results[1]["scope"] == "mcp:connect paper:read"
    assert results[2]["scope"] == "mcp:connect paper:read paper:trade"
    for previous in results[:2]:
        with pytest.raises(OAuthError) as denied:
            repo.refresh(client_id=client["client_id"], refresh_token=previous["refresh_token"],
                         resource=RESOURCE, scope="mcp:connect paper:read paper:trade")
        assert denied.value.error == "invalid_scope"
    narrowed = repo.refresh(client_id=client["client_id"], refresh_token=results[2]["refresh_token"],
                            resource=RESOURCE, scope="mcp:connect paper:read")
    assert narrowed["scope"] == "mcp:connect paper:read"
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=narrowed["refresh_token"],
                     resource=RESOURCE, scope="mcp:connect paper:read paper:trade")


def test_old_registered_clients_and_trade_only_requests_stay_rejected(oauth):
    repo, _, _ = oauth
    old_client = repo.register_client({"redirect_uris": [STABLE_CALLBACK], "scope": "mcp:connect market:read analysis:read research:read"})
    _, nonce, _, query = authorization(repo, old_client)
    query["scope"] = "mcp:connect paper:read paper:trade"
    with pytest.raises(OAuthError):
        repo.begin_authorization(query, nonce)
    with pytest.raises(OAuthError):
        repo.register_client({"redirect_uris": [STABLE_CALLBACK], "scope": "mcp:connect paper:trade"})


def test_oauth_bindings_do_not_consume_valid_code_and_code_reuse_revokes(oauth):
    repo, verifier, _ = oauth
    client = registration(repo)
    other_client = registration(repo)
    handle, nonce, proof, query = authorization(repo, client)
    with pytest.raises(OAuthError):
        repo.pending_authorization(handle, secrets.token_urlsafe(32), TENANT_A, USER_A)
    repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
    with pytest.raises(OAuthError):
        repo.approve_authorization(handle, nonce, TENANT_B, USER_B)
    redirect = repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    request = {"client_id": client["client_id"], "code": code, "code_verifier": proof,
               "redirect_uri": STABLE_CALLBACK, "resource": RESOURCE}
    for alteration in ({"code_verifier": secrets.token_urlsafe(48)}, {"client_id": other_client["client_id"]},
                       {"redirect_uri": "https://evil.example/callback"}, {"resource": "https://evil.example/mcp"}):
        with pytest.raises(OAuthError):
            repo.exchange_code(**{**request, **alteration})
    token = repo.exchange_code(**request)
    assert verifier.verify_access(token["access_token"]) is not None
    with pytest.raises(OAuthError):
        repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    with pytest.raises(OAuthError):
        repo.exchange_code(**request)
    assert verifier.verify_access(token["access_token"]) is None


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
def test_oauth_existing_ui_revoke_token_revokes_refresh_family_and_client_auth(oauth, method):
    repo, verifier, _ = oauth
    client, token, _ = issued(repo, method)
    principal = verifier.verify_access(token["access_token"])
    assert principal is not None
    if method != "none":
        with pytest.raises(OAuthError) as error:
            repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=RESOURCE)
        assert error.value.error == "invalid_client"
    PostgresMcpRepository(repo.pool).revoke_token(TENANT_A, USER_A, principal.client_id, principal.id)
    assert verifier.verify_access(token["access_token"]) is None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], client_secret=client.get("client_secret"), auth_method=method,
                     refresh_token=token["refresh_token"], resource=RESOURCE)


def test_oauth_deny_owner_state_revocation_expiry_and_rls(oauth):
    repo, verifier, database_url = oauth
    client = registration(repo)
    handle, nonce, _, query = authorization(repo, client)
    redirect = repo.deny_authorization(handle, nonce, TENANT_A, USER_A)
    parsed = parse_qs(urlsplit(redirect).query)
    assert parsed["error"] == ["access_denied"] and parsed["iss"] == [ORIGIN]
    assert parsed["state"] == [query["state"]] and "code" not in parsed
    with pytest.raises(OAuthError):
        repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
    client, token, _ = issued(repo)
    principal = verifier.verify_access(token["access_token"])
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE memberships SET role='VIEWER' WHERE user_id=%s AND tenant_id=%s", (USER_A,TENANT_A))
    assert verifier.verify_access(token["access_token"]) is None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=RESOURCE)
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE memberships SET role='OWNER' WHERE user_id=%s AND tenant_id=%s", (USER_A,TENANT_A))
    with repo.pool.transaction(TENANT_B, USER_B) as connection:
        assert connection.execute("SELECT count(*) FROM oauth_grants").fetchone()[0] == 0
    with verifier.pool.public_transaction() as connection:
        assert connection.execute("SELECT has_table_privilege(current_user,'gatescope_security.oauth_registered_clients','SELECT')").fetchone()[0] is False
        assert connection.execute("SELECT has_function_privilege(current_user,'gatescope_security.locate_oauth_credential(text,text)','EXECUTE')").fetchone()[0] is False
    repo.revoke(client_id=registration(repo)["client_id"], token=token["refresh_token"])
    assert verifier.verify_access(token["access_token"]) is not None
    repo.revoke(client_id=client["client_id"], token=token["refresh_token"])
    assert verifier.verify_access(token["access_token"]) is None
    repo.revoke(client_id=client["client_id"], token="unknown")
    assert principal is not None


def test_oauth_invalid_registration_authorization_and_expiration(oauth):
    repo, verifier, database_url = oauth
    for callback in ("https://evil.example/", STABLE_CALLBACK + "?evil=1", STABLE_CALLBACK + "/",
                     "https://chatgpt.com.evil.example/connector_platform_oauth_redirect", "http://chatgpt.com/connector/oauth/x"):
        with pytest.raises(OAuthError):
            repo.register_client({"redirect_uris": [callback]})
    client = registration(repo)
    handle, nonce, proof, query = authorization(repo, client)
    for alteration in ({"code_challenge_method": "plain"}, {"resource": RESOURCE + "/"},
                       {"scope": "mcp:connect trade:write"}, {"state": ""}):
        with pytest.raises(OAuthError):
            repo.begin_authorization({**query, **alteration}, nonce)
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE gatescope_security.oauth_pending_requests SET expires_at=now()-interval '1 second'")
    with pytest.raises(OAuthError):
        repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    client, token, request = issued(repo)
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE mcp_tokens SET created_at=now()-interval '20 minutes',expires_at=now()-interval '1 second'")
    assert verifier.verify_access(token["access_token"]) is None
    renewed = repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=RESOURCE)
    assert verifier.verify_access(renewed["access_token"]) is not None
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE oauth_grants SET created_at=now()-interval '31 days',expires_at=now()-interval '1 second'")
    assert verifier.verify_access(renewed["access_token"]) is None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=renewed["refresh_token"], resource=RESOURCE)


def test_oauth_registration_shape_and_scope_and_grant_contract(oauth):
    repo, verifier, _ = oauth
    for invalid in ({"grant_types": [{}]}, {"grant_types": ["authorization_code", "authorization_code"]},
                    {"grant_types": "authorization_code"}, {"scope": []}, {"redirect_uris": [{}]},
                    {"token_endpoint_auth_method": []}, {"response_types": "code"}):
        with pytest.raises(OAuthError):
            repo.register_client({"redirect_uris": [STABLE_CALLBACK], **invalid})
    client = repo.register_client({"redirect_uris": [STABLE_CALLBACK], "grant_types": ["authorization_code"],
                                   "scope": "mcp:connect market:read"})
    assert client["scope"] == "mcp:connect market:read"
    assert client["grant_types"] == ["authorization_code"]
    broad_client = registration(repo)
    _, nonce, proof, query = authorization(repo, broad_client)
    query["client_id"] = client["client_id"]
    with pytest.raises(OAuthError) as escalation:
        repo.begin_authorization(query, nonce)
    assert escalation.value.error == "invalid_scope"
    query.pop("scope")
    handle = repo.begin_authorization(query, nonce)
    redirect = repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    token = repo.exchange_code(client_id=client["client_id"], code=code, code_verifier=proof,
                               redirect_uri=STABLE_CALLBACK, resource=RESOURCE)
    assert "refresh_token" not in token
    assert token["scope"] == client["scope"]
    assert verifier.verify_access(token["access_token"]) is not None
    with pytest.raises(OAuthError) as no_refresh:
        repo.refresh(client_id=client["client_id"], refresh_token="x"*48, resource=RESOURCE)
    assert no_refresh.value.error == "unauthorized_client"


def test_oauth_concurrent_refresh_and_ui_client_revocation(oauth):
    repo, verifier, _ = oauth
    client, token, _ = issued(repo)

    def refresh_once():
        try:
            return repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=RESOURCE)
        except OAuthError as error:
            return error.error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: refresh_once(), range(2)))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert results.count("invalid_grant") == 1
    assert verifier.verify_access(successes[0]["access_token"]) is None
    client, token, _ = issued(repo)
    principal = verifier.verify_access(token["access_token"])
    PostgresMcpRepository(repo.pool).revoke_client(TENANT_A, USER_A, principal.client_id)
    assert verifier.verify_access(token["access_token"]) is None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=RESOURCE)


def test_oauth_same_tenant_owner_binding_and_expired_code_and_cleanup(oauth):
    repo, verifier, database_url = oauth
    with psycopg.connect(database_url) as connection:
        connection.execute("""INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at)
          VALUES ('10000000-0000-0000-0000-000000000088',%s,%s,'OWNER','ACTIVE',now(),now())""", (TENANT_A, USER_B))
    client = registration(repo)
    handle, nonce, proof, _ = authorization(repo, client)
    repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
    with pytest.raises(OAuthError):
        repo.approve_authorization(handle, nonce, TENANT_A, USER_B)
    code = parse_qs(urlsplit(repo.approve_authorization(handle, nonce, TENANT_A, USER_A)).query)["code"][0]
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE oauth_codes SET expires_at=now()-interval '1 second'")
    with pytest.raises(OAuthError):
        repo.exchange_code(client_id=client["client_id"], code=code, code_verifier=proof, redirect_uri=STABLE_CALLBACK, resource=RESOURCE)
    unused = registration(repo)
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE gatescope_security.oauth_registered_clients SET created_at=now()-interval '31 days'")
    registration(repo)  # Bounded housekeeping must retain any previously authorized client.
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT 1 FROM gatescope_security.oauth_registered_clients WHERE client_id=%s", (unused["client_id"],)).fetchone() is None
        assert connection.execute("SELECT 1 FROM gatescope_security.oauth_registered_clients WHERE client_id=%s", (client["client_id"],)).fetchone() == (1,)


def test_configured_tunnel_alias_keeps_canonical_storage_pkce_and_refresh_bindings(oauth):
    original, verifier, database_url = oauth
    repo = PostgresOAuthRepository(original.pool, public_origin=ORIGIN, resource_aliases=(TUNNEL_ALIAS,))
    client = registration(repo)
    other_client = registration(repo)
    _, nonce, proof, query = authorization(repo, client)
    query["resource"] = TUNNEL_ALIAS
    invalid_resources = [TUNNEL_ALIAS + "/", TUNNEL_ALIAS + "?other=1", TUNNEL_ALIAS + "#fragment",
                         TUNNEL_ALIAS.replace("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210")]
    for resource in invalid_resources:
        with pytest.raises(OAuthError) as invalid:
            repo.begin_authorization({**query, "resource": resource}, nonce)
        assert invalid.value.error == "invalid_target"
    with pytest.raises(OAuthError):
        original.begin_authorization(query, nonce)
    handle = repo.begin_authorization(query, nonce)
    assert repo.pending_authorization(handle, nonce, TENANT_A, USER_A)["resource"] == RESOURCE
    with pytest.raises(OAuthError):
        repo.approve_authorization(handle, nonce, TENANT_B, USER_B)
    redirect = repo.approve_authorization(handle, nonce, TENANT_A, USER_A)
    callback = parse_qs(urlsplit(redirect).query)
    assert callback["iss"] == [ORIGIN]
    request = {"client_id": client["client_id"], "code": callback["code"][0], "code_verifier": proof,
               "redirect_uri": STABLE_CALLBACK, "resource": TUNNEL_ALIAS}
    for update in [{"resource": resource} for resource in invalid_resources] + [
        {"client_id": other_client["client_id"]}, {"code_verifier": secrets.token_urlsafe(48)},
        {"redirect_uri": "https://untrusted.example/callback"},
    ]:
        with pytest.raises(OAuthError):
            repo.exchange_code(**{**request, **update})
    token = repo.exchange_code(**request)
    principal = verifier.verify_access(token["access_token"])
    assert principal is not None and principal.tenant_id == TENANT_A and principal.subject_user_id == USER_A
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT DISTINCT resource FROM gatescope_security.oauth_pending_requests").fetchall() == [(RESOURCE,)]
        assert connection.execute("SELECT resource,issuer FROM oauth_grants").fetchall() == [(RESOURCE,ORIGIN)]
    for resource in invalid_resources:
        with pytest.raises(OAuthError) as invalid:
            repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=resource)
        assert invalid.value.error == "invalid_target"
    with pytest.raises(OAuthError):
        repo.refresh(client_id=other_client["client_id"], refresh_token=token["refresh_token"], resource=TUNNEL_ALIAS)
    refreshed = repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=TUNNEL_ALIAS)
    assert verifier.verify_access(refreshed["access_token"]) is not None
    # Removing the configured alias fails closed without changing existing canonical grants/tokens.
    with pytest.raises(OAuthError) as removed:
        original.refresh(client_id=client["client_id"], refresh_token=refreshed["refresh_token"], resource=TUNNEL_ALIAS)
    assert removed.value.error == "invalid_target"
    canonical = original.refresh(client_id=client["client_id"], refresh_token=refreshed["refresh_token"], resource=RESOURCE)
    assert verifier.verify_access(canonical["access_token"]) is not None
    with pytest.raises(OAuthError):
        repo.refresh(client_id=client["client_id"], refresh_token=token["refresh_token"], resource=TUNNEL_ALIAS)
    assert verifier.verify_access(canonical["access_token"]) is None


def test_canonical_authorization_can_exchange_via_exact_same_resource_alias(oauth):
    original, verifier, _ = oauth
    repo = PostgresOAuthRepository(original.pool, public_origin=ORIGIN, resource_aliases=(TUNNEL_ALIAS,))
    client = registration(repo)
    handle, nonce, proof, _ = authorization(repo, client)
    code = parse_qs(urlsplit(repo.approve_authorization(handle, nonce, TENANT_A, USER_A)).query)["code"][0]
    request = {"client_id": client["client_id"], "code": code, "code_verifier": proof,
               "redirect_uri": STABLE_CALLBACK, "resource": TUNNEL_ALIAS}
    token = repo.exchange_code(**request)
    assert verifier.verify_access(token["access_token"]) is not None
    with pytest.raises(OAuthError):
        original.exchange_code(**{**request, "resource": RESOURCE})
    assert verifier.verify_access(token["access_token"]) is None


@pytest.mark.parametrize("origin", ["http://localhost:8780", "http://127.0.0.1:8777", "http://[::1]:8777", ORIGIN])
def test_oauth_origin_supports_only_https_or_explicit_loopback(origin):
    assert PostgresOAuthRepository(None, public_origin=origin).issuer == origin


@pytest.mark.parametrize("origin", ["http://example.com", "https://u:p@example.com", "https://@example.com", "https://example.com/path",
                                    "https://example.com:invalid", "https://example.com:0", "http://127.0.0.1.evil.example"])
def test_oauth_origin_rejects_unsafe_values(origin):
    with pytest.raises(ValueError):
        PostgresOAuthRepository(None, public_origin=origin)
