"""Read-only deployed PAPER UI acceptance for one pre-created smoke account.

Run only after deployment with --account-id pointing to the exact owner account
created by the separate MCP acceptance script; its name must start with
``MCP模拟验收-``. This script never creates an account, submits/cancels/closes an
order, changes configuration, issues a token, or calls OpenAI. The request fence
permits same-origin reads, encrypted login, existing MFA verification, logout,
and the existing chart's read-only indicator preview only.

Credentials and raw HTTP/JavaScript errors remain in memory. Evidence contains
only safe counts/statuses and screenshots of the dedicated account with profile
and unrelated legacy account/trade sections masked. No video/trace/HAR is made.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_owner_browser import logout, next_totp, response_is
from verify_deployed_mcp import resolve_owner

ACCOUNT_PREFIX = "MCP模拟验收-"


def safe_error(exc: Exception, step: str) -> dict:
    return {"step": step, "type": type(exc).__name__, "lines": [
        item.lineno for item in traceback.extract_tb(exc.__traceback__)
        if Path(item.filename).name == Path(__file__).name
    ]}


def allowed_request(origin: str, url: str, method: str, payload: object = None) -> bool:
    """Pure fail-closed route policy, separately unit tested without a browser."""
    parsed = urlsplit(url)
    if f"{parsed.scheme}://{parsed.netloc}" != origin or parsed.username or parsed.password:
        return False
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method != "POST":
        return False
    if parsed.path in {"/auth/login", "/auth/reauthenticate"}:
        return isinstance(payload, dict) and set(payload) == {"encrypted_submission"} and isinstance(payload["encrypted_submission"], dict)
    if parsed.path == "/auth/mfa/verify":
        return isinstance(payload, dict) and set(payload) == {"code"}
    return parsed.path == "/auth/logout" or (parsed.path == "/api/v2/indicators/preview" and isinstance(payload, dict))


def snapshot_safe(page, evidence: Path, name: str, *, dialog: str | None = None) -> None:
    if page.locator('input[type="password"], .mfa-code-inline, .one-time-secret').count():
        raise RuntimeError("SECRET_SCREEN_NOT_CAPTURED")
    if dialog is None:
        assert page.locator("dialog[open]").count() == 0
    else:
        expect(page.get_by_role("dialog", name=dialog, exact=True)).to_be_visible()
        assert page.locator("dialog[open]").count() == 1
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=True,
                    mask=[page.locator(".profile-menu"), page.locator(".trade-dashboard"),
                          page.locator(".trade-ledger")])


def read_json(page, path: str) -> dict:
    """Raw private response stays in memory and is never attached to a report."""
    result = page.evaluate("""async path => {
      const response = await fetch(path, {credentials:'same-origin'});
      return {status:response.status, data:await response.json()};
    }""", path)
    assert result["status"] == 200
    return result["data"]


def run_browser(origin, owner, enrollment, user, account_id, evidence, report):
    step = "launch"
    account_path = f"/api/v2/paper/accounts/{account_id}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN", service_workers="block")

        def fence(route):
            request = route.request
            try:
                payload = request.post_data_json if request.method == "POST" and request.post_data else None
            except Exception:
                payload = None
            allowed = allowed_request(origin, request.url, request.method, payload)
            if allowed:
                if request.method == "POST":
                    path = urlsplit(request.url).path
                    report["allowed_posts"][path] = report["allowed_posts"].get(path, 0) + 1
                route.continue_()
            else:
                parsed = urlsplit(request.url)
                report["blocked"].append({"method": request.method, "same_origin": f"{parsed.scheme}://{parsed.netloc}" == origin})
                route.abort()

        context.route("**/*", fence)
        context.route_web_socket("**/*", lambda socket: (report["blocked"].append({"method": "WEBSOCKET"}), socket.close()))
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
            with page.expect_response(lambda response: response_is(response, "/auth/login")) as result:
                page.get_by_role("button", name="安全登录", exact=True).click()
            assert result.value.status == 200
            expect(page.locator(".mfa-code-inline")).to_be_visible()
            code, _ = next_totp(page, enrollment["secret"])
            page.locator(".mfa-code-inline").fill(code)
            with page.expect_response(lambda response: response_is(response, "/auth/mfa/verify")) as result:
                page.get_by_role("button", name="验证并进入系统", exact=True).click()
            assert result.value.status == 200
            expect(page.locator(".topbar")).to_be_visible()
            me = read_json(page, "/auth/me")
            assert me["user"]["id"] == str(user) and me["user"]["role"] == "OWNER"
            report["login"] = {"encrypted": True, "existing_mfa_verified": True, "owner_bound": True}

            step = "select_exact_smoke_account"
            page.goto(origin + "/trading/paper", wait_until="domcontentloaded")
            workspace = page.get_by_role("region", name="模拟委托与持仓")
            expect(workspace).to_be_visible()
            selector = workspace.get_by_label("模拟交易账户", exact=True)
            expect(selector.locator(f'option[value="{account_id}"]')).to_have_count(1)
            selected_name = selector.locator(f'option[value="{account_id}"]').inner_text()
            assert selected_name.startswith(ACCOUNT_PREFIX)
            if selector.input_value() != str(account_id):
                with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path == account_path + "/snapshot") as result:
                    selector.select_option(str(account_id))
                assert result.value.status == 200
            expect(selector).to_have_value(str(account_id))
            expect(workspace.locator(".paper-balance-grid")).to_be_visible()
            snapshot = read_json(page, account_path + "/snapshot")
            assert snapshot["account"]["id"] == str(account_id) and snapshot["environment"] == "PAPER"
            report["snapshot"] = {"environment": "PAPER", "account_matches": True,
                                  "net_cash_change_field": "realized_pnl" in snapshot["balance"],
                                  "unknown_marks_not_zeroed": snapshot["balance"].get("unrealized_pnl") is not None
                                  or snapshot["balance"].get("equity") is None}

            step = "verify_real_records"
            listings = {kind: read_json(page, account_path + f"/{kind}?limit=10&offset=0" + ("&state=ALL" if kind == "positions" else ""))
                        for kind in ("orders", "positions", "fills")}
            for kind, listing in listings.items():
                assert listing["items"], "SMOKE_RECORDS_REQUIRED"
                assert all(str(item["account_id"]) == str(account_id) for item in listing["items"])
                report["record_counts"][kind] = len(listing["items"])
            order_id = UUID(listings["orders"]["items"][0]["id"])
            order_detail = read_json(page, f"/api/v2/paper/orders/{order_id}")
            assert order_detail["account_id"] == str(account_id)
            report["same_account_records"] = True

            for width in (1440, 390):
                step = f"paper_tabs_{width}"
                page.set_viewport_size({"width": width, "height": 960})
                for kind, label in (("orders", "委托记录"), ("positions", "持仓与平仓"), ("fills", "成交明细")):
                    tab = workspace.get_by_role("tab", name=label, exact=True)
                    if tab.get_attribute("aria-selected") != "true":
                        with page.expect_response(lambda response, expected=kind: response.request.method == "GET" and urlsplit(response.url).path == account_path + "/" + expected) as result:
                            tab.click()
                        assert result.value.status == 200
                    if kind == "positions":
                        state = workspace.get_by_label("持仓状态", exact=True)
                        if state.input_value() != "ALL":
                            with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path == account_path + "/positions"
                                                      and parse_qs(urlsplit(response.url).query).get("state") == ["ALL"]) as result:
                                state.select_option("ALL")
                            assert result.value.status == 200
                    expect(workspace.locator(".paper-record").first).to_be_visible()
                    expect(workspace.locator(".page-error")).to_have_count(0)
                    expect(selector).to_have_value(str(account_id))
                    snapshot_safe(page, evidence, f"paper-{kind}-{width}")
                    report["viewports"].append({"width": width, "tab": kind, "overflow": False,
                                               "visible_records": workspace.locator(".paper-record").count()})

                step = f"order_detail_{width}"
                workspace.get_by_role("tab", name="委托记录", exact=True).click()
                expect(workspace.locator(".paper-record").first).to_be_visible()
                with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path.startswith("/api/v2/paper/orders/")) as result:
                    workspace.locator(".paper-record").first.get_by_role("button", name="详情", exact=True).click()
                assert result.value.status == 200
                assert result.value.json()["account_id"] == str(account_id)
                dialog = page.get_by_role("dialog")
                expect(dialog).to_be_visible()
                expect(dialog).to_contain_text("订单 ID")
                title = dialog.locator(".app-dialog-body > header h2").inner_text()
                snapshot_safe(page, evidence, f"paper-detail-{width}", dialog=title)
                dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
                report["detail_checks"].append({"width": width, "same_account": True, "get_status": 200})

                step = f"open_dialog_without_submission_{width}"
                opener = workspace.get_by_role("button", name="模拟开仓", exact=True)
                expect(opener).to_be_enabled()
                opener.click()
                dialog = page.get_by_role("dialog", name="模拟开仓", exact=True)
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role("button", name="确认模拟委托", exact=True)).to_be_disabled()
                expect(dialog.get_by_label("最大滑点（bps）", exact=True)).to_have_attribute("max", "100")
                # A wrapping label can acquire its select's option text in the
                # browser label lookup. Diagnose only these non-sensitive enum
                # controls; use stable field names for the behavioral assertion.
                type_control = dialog.locator('select[name="order_type"]')
                tif_control = dialog.locator('select[name="time_in_force"]')
                diagnostic = dialog.evaluate("""root => {
                  const kinds = new Set(['MARKET','LIMIT']);
                  const rules = new Set(['IOC','GTC','FOK']);
                  const type = root.querySelector('select[name="order_type"]');
                  const tif = root.querySelector('select[name="time_in_force"]');
                  return {
                    order_type: type ? (kinds.has(type.value) ? type.value : 'UNEXPECTED') : 'MISSING',
                    time_in_force: tif ? (rules.has(tif.value) ? tif.value : 'UNEXPECTED') : 'MISSING',
                    options: tif ? Array.from(tif.options, option => ({
                      value: rules.has(option.value) ? option.value : 'UNEXPECTED',
                      hidden: option.hidden, disabled: option.disabled
                    })) : []
                  };
                }""")
                report.setdefault("open_dialog_diagnostics", []).append({
                    "width": width, "type_select_count": type_control.count(),
                    "tif_select_count": tif_control.count(),
                    "exact_label_match_count": dialog.get_by_label("成交规则", exact=True).count(),
                    "option_count": tif_control.locator("option").count(), **diagnostic,
                })
                expect(type_control).to_have_value("MARKET")
                expect(tif_control).to_have_value("IOC")
                expect(tif_control.locator("option")).to_have_count(1)
                assert diagnostic["options"] == [{"value": "IOC", "hidden": False, "disabled": False}]
                snapshot_safe(page, evidence, f"paper-open-dialog-{width}", dialog="模拟开仓")
                dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
                report["open_dialog_checks"].append({"width": width, "opened_only": True, "submitted": False})
        except Exception as exc:
            report["errors"].append(safe_error(exc, step))
        finally:
            try:
                report["logout_status"] = logout(page)
            except Exception as exc:
                report["errors"].append(safe_error(exc, "logout"))
            context.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only UI check of an exact pre-created PAPER smoke account")
    parser.add_argument("--account-id", required=True, type=UUID)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-paper-browser-"))
    report = {"errors": [], "blocked": [], "allowed_posts": {}, "record_counts": {}, "viewports": [],
              "detail_checks": [], "open_dialog_checks": [], "orders_mutated": False,
              "accounts_created": False, "existing_configuration_modified": False, "openai_called": False}
    pool = None
    try:
        runtime = read_env(SECRET_DIR / "formal-runtime.env")
        origin = runtime["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        assert origin == "https://43.108.44.74"
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = runtime[key]
        enrollment = recover_active_enrollment(owner["username"])
        assert enrollment, "EXISTING_MFA_REQUIRED_NO_ENROLLMENT_ALLOWED"
        pool = PostgresPool(read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"], min_size=1, max_size=1)
        pool.open()
        tenant, user = resolve_owner(pool, owner["username"])
        account = PostgresPaperRepository(pool).get_account(tenant, user, args.account_id)
        assert account.name.startswith(ACCOUNT_PREFIX) and account.status == "ACTIVE", "ACTIVE_DEDICATED_SMOKE_ACCOUNT_REQUIRED"
        run_browser(origin, owner, enrollment, user, args.account_id, evidence, report)
    except Exception as exc:
        report["errors"].append(safe_error(exc, "setup"))
    finally:
        if pool is not None:
            pool.close()
    report["ok"] = (not report["errors"] and not report["blocked"] and report.get("logout_status") == 200
                    and len(report["viewports"]) == 6 and len(report["detail_checks"]) == 2
                    and len(report["open_dialog_checks"]) == 2 and report.get("same_account_records") is True)
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({**report, "private_evidence": str(evidence)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
