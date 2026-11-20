"""Read-only post-deployment acceptance of PAPER preview and portfolio layout.

Requires the existing dedicated ``MCP模拟验收-`` account, owner enrollment and
runtime configuration. Never creates/enrolls/configures anything, submits an
order, or starts a research job. The only additional permitted POST is an exact
one-contract SOL PAPER preview bound to the selected account and instrument.

No raw responses, credentials, tokens, traces, HAR or video are saved. Portfolio
source rows and existing private portfolio reports are masked in screenshots.
Both genuinely empty portfolios and existing report states are accepted without
mocking, creating or changing the records.
Run manually after deployment; importing this file does not run acceptance.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from playwright.sync_api import expect, sync_playwright

from gatescope.adapters.gate_paper_market import GatePaperMarket
from smoke_mcp_paper import _terms, database_evidence
from smoke_paper_browser import (ACCOUNT_PREFIX, SECRET_DIR, PostgresPaperRepository, PostgresPool,
    allowed_request as existing_allowed_request, logout, next_totp, read_env, read_json,
    recover_active_enrollment, resolve_owner, response_is)

PREVIEW_PATH = "/api/v2/paper/orders:preview"


def allowed_request(origin: str, url: str, method: str, payload: object = None,
                    *, account_id: str, instrument_id: str) -> bool:
    """Pure allowlist: no execution endpoint or generic POST escape hatch."""
    if existing_allowed_request(origin, url, method, payload):
        return True
    parsed = urlsplit(url)
    if (method != "POST" or f"{parsed.scheme}://{parsed.netloc}" != origin
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != PREVIEW_PATH or not isinstance(payload, dict)):
        return False
    if set(payload) != {"account_id", "instrument_id", "timeframe", "side", "order_type", "contracts",
                        "time_in_force", "stop_price", "take_profit_price", "max_slippage_bps",
                        "ttl_seconds", "idempotency_key"}:
        return False
    try:
        UUID(account_id)
        UUID(instrument_id)
        UUID(payload["idempotency_key"])
        stop, target = Decimal(str(payload["stop_price"])), Decimal(str(payload["take_profit_price"]))
        return (payload["account_id"] == account_id and payload["instrument_id"] == instrument_id
            and payload["timeframe"] == "15m" and payload["side"] == "LONG"
            and payload["order_type"] == "MARKET" and payload["time_in_force"] == "IOC"
            and type(payload["contracts"]) is int and payload["contracts"] == 1
            and type(payload["ttl_seconds"]) is int and payload["ttl_seconds"] == 60
            and payload["max_slippage_bps"] == "50"
            and stop.is_finite() and target.is_finite() and 0 < stop < target < Decimal("1e9"))
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        return False


def safe_error(exc: Exception, step: str) -> dict:
    return {"step": step, "type": type(exc).__name__, "lines": [
        item.lineno for item in traceback.extract_tb(exc.__traceback__)
        if Path(item.filename).name == Path(__file__).name]}


def control_within_bounds(control: dict | None, dialog: dict | None, viewport: dict) -> bool:
    """Require the whole button inside both the dialog and visible viewport."""
    if control is None or dialog is None:
        return False
    screen = {"x": 0, "y": 0, **viewport}
    for box in (control, dialog, screen):
        if any(type(box.get(key)) not in (int, float) or not math.isfinite(box[key])
               for key in ("x", "y", "width", "height")):
            return False
        if box["width"] <= 0 or box["height"] <= 0:
            return False
    # One CSS pixel accommodates fractional layout coordinates, not a hidden
    # or partially clipped button. No coordinates/private content are logged.
    return all(control["x"] >= outer["x"]-1 and control["y"] >= outer["y"]-1
        and control["x"]+control["width"] <= outer["x"]+outer["width"]+1
        and control["y"]+control["height"] <= outer["y"]+outer["height"]+1
        for outer in (dialog, screen))


def scroll_control_into_view(page, dialog, control) -> None:
    # Scrolling also works for a disabled button and never triggers submission.
    control.scroll_into_view_if_needed()
    viewport = page.evaluate("({width: innerWidth, height: innerHeight})")
    assert control_within_bounds(control.bounding_box(), dialog.bounding_box(), viewport), "CONTROL_NOT_FULLY_REACHABLE"


def snapshot_safe(page, evidence: Path, name: str, *, dialog: str | None = None,
                  portfolio_reports: bool = False) -> None:
    # Same privacy boundary as smoke_paper_browser, extended for private source
    # research names/times in the new builder. Masking does not change geometry.
    if page.locator('input[type="password"], .mfa-code-inline, .one-time-secret').count():
        raise RuntimeError("SECRET_SCREEN_NOT_CAPTURED")
    assert page.locator("dialog[open]").count() == int(dialog is not None)
    if dialog:
        active = page.get_by_role("dialog", name=dialog, exact=True)
        expect(active).to_be_visible()
        assert active.evaluate("node => node.scrollWidth <= node.clientWidth + 2")
        assert active.locator(".app-dialog-body").evaluate("node => node.scrollWidth <= node.clientWidth + 2")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    masks = [
        page.locator(selector) for selector in (".profile-menu", ".trade-dashboard", ".trade-ledger",
            ".portfolio-source-list", ".portfolio-selection", ".portfolio-run-selector")]
    if portfolio_reports:
        masks.extend(page.locator(selector) for selector in (
            ".portfolio-workspace > section", ".portfolio-workspace > .research-metrics",
            ".portfolio-workspace > .research-result-heading", ".portfolio-workspace > .error-banner"))
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=True, mask=masks)


def run_browser(origin, owner, enrollment, user, account_id, instrument_id, evidence, report):
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
            allowed = allowed_request(origin, request.url, request.method, payload,
                                      account_id=str(account_id), instrument_id=str(instrument_id))
            if allowed:
                if request.method == "POST":
                    path = urlsplit(request.url).path
                    report["allowed_posts"][path] = report["allowed_posts"].get(path, 0) + 1
                    # Two viewport previews are the entire authorized budget.
                    if path == PREVIEW_PATH and report["allowed_posts"][path] > 2:
                        report["blocked"].append({"method": "POST", "reason": "PREVIEW_BUDGET"})
                        route.abort()
                        return
                route.continue_()
            else:
                report["blocked"].append({"method": request.method,
                    "same_origin": f"{urlsplit(request.url).scheme}://{urlsplit(request.url).netloc}" == origin})
                route.abort()

        context.route("**/*", fence)
        context.route_web_socket("**/*", lambda socket: (report["blocked"].append({"method": "WEBSOCKET"}), socket.close()))
        page = context.new_page()
        page.set_default_timeout(30000)
        expect.set_options(timeout=30000)
        page.on("pageerror", lambda _: report["errors"].append({"type": "JAVASCRIPT_ERROR"}))
        quotes = GatePaperMarket()  # Public read-only quotes; no exchange credentials.
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
            report["owner_bound"] = True

            for width in (1440, 390):
                step = f"paper_preview_{width}"
                page.set_viewport_size({"width": width, "height": 960})
                page.goto(origin + "/trading/paper", wait_until="domcontentloaded")
                workspace = page.get_by_role("region", name="模拟委托与持仓")
                expect(workspace).to_be_visible()
                selector = workspace.get_by_label("模拟交易账户", exact=True)
                option = selector.locator(f'option[value="{account_id}"]')
                expect(option).to_have_count(1)
                assert option.inner_text().startswith(ACCOUNT_PREFIX)
                if selector.input_value() != str(account_id):
                    with page.expect_response(lambda response: response.request.method == "GET"
                                              and urlsplit(response.url).path == account_path + "/snapshot") as result:
                        selector.select_option(str(account_id))
                    assert result.value.status == 200
                expect(selector).to_have_value(str(account_id))
                snapshot = read_json(page, account_path + "/snapshot")
                assert snapshot["account"]["id"] == str(account_id) and snapshot["environment"] == "PAPER"
                workspace.get_by_role("button", name="模拟开仓", exact=True).click()
                dialog = page.get_by_role("dialog", name="模拟开仓", exact=True)
                expect(dialog).to_be_visible()
                preview_button = dialog.get_by_role("button", name="预览模拟委托", exact=True)
                expect(preview_button).to_be_disabled()
                expect(dialog.get_by_role("button", name="确认模拟委托", exact=True)).to_have_count(0)
                dialog.locator(".instrument-trigger").click()
                with page.expect_response(lambda response: response.request.method == "GET"
                                          and urlsplit(response.url).path == "/api/v2/instruments"
                                          and parse_qs(urlsplit(response.url).query).get("q") == ["SOL_USDT"]) as result:
                    dialog.get_by_role("combobox", name="搜索币种", exact=True).fill("SOL_USDT")
                assert result.value.status == 200
                found = result.value.json()["items"]
                assert any(str(item["id"]) == str(instrument_id) and item["symbol"] == "SOL_USDT"
                           and item["market"].upper() == "FUTURES_USDT" for item in found)
                choice = dialog.get_by_role("option").filter(has=page.get_by_text("SOL_USDT", exact=True))
                choice = choice.filter(has=page.locator("small", has_text="USDT 合约"))
                expect(choice).to_have_count(1)
                choice.click()
                stop, target = _terms(quotes.get("SOL_USDT"))
                dialog.locator('input[name="stop_price"]').fill(stop)
                dialog.locator('input[name="take_profit_price"]').fill(target)
                dialog.locator('input[name="ttl_seconds"]').fill("60")
                expect(preview_button).to_be_enabled()
                with page.expect_response(lambda response: response.request.method == "POST"
                                          and urlsplit(response.url).path == PREVIEW_PATH) as result:
                    preview_button.click()
                assert result.value.status == 200
                preview = result.value.json()
                assert preview["environment"] == "PAPER" and preview["preview_only"] is True and preview["order_created"] is False
                assert preview["account"]["account"]["id"] == str(account_id)
                assert preview["checks"]["balance_reserved"] is False and preview["checks"]["depth_reserved"] is False
                assert preview["estimate"]["requested_contracts"] == 1
                assert preview["sizing"]["stop_loss_is_guaranteed"] is False
                for key in ("required_margin", "required_fees", "reserved_risk", "estimated_fill_fees"):
                    value = Decimal(str(preview["sizing"][key]))
                    assert value.is_finite() and value >= 0
                panel = dialog.get_by_role("region", name="模拟委托预览", exact=True)
                expect(panel).to_be_visible()
                for label in ("委托张数 → 币数量", "预计成交手续费", "全量委托预占资金 / 手续费",
                              "可用余额 / 当前预算最多张数", "全量委托风险预占"):
                    expect(panel.get_by_text(label, exact=True)).to_be_visible()
                confirm = dialog.get_by_role("button", name="确认模拟委托", exact=True)
                if preview["checks"]["budget_available"]:
                    expect(confirm).to_be_enabled()
                else:
                    expect(confirm).to_be_disabled()
                    expect(panel.get_by_role("alert")).to_be_visible()
                # Never click confirm, including when enabled.
                panel.locator("header").scroll_into_view_if_needed()
                snapshot_safe(page, evidence, f"paper-preview-{width}", dialog="模拟开仓")
                scroll_control_into_view(page, dialog, confirm)
                snapshot_safe(page, evidence, f"paper-preview-controls-{width}", dialog="模拟开仓")
                dialog.locator('input[name="contracts"]').fill("2")
                expect(dialog.get_by_role("button", name="预览模拟委托", exact=True)).to_be_visible()
                expect(dialog.get_by_role("button", name="确认模拟委托", exact=True)).to_have_count(0)
                dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
                report["preview_checks"].append({"width": width, "preview_status": 200, "fee_budget_visible": True,
                    "confirm_matches_budget": True, "confirm_scroll_reachable": True, "parameter_change_invalidates_preview": True,
                    "budget_available": bool(preview["checks"]["budget_available"]), "submitted": False, "overflow": False})

                step = f"portfolio_current_state_and_builder_{width}"
                current = read_json(page, "/api/v2/research/portfolios?limit=1&offset=0")
                empty = current["total"] == 0
                assert isinstance(current["items"], list) and bool(current["items"]) is not empty
                page.goto(origin + "/strategies/backtests?view=portfolio", wait_until="domcontentloaded")
                portfolio = page.get_by_role("region", name="共享资金组合回测", exact=True)
                expect(portfolio).to_be_visible()
                if empty:
                    expect(portfolio.get_by_text("暂无组合研究。", exact=False)).to_be_visible()
                else:
                    expect(portfolio.locator(".portfolio-run-selector select")).to_have_value(current["items"][0]["id"])
                    state = current["items"][0]["state"]
                    assert state in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED"}
                    if state == "SUCCEEDED":
                        expect(portfolio.get_by_role("heading", name="组合账户报告", exact=True)).to_be_visible()
                expect(portfolio.locator(".page-error")).to_have_count(0)
                snapshot_safe(page, evidence, f"portfolio-current-{width}", portfolio_reports=not empty)
                with page.expect_response(lambda response: response.request.method == "GET"
                                          and urlsplit(response.url).path == "/api/v2/research/backtests") as result:
                    portfolio.get_by_role("button", name="＋ 新建组合回测", exact=True).click()
                assert result.value.status == 200
                builder = page.get_by_role("dialog", name="新建共享资金回测", exact=True)
                expect(builder).to_be_visible()
                expect(builder.get_by_role("button", name="创建后台组合回测", exact=True)).to_be_disabled()
                expect(builder.locator('input[name="initial_equity"]')).to_have_value("10000")
                expect(builder.locator('input[name="max_positions"]')).to_have_attribute("max", "20")
                sources = result.value.json()["items"]
                can_select = any(item["state"] == "SUCCEEDED" for item in sources)
                if can_select:
                    # Selection is local form state only. Do not click submit.
                    checkbox = builder.locator('.portfolio-source-list input[type="checkbox"]').first
                    expect(checkbox).to_be_visible()
                    checkbox.check()
                    expect(builder.get_by_role("button", name="创建后台组合回测", exact=True)).to_be_enabled()
                    expect(builder.locator(".portfolio-selection")).to_be_visible()
                    checkbox.uncheck()
                    expect(builder.get_by_role("button", name="创建后台组合回测", exact=True)).to_be_disabled()
                # Exercise a true no-match filter without checking any source.
                builder.get_by_role("searchbox").fill("__SMOKE_NO_SOURCE_MATCH__")
                expect(builder.get_by_text("当前页没有匹配的已完成报告", exact=False)).to_be_visible()
                snapshot_safe(page, evidence, f"portfolio-builder-{width}", dialog="新建共享资金回测", portfolio_reports=not empty)
                submit_button = builder.get_by_role("button", name="创建后台组合回测", exact=True)
                expect(submit_button).to_be_disabled()
                scroll_control_into_view(page, builder, submit_button)
                snapshot_safe(page, evidence, f"portfolio-builder-controls-{width}", dialog="新建共享资金回测", portfolio_reports=not empty)
                builder.get_by_role("button", name="关闭弹窗", exact=True).click()
                report["portfolio_checks"].append({"width": width, "real_empty_state": empty,
                    "existing_state": None if empty else state, "source_selection_checked": can_select,
                    "builder_opened": True, "submit_scroll_reachable": True, "empty_filter": True, "job_created": False, "overflow": False})
        except Exception as exc:
            report["errors"].append(safe_error(exc, step))
        finally:
            quotes.close()
            try:
                report["logout_status"] = logout(page)
            except Exception as exc:
                report["errors"].append(safe_error(exc, "logout"))
            context.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only PAPER preview and portfolio dual-viewport acceptance")
    parser.add_argument("--account-id", required=True, type=UUID)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-optimization-browser-"))
    report = {"errors": [], "blocked": [], "allowed_posts": {}, "preview_checks": [], "portfolio_checks": [],
              "orders_mutated": False, "accounts_created": False, "research_jobs_created": False,
              "existing_configuration_modified": False, "openai_called": False}
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
        with pool.public_transaction() as connection:
            rows = connection.execute("""SELECT id FROM instruments WHERE symbol='SOL_USDT' AND venue='GATE'
                AND market='FUTURES_USDT' AND settle_asset='USDT' AND quote_asset='USDT' AND status='ACTIVE' LIMIT 2""").fetchall()
        assert len(rows) == 1
        before = database_evidence(pool, tenant, user, args.account_id)["web_database_counts"]
        run_browser(origin, owner, enrollment, user, args.account_id, rows[0][0], evidence, report)
        after = database_evidence(pool, tenant, user, args.account_id)["web_database_counts"]
        report["dedicated_account_fact_counts_unchanged"] = before == after
    except Exception as exc:
        report["errors"].append(safe_error(exc, "setup"))
    finally:
        if pool is not None:
            pool.close()
    report["ok"] = (not report["errors"] and not report["blocked"] and report.get("logout_status") == 200
        and report.get("owner_bound") is True and report.get("dedicated_account_fact_counts_unchanged") is True
        and len(report["preview_checks"]) == 2 and len(report["portfolio_checks"]) == 2
        and report["allowed_posts"].get(PREVIEW_PATH) == 2)
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({**report, "private_evidence": str(evidence)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
