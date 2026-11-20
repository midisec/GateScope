"""Owner MCP UI acceptance with a new, exactly scoped, ephemeral client.

Run only on the authorized deployment host. Creates one uniquely named client
through the UI, confirms the password through encrypted submission, issues one
one-hour read-only token and revokes this run's client. A repository cleanup
finally block also handles a lost creation response. Existing clients, settings
and trading state are never modified. No secret, request body or raw error is
written to evidence, screenshots or output; no OpenAI call is made.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2 as httpx
from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from gatescope.security_secrets import totp_code
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_market_export import run as smoke_run
from verify_deployed_mcp import resolve_owner

CLIENTS = "/api/v2/mcp/clients"
SCOPES = {"mcp:connect", "market:read", "analysis:read"}


def safe_error(exc: Exception, step: str) -> dict:
    return {"step": step, "type": type(exc).__name__, "lines": [
        frame.lineno for frame in traceback.extract_tb(exc.__traceback__)
        if Path(frame.filename).name == Path(__file__).name
    ]}


def next_totp(page, secret: str, after_step: int | None = None) -> tuple[str, int]:
    """Wait for actual clock progression, never use a future or replayed code."""
    floor = int(time.time()) // 30 if after_step is None else after_step
    deadline = time.monotonic() + 36
    while time.monotonic() < deadline:
        code, step = totp_code(secret)
        if step > floor:
            return code, step
        page.wait_for_timeout(250)
    raise RuntimeError("FRESH_MFA_STEP_UNAVAILABLE")


def response_is(response, path: str) -> bool:
    return response.request.method == "POST" and urlsplit(response.url).path == path


def screenshot_safe(page, evidence: Path, name: str) -> None:
    if page.locator('input[type="password"], .mfa-code-inline, .one-time-secret, dialog[open]').count():
        raise RuntimeError("SECRET_OR_DIALOG_SCREEN_NOT_CAPTURED")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    page.screenshot(
        path=str(evidence / f"{name}.png"), full_page=True,
        mask=[page.locator(".profile-menu"), page.locator(".compact-mfa input"),
              page.locator(".token-row code")],
    )


def logout(page) -> int:
    return page.evaluate("""async () => {
      const csrf = document.cookie.split(';').map(s => s.trim())
        .find(s => s.startsWith('gatescope_csrf='))?.slice('gatescope_csrf='.length);
      const response = await fetch('/auth/logout', {method:'POST',credentials:'same-origin',
        headers:csrf ? {'X-CSRF-Token':csrf} : {}});
      return response.status;
    }""")


def run_browser(pool, repository, tenant, user, origin, mcp_url, owner, enrollment, evidence, report):
    client_name = f"owner-ui-smoke-{uuid4().hex}"
    token_label = "one-hour owner UI acceptance"
    client_id = None
    bearer = None
    step = "configuration"
    assert not any(item.name == client_name for item in repository.list_clients(tenant, user))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      locale="zh-CN", service_workers="block")

        def fence(route):
            request = route.request
            parsed = urlsplit(request.url)
            allowed = parsed.scheme + "://" + parsed.netloc == origin
            if allowed and request.method not in {"GET", "HEAD", "OPTIONS"}:
                allowed = False
                if request.method == "POST":
                    try:
                        payload = request.post_data_json if request.post_data else {}
                        if parsed.path in {"/auth/login", "/auth/reauthenticate"}:
                            allowed = (isinstance(payload, dict) and set(payload) == {"encrypted_submission"}
                                       and isinstance(payload["encrypted_submission"], dict))
                        elif parsed.path == "/auth/mfa/verify":
                            allowed = isinstance(payload, dict) and set(payload) == {"code"}
                        elif parsed.path == "/auth/logout":
                            allowed = True
                        elif parsed.path == "/api/v2/indicators/preview":
                            allowed = True  # Existing chart's read-only preview during login navigation.
                        elif parsed.path == CLIENTS:
                            allowed = payload == {"name": client_name} and client_id is None
                        elif client_id and parsed.path == f"{CLIENTS}/{client_id}/tokens":
                            allowed = (isinstance(payload, dict) and payload.get("label") == token_label
                                       and payload.get("ttl_hours") == 1 and set(payload.get("scopes", [])) == SCOPES
                                       and set(payload) == {"label", "ttl_hours", "scopes", "mfa_code"}
                                       and bearer is None)
                        elif client_id and parsed.path == f"{CLIENTS}/{client_id}:revoke":
                            allowed = isinstance(payload, dict) and set(payload) == {"code"}
                    except Exception:
                        allowed = False
            if not allowed:
                # Never record query, body, headers or an external request's full URL.
                report["blocked"].append({"method": request.method, "same_origin": parsed.netloc == urlsplit(origin).netloc})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", fence)
        page = context.new_page()
        page.set_default_timeout(30000)
        expect.set_options(timeout=30000)
        page.on("pageerror", lambda _: report["errors"].append({"type": "JAVASCRIPT_ERROR"}))
        try:
            step = "encrypted_owner_login"
            page.goto(origin + "/login", wait_until="domcontentloaded")
            page.locator('select[name="tenant"]').select_option("default")
            page.locator('input[name="username"]').fill(owner["username"])
            page.locator('input[name="password"]').fill(owner["password"])
            with page.expect_response(lambda r: response_is(r, "/auth/login")) as login_response:
                page.get_by_role("button", name="安全登录", exact=True).click()
            assert login_response.value.status == 200
            expect(page.locator(".mfa-code-inline")).to_be_visible()
            code, mfa_step = next_totp(page, enrollment["secret"])
            page.locator(".mfa-code-inline").fill(code)
            with page.expect_response(lambda r: response_is(r, "/auth/mfa/verify")) as mfa_response:
                page.get_by_role("button", name="验证并进入系统", exact=True).click()
            assert mfa_response.value.status == 200
            expect(page.locator(".topbar")).to_be_visible()
            page.goto(origin + "/integrations/mcp", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="MCP 接入管理", exact=True)).to_be_visible()
            me = page.evaluate("""async () => { const r=await fetch('/auth/me', {credentials:'same-origin'});
              return {status:r.status,data:await r.json()}; }""")
            assert me["status"] == 200 and me["data"]["user"]["role"] == "OWNER"
            assert me["data"]["user"]["id"] == str(user)
            expect(page.locator(".mcp-preview-debug")).not_to_have_attribute("open", "")
            expect(page.get_by_role("button", name="创建隔离客户端", exact=True)).to_be_enabled()
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 960})
                screenshot_safe(page, evidence, f"owner-management-{width}")
                report["checks"].append({"kind": "management", "width": width, "overflow": False})

            step = "owner_creates_unbound_client"
            page.set_viewport_size({"width": 1440, "height": 960})
            page.get_by_label("客户端名称", exact=True).fill(client_name)
            with page.expect_response(lambda r: response_is(r, CLIENTS)) as created_response:
                page.get_by_role("button", name="创建隔离客户端", exact=True).click()
            assert created_response.value.status == 201
            created = created_response.value.json()
            client_id = UUID(created["id"])
            assert created["name"] == client_name and "tokens" not in created
            assert not {"symbol", "instrument_id", "timeframe"} & created.keys()
            card = page.locator(".mcp-client").filter(has=page.get_by_text(client_name, exact=True))
            expect(card).to_be_visible()
            expect(card).to_contain_text("尚未签发令牌")
            report["checks"].append({"kind": "create_client", "status": 201,
                                     "missing_tokens_response_rendered": True, "bound_market_parameters": False})

            step = "encrypted_password_reauthentication"
            page.get_by_role("button", name="确认登录身份", exact=True).click()
            dialog = page.get_by_role("dialog", name="确认登录身份", exact=True)
            dialog.get_by_label("当前密码", exact=True).fill(owner["password"])
            with page.expect_response(lambda r: response_is(r, "/auth/reauthenticate")) as reauth_response:
                dialog.get_by_role("button", name="验证密码", exact=True).click()
            assert reauth_response.value.status == 200
            assert reauth_response.value.json()["reauthenticated"] is True
            submitted = reauth_response.value.request.post_data_json
            assert set(submitted) == {"encrypted_submission"}
            assert submitted["encrypted_submission"]["purpose"] == f"user:{user}:reauthenticate"
            expect(dialog).not_to_be_visible()
            report["checks"].append({"kind": "reauthenticate", "status": 200, "encrypted": True})

            step = "issue_one_hour_minimal_token"
            page.get_by_label("客户端", exact=True).select_option(str(client_id))
            page.get_by_label("令牌标签", exact=True).fill(token_label)
            page.get_by_label("有效时长", exact=True).select_option("1")
            for checkbox in page.locator('fieldset input[type="checkbox"]').all():
                name = checkbox.get_attribute("name")
                if name == "mcp:connect":
                    expect(checkbox).to_be_checked()
                else:
                    checkbox.set_checked(name in SCOPES)
            code, mfa_step = next_totp(page, enrollment["secret"], mfa_step)
            page.get_by_label("敏感操作 MFA", exact=True).fill(code)
            with page.expect_response(lambda r: response_is(r, f"{CLIENTS}/{client_id}/tokens")) as issued_response:
                page.get_by_role("button", name="用 MFA 签发", exact=True).click()
            assert issued_response.value.status == 201
            issued = issued_response.value.json()
            bearer = issued["token"]
            assert isinstance(bearer, str) and bearer and issued["shown_once"] is True
            assert set(issued["scopes"]) == SCOPES
            expires = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
            assert 3500 <= (expires - datetime.now(timezone.utc)).total_seconds() <= 3601
            secret_dialog = page.get_by_role("dialog", name="保存新令牌", exact=True)
            expect(secret_dialog).to_be_visible()
            assert secret_dialog.locator(".one-time-secret").inner_text() == bearer
            secret_dialog.get_by_label("我已安全保存", exact=True).check()
            secret_dialog.get_by_role("button", name="清除显示并关闭", exact=True).click()
            expect(page.locator(".one-time-secret")).to_have_count(0)
            report["checks"].append({"kind": "issue_token", "status": 201, "ttl_hours": 1,
                                     "scope_count": 3, "one_time_secret_cleared": True})
            for width in (390, 1440):
                page.set_viewport_size({"width": width, "height": 960})
                screenshot_safe(page, evidence, f"owner-issued-secret-hidden-{width}")

            step = "same_token_per_call_parameters"
            for symbol, timeframe in (("BTC_USDT", "1m"), ("龙虾_USDT", "15m"), ("BTC_USDT", "15m")):
                result = smoke_run(mcp_url, bearer, symbol, "futures:usdt", timeframe, 100)
                assert result["ok"] and result["anonymous_status"] == 401
                assert 0 < result["closed_bars"] <= 100 and result["indicator_count"] == 8
                report["cases"].append({"symbol": symbol, "timeframe": timeframe, **result})

            step = "ui_revokes_own_client"
            code, mfa_step = next_totp(page, enrollment["secret"], mfa_step)
            page.get_by_label("敏感操作 MFA", exact=True).fill(code)
            card.get_by_role("button", name="吊销客户端", exact=True).click()
            confirm = page.get_by_role("dialog", name="吊销客户端", exact=True)
            expect(confirm).to_contain_text(client_name)
            with page.expect_response(lambda r: response_is(r, f"{CLIENTS}/{client_id}:revoke")) as revoked_response:
                confirm.get_by_role("button", name="确认操作", exact=True).click()
            assert revoked_response.value.status == 200
            expect(card.locator("header .status")).to_have_text("已吊销")
            report["ui_revocation_status"] = 200
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 960})
                screenshot_safe(page, evidence, f"owner-revoked-{width}")
        except Exception as exc:
            report["errors"].append(safe_error(exc, step))
        finally:
            # A successful INSERT may have preceded a lost HTTP response: locate
            # only the exact unpredictable name created for this run, never a prefix.
            try:
                matches = [item for item in repository.list_clients(tenant, user) if item.name == client_name]
                if len(matches) > 1:
                    raise RuntimeError("AMBIGUOUS_TEMPORARY_CLIENT")
                if matches:
                    target = matches[0]
                    if client_id is not None and target.id != client_id:
                        raise RuntimeError("TEMPORARY_CLIENT_ID_MISMATCH")
                    if target.status == "ACTIVE":
                        repository.revoke_client(tenant, user, target.id)
                        report["fallback_cleanup_used"] = True
                    target = repository.get_client(tenant, user, target.id)
                    tokens = repository.list_tokens(tenant, user, target.id)
                    report["temporary_client_revoked"] = target.status == "REVOKED" and all(
                        token.revoked_at is not None for token in tokens)
                if bearer and report["temporary_client_revoked"]:
                    with httpx.Client(timeout=15, follow_redirects=False) as client:
                        response = client.post(mcp_url, headers={"Authorization": f"Bearer {bearer}",
                            "Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
                        report["revoked_http_status"] = response.status_code
            except Exception as exc:
                report["errors"].append(safe_error(exc, "exact_client_cleanup"))
            try:
                report["logout_status"] = logout(page)
            except Exception as exc:
                report["errors"].append(safe_error(exc, "logout"))
            context.close()
            browser.close()


def main() -> int:
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-owner-mcp-browser-"))
    report = {"checks": [], "cases": [], "errors": [], "blocked": [],
              "temporary_client_revoked": False, "revoked_http_status": None,
              "ui_revocation_status": None, "fallback_cleanup_used": False,
              "existing_clients_modified": False, "openai_called": False}
    pool = None
    try:
        runtime = read_env(SECRET_DIR / "formal-runtime.env")
        origin = runtime["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        mcp_url = read_env(SECRET_DIR / "formal-mcp.env")["GATESCOPE_MCP_PUBLIC_URL"]
        assert origin == "https://43.108.44.74" and mcp_url == origin + "/mcp"
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = runtime[key]
        enrollment = recover_active_enrollment(owner["username"])
        assert enrollment, "EXISTING_MFA_REQUIRED_NO_ENROLLMENT_ALLOWED"
        pool = PostgresPool(read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"], min_size=1, max_size=1)
        pool.open()
        tenant, user = resolve_owner(pool, owner["username"])
        run_browser(pool, PostgresMcpRepository(pool), tenant, user, origin, mcp_url,
                    owner, enrollment, evidence, report)
    except Exception as exc:
        report["errors"].append(safe_error(exc, "setup"))
    finally:
        if pool is not None:
            pool.close()
    report["ok"] = (not report["errors"] and not report["blocked"] and len(report["cases"]) == 3
                    and all(case["ok"] for case in report["cases"]) and report["ui_revocation_status"] == 200
                    and report["temporary_client_revoked"] and report["revoked_http_status"] == 401
                    and report.get("logout_status") == 200)
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({**report, "private_evidence": str(evidence)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
