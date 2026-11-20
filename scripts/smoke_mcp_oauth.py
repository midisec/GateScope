"""Authorized production OAuth browser/protocol acceptance, never an OpenAI call.

Creates one new DCR client and two owner-bound grants. Both grants are revoked;
an exact registered-client/tenant/user cleanup also handles lost responses.
Existing clients, settings, credentials and trading state are never modified.
Passwords, MFA, OAuth URLs, codes, tokens and raw errors stay in memory only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx2 as httpx
from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_market_export import run as smoke_run
from smoke_mcp_research import run as research_smoke_run
from smoke_mcp_legacy_snapshot import run as legacy_snapshot_smoke_run
from smoke_mcp_owner_browser import logout, next_totp, response_is
from verify_deployed_mcp import resolve_owner

CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
CONSENT = "/api/v2/mcp/oauth/consent"
CONSENT_PAGE = "/integrations/mcp/authorize"
SCOPES = {"mcp:connect", "market:read", "analysis:read", "research:read"}


def safe_error(exc: Exception, step: str) -> dict:
    return {"step": step, "type": type(exc).__name__, "lines": [frame.lineno
        for frame in traceback.extract_tb(exc.__traceback__)
        if Path(frame.filename).name == Path(__file__).name],
        "research_frames": [{"file": Path(frame.filename).name, "line": frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)
            if Path(frame.filename).name in {"smoke_mcp_research.py", "smoke_mcp_market_export.py", "smoke_mcp_legacy_snapshot.py"}]}


def bearer_request(client, url, token, method="tools/list", params=None):
    return client.post(url, headers={"Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 91, "method": method, "params": params or {}})


def token_request(client, origin, fields, expected=200):
    response = client.post(origin + "/oauth/token", data=fields)
    assert response.status_code == expected
    assert response.headers.get("cache-control") == "no-store"
    return response.json()


def verify_discovery(client, origin, report):
    resource = origin + "/mcp"
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        response = client.get(origin + path)
        assert response.status_code == 200
        metadata = response.json()
        assert metadata["resource"] == resource and metadata["authorization_servers"] == [origin]
        assert set(metadata["scopes_supported"]) == SCOPES | {"paper:read", "paper:trade"}
    response = client.get(origin + "/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == origin and metadata["authorization_response_iss_parameter_supported"] is True
    for field, path in (("authorization_endpoint", "/oauth/authorize"), ("token_endpoint", "/oauth/token"),
                        ("registration_endpoint", "/oauth/register"), ("revocation_endpoint", "/oauth/revoke")):
        assert metadata[field] == origin + path
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    anonymous = client.post(resource, headers={"Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert anonymous.status_code == 401
    challenge = anonymous.headers.get("www-authenticate", "")
    assert 'resource_metadata="' + origin + "/.well-known/oauth-protected-resource/mcp" in challenge
    report["discovery"] = {"resource_metadata_count": 2, "authorization_metadata_status": 200,
                           "anonymous_status": 401, "resource_challenge": True, "pkce_s256": True}


def browser_checks(pool, tenant, user, origin, owner, enrollment, evidence, report):
    registered_id = None
    repository = PostgresMcpRepository(pool)
    step = "discovery"
    with httpx.Client(timeout=40, follow_redirects=False) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN", service_workers="block")
        active_request = None
        callbacks = []

        def fence(route):
            request = route.request
            parsed = urlsplit(request.url)
            destination = parsed.scheme + "://" + parsed.netloc
            if destination + parsed.path == CALLBACK:
                # Chromium can retry a deliberately aborted navigation. Count
                # unique complete destinations, never tolerate an extra code or
                # state, and retain the retry count without logging the URL.
                if request.url not in callbacks:
                    callbacks.append(request.url)  # Memory only; no external request.
                else:
                    report["intercepted_identical_callback_retries"] = report.get(
                        "intercepted_identical_callback_retries", 0) + 1
                route.abort()
                return
            allowed = destination == origin
            if allowed and request.method in {"GET", "HEAD"}:
                allowed = parsed.path in {"/oauth/authorize", CONSENT_PAGE, CONSENT, "/auth/me",
                    "/auth/workspaces", "/auth/mfa-methods", "/api/v2/security/submission-key", "/favicon.ico"} or parsed.path.startswith("/assets/")
            elif allowed and request.method == "POST":
                try:
                    payload = request.post_data_json if request.post_data else {}
                    if parsed.path in {"/auth/login", "/auth/reauthenticate"}:
                        allowed = isinstance(payload, dict) and set(payload) == {"encrypted_submission"}
                    elif parsed.path == "/auth/mfa/verify":
                        allowed = isinstance(payload, dict) and set(payload) == {"code"}
                    elif parsed.path == CONSENT:
                        allowed = active_request is not None and (
                            payload == {"request_id": active_request, "decision": "approve"}
                            or (isinstance(payload, dict) and set(payload) == {"request_id", "decision", "approved_scopes"}
                                and payload["request_id"] == active_request and payload["decision"] == "approve"
                                and isinstance(payload["approved_scopes"], list)
                                and set(payload["approved_scopes"]) == SCOPES))
                    else:
                        allowed = parsed.path == "/auth/logout"
                except Exception:
                    allowed = False
            else:
                allowed = False
            if allowed:
                route.continue_()
            else:
                report["blocked"].append({"method": request.method, "same_origin": destination == origin})
                route.abort()

        context.route("**/*", fence)
        page = context.new_page()
        page.set_default_timeout(30000)
        expect.set_options(timeout=30000)
        page.on("pageerror", lambda _: report["errors"].append({"type": "JAVASCRIPT_ERROR"}))

        def authorize(first):
            nonlocal active_request
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            query = {"response_type": "code", "client_id": registered_id, "redirect_uri": CALLBACK,
                "code_challenge": challenge, "code_challenge_method": "S256", "resource": origin + "/mcp",
                "scope": " ".join(sorted(SCOPES)), "state": state}
            page.goto(origin + "/oauth/authorize?" + urlencode(query), wait_until="domcontentloaded")
            assert urlsplit(page.url).path == CONSENT_PAGE
            active_request = parse_qs(urlsplit(page.url).query)["request"][0]
            if first:
                expect(page.get_by_role("heading", name="登录并连接 ChatGPT", exact=True)).to_be_visible()
                page.locator('select[name="tenant"]').select_option("default")
                page.locator('input[name="username"]').fill(owner["username"])
                page.locator('input[name="password"]').fill(owner["password"])
                with page.expect_response(lambda r: response_is(r, "/auth/login")) as response:
                    page.get_by_role("button", name="安全登录", exact=True).click()
                assert response.value.status == 200
                submitted = response.value.request.post_data_json
                assert set(submitted) == {"encrypted_submission"} and submitted["encrypted_submission"]["purpose"] == "auth_login"
                expect(page.locator(".mfa-code-inline")).to_be_visible()
                code, _ = next_totp(page, enrollment["secret"])
                page.locator(".mfa-code-inline").fill(code)
                with page.expect_response(lambda r: response_is(r, "/auth/mfa/verify")) as response:
                    page.get_by_role("button", name="验证并进入系统", exact=True).click()
                assert response.value.status == 200
                report["login"] = {"encrypted": True, "mfa_status": 200, "path_preserved": True}
            expect(page.get_by_role("heading", name="连接 ChatGPT", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="允许只读访问", exact=True)).to_be_enabled()
            research_checkbox = page.locator('input[name="research:read"]')
            expect(research_checkbox).not_to_be_checked()
            research_checkbox.check()
            assert urlsplit(page.url).path == CONSENT_PAGE
            if first:
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 960})
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
                    assert page.locator('input[type="password"], .mfa-code-inline, .one-time-secret').count() == 0
                    page.screenshot(path=str(evidence / f"consent-{width}.png"), full_page=True,
                                    mask=[page.locator(".oauth-account")])
                    report["viewports"].append({"width": width, "overflow": False})
                missing_csrf = page.evaluate("""async (requestId) => {
                  const response = await fetch('/api/v2/mcp/oauth/consent', {method:'POST',credentials:'same-origin',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId,decision:'approve'})});
                  const body = await response.json(); return {status:response.status,code:body.error?.code};
                }""", active_request)
                assert missing_csrf == {"status": 403, "code": "CSRF_REJECTED"}
                report["missing_csrf_status"] = 403
            before = len(callbacks)
            with page.expect_request(lambda r: r.url.startswith(CALLBACK + "?")), page.expect_response(lambda r: response_is(r, CONSENT)) as response:
                page.get_by_role("button", name="允许只读访问", exact=True).click(no_wait_after=True)
            assert response.value.status == 200
            for _ in range(20):
                if len(callbacks) > before:
                    break
                page.wait_for_timeout(100)
            assert len(callbacks) == before + 1
            # An aborted main-frame navigation can evict the consent response
            # body from Chromium's cache. The intercepted browser destination
            # is the URL the UI actually selected; it never reached ChatGPT.
            returned = callbacks[-1]
            parsed = urlsplit(returned)
            assert parsed.scheme + "://" + parsed.netloc + parsed.path == CALLBACK and not parsed.fragment
            params = parse_qs(parsed.query, strict_parsing=True)
            assert params.get("state") == [state] and params.get("iss") == [origin]
            assert len(params.get("code", [])) == 1 and "error" not in params
            return {"grant_type": "authorization_code", "client_id": registered_id,
                    "code": params["code"][0], "code_verifier": verifier,
                    "redirect_uri": CALLBACK, "resource": origin + "/mcp"}

        try:
            verify_discovery(client, origin, report)
            step = "dynamic_client_registration"
            response = client.post(origin + "/oauth/register", json={"client_name": f"oauth-smoke-{uuid4().hex}",
                "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
                "response_types": ["code"], "grant_types": ["authorization_code", "refresh_token"],
                "scope": " ".join(sorted(SCOPES))})
            assert response.status_code == 201
            registered = response.json()
            registered_id = registered["client_id"]
            assert registered["token_endpoint_auth_method"] == "none" and "client_secret" not in registered
            report["registration_status"] = 201
            step = "login_mfa_consent"
            fields = authorize(True)
            step = "pkce_and_resource_binding"
            wrong = token_request(client, origin, {**fields, "code_verifier": secrets.token_urlsafe(48)}, 400)
            assert wrong["error"] == "invalid_grant"
            wrong = token_request(client, origin, {**fields, "resource": origin + "/not-mcp"}, 400)
            assert wrong["error"] == "invalid_target"
            issued = token_request(client, origin, fields)
            assert issued["token_type"].lower() == "bearer" and set(issued["scope"].split()) == SCOPES
            assert 0 < issued["expires_in"] <= 900
            report["binding"] = {"wrong_pkce_status": 400, "wrong_resource_status": 400, "exchange_status": 200}
            step = "same_token_market_and_private_tool_boundary"
            for symbol, timeframe in (("BTC_USDT", "1m"), ("龙虾_USDT", "15m")):
                result = smoke_run(origin + "/mcp", issued["access_token"], symbol, "futures:usdt", timeframe, 100)
                assert result["ok"] and 0 < result["closed_bars"] <= 100 and result["indicator_count"] == 8
                report["cases"].append({"symbol": symbol, "timeframe": timeframe, **result})
            denied = bearer_request(client, origin + "/mcp", issued["access_token"], "tools/call",
                                    {"name": "signal_list", "arguments": {"limit": 1}})
            assert denied.status_code == 200
            payload = denied.json()
            assert "error" in payload or payload.get("result", {}).get("isError") is True
            report["private_tool_denied"] = True
            step = "research_read_tools_and_actual_chart_images"
            report["research"] = research_smoke_run(origin + "/mcp", issued["access_token"], image_directory=evidence)
            step = "existing_tool_sol_images_and_structures"
            report["legacy_snapshot"] = legacy_snapshot_smoke_run(origin + "/mcp", issued["access_token"], image_directory=evidence)
            step = "refresh_rotation_and_replay_revocation"
            refresh = {"grant_type": "refresh_token", "client_id": registered_id,
                       "refresh_token": issued["refresh_token"], "resource": origin + "/mcp"}
            rotated = token_request(client, origin, refresh)
            assert rotated["refresh_token"] != issued["refresh_token"] and rotated["access_token"] != issued["access_token"]
            assert bearer_request(client, origin + "/mcp", rotated["access_token"]).status_code == 200
            assert token_request(client, origin, refresh, 400)["error"] == "invalid_grant"
            assert bearer_request(client, origin + "/mcp", rotated["access_token"]).status_code == 401
            assert bearer_request(client, origin + "/mcp", issued["access_token"]).status_code == 401
            report["refresh"] = {"rotated": True, "replay_status": 400, "revoked_access_status": 401}
            step = "explicit_revocation_endpoint"
            second = token_request(client, origin, authorize(False))
            response = client.post(origin + "/oauth/revoke", data={"client_id": registered_id,
                "token": second["refresh_token"], "token_type_hint": "refresh_token"})
            assert response.status_code == 200
            assert bearer_request(client, origin + "/mcp", second["access_token"]).status_code == 401
            assert token_request(client, origin, {"grant_type": "refresh_token", "client_id": registered_id,
                "refresh_token": second["refresh_token"], "resource": origin + "/mcp"}, 400)["error"] == "invalid_grant"
            report["explicit_revocation"] = {"status": 200, "revoked_access_status": 401, "refresh_denied": True}
            report["intercepted_callbacks"] = len(callbacks)
        except Exception as exc:
            report["errors"].append(safe_error(exc, step))
        finally:
            try:
                if registered_id is not None:
                    with pool.transaction(tenant, user) as connection:
                        matches = connection.execute("""SELECT mc.id,mc.status FROM oauth_grants g
                          JOIN mcp_clients mc ON mc.id=g.mcp_client_id AND mc.tenant_id=g.tenant_id
                          WHERE g.oauth_client_id=%s AND g.tenant_id=%s AND g.user_id=%s
                            AND mc.owner_user_id=%s""", (registered_id, tenant, user, user)).fetchall()
                    assert len(matches) <= 2
                    for client_id, status in matches:
                        if status == "ACTIVE":
                            repository.revoke_client(tenant, user, client_id)
                        assert repository.get_client(tenant, user, client_id).status == "REVOKED"
                        assert all(token.revoked_at is not None for token in repository.list_tokens(tenant, user, client_id))
                    report["temporary_clients_revoked"] = len(matches)
            except Exception as exc:
                report["errors"].append(safe_error(exc, "exact_grant_cleanup"))
            try:
                if active_request:
                    # Aborted cross-origin navigation can leave a browser error page.
                    # Restore only this consumed local consent URL before logout.
                    page.goto(origin + CONSENT_PAGE + "?request=" + active_request, wait_until="domcontentloaded")
                    report["logout_status"] = logout(page)
            except Exception as exc:
                report["errors"].append(safe_error(exc, "logout"))
            context.close()
            browser.close()


def main() -> int:
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-oauth-smoke-"))
    report = {"errors": [], "blocked": [], "cases": [], "viewports": [],
              "existing_clients_modified": False, "openai_called": False}
    pool = None
    try:
        runtime = read_env(SECRET_DIR / "formal-runtime.env")
        origin = runtime["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        assert origin == "https://43.108.44.74"
        assert read_env(SECRET_DIR / "formal-mcp.env")["GATESCOPE_MCP_PUBLIC_URL"] == origin + "/mcp"
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = runtime[key]
        enrollment = recover_active_enrollment(owner["username"])
        assert enrollment
        pool = PostgresPool(read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"], min_size=1, max_size=1)
        pool.open()
        tenant, user = resolve_owner(pool, owner["username"])
        browser_checks(pool, tenant, user, origin, owner, enrollment, evidence, report)
    except Exception as exc:
        report["errors"].append(safe_error(exc, "setup"))
    finally:
        if pool is not None:
            pool.close()
    report["ok"] = (not report["errors"] and not report["blocked"] and len(report["cases"]) == 2
        and all(case["ok"] for case in report["cases"]) and report.get("temporary_clients_revoked") == 2
        and report.get("intercepted_callbacks") == 2 and report.get("logout_status") == 200
        and report.get("private_tool_denied") is True and report.get("missing_csrf_status") == 403
        and "refresh" in report and "explicit_revocation" in report
        and report.get("research", {}).get("ok") is True
        and report.get("legacy_snapshot", {}).get("ok") is True)
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({**report, "private_evidence": str(evidence)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
