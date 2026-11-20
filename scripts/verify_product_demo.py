"""Real password-only demo login and isolated, non-trading browser acceptance.

Only encrypted login/logout and read-only indicator/order previews may POST.
No orders, credentials, MFA methods, bindings, layouts or user settings change.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from playwright.sync_api import expect, sync_playwright

from prepare_service_envs import SECRET_DIR, read_env
from smoke_chart_studies import wait_for_ui


def check_database(env, account):
    with psycopg.connect(env["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        source = connection.execute("""SELECT t.id,u.id,u.mfa_required FROM tenants t
          JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id
          WHERE t.slug='default' AND u.normalized_username='owner' AND m.role='OWNER' AND m.status='ACTIVE'""").fetchone()
        target = connection.execute("""SELECT t.id,u.id,u.mfa_required,m.role FROM tenants t
          JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id
          WHERE t.slug=%s AND u.normalized_username=%s AND m.status='ACTIVE'""",
                                    (account["tenant_slug"], account["username"].lower())).fetchone()
        assert source and target and source[0] != target[0] and source[1] != target[1]
        assert source[2] is True and target[2] is False and target[3] == "OWNER"
        assert str(target[0]) == account["tenant_id"] and str(target[1]) == account["user_id"]
        assert connection.execute("SELECT count(*) FROM memberships WHERE user_id=%s AND tenant_id=%s", (target[1], source[0])).fetchone()[0] == 0
        expected = []
        for tenant, user in (source[:2], target[:2]):
            rows = connection.execute("""SELECT w.name,i.instrument_id,i.position,i.default_timeframe
              FROM watchlists w JOIN watchlist_items i ON i.watchlist_id=w.id AND i.tenant_id=w.tenant_id
              WHERE w.tenant_id=%s AND w.owner_user_id=%s AND w.deleted_at IS NULL
              ORDER BY w.name,i.position,i.instrument_id""", (tenant, user)).fetchall()
            expected.append(rows)
        assert expected[0] == expected[1] and expected[1]
        for table in ("chart_layouts", "indicator_templates"):
            resources = [connection.execute(f"SELECT name,spec_json FROM {table} WHERE tenant_id=%s AND owner_user_id=%s AND deleted_at IS NULL ORDER BY name", (tenant, user)).fetchall()
                         for tenant, user in (source[:2], target[:2])]
            assert resources[0] == resources[1]
        rules = connection.execute("""SELECT r.status,v.routing_json,v.created_by FROM alert_rules r
          JOIN alert_rule_versions v ON v.alert_rule_id=r.id AND v.version=r.current_version AND v.tenant_id=r.tenant_id
          WHERE r.tenant_id=%s AND r.owner_user_id=%s""", target[:2]).fetchall()
        assert all(row[0] == "PAUSED" and row[1] == {"channel_ids": []} and row[2] == target[1] for row in rules)
        assert connection.execute("SELECT count(*) FROM paper_accounts WHERE tenant_id=%s AND owner_user_id=%s AND realized_pnl<>0", target[:2]).fetchone()[0] == 0
        for table in ("paper_orders", "paper_positions", "telegram_connections", "secret_credentials"):
            assert connection.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (target[0],)).fetchone()[0] == 0
        source_watch = connection.execute("SELECT id FROM watchlists WHERE tenant_id=%s AND owner_user_id=%s AND deleted_at IS NULL LIMIT 1", source[:2]).fetchone()
        source_paper = connection.execute("SELECT id FROM paper_accounts WHERE tenant_id=%s AND owner_user_id=%s LIMIT 1", source[:2]).fetchone()
        return {"watch_items": len(expected[1]), "rules_paused": len(rules)}, str(source_watch[0]), str(source_paper[0])


def main():
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-product-demo-evidence-"))
    report = {"checks": [], "blocked": [], "errors": [], "orders_submitted": 0}
    step = "setup"
    try:
        env = read_env(SECRET_DIR / "formal-runtime.env")
        account = json.loads((SECRET_DIR / "product-test-login.json").read_text())
        origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        assert origin == "https://43.108.44.74"
        counts, owner_watch, owner_paper = check_database(env, account)
        report["copied"] = counts
        report["checks"].append("separate_tenant_owner_mfa_unchanged_watchlist_equal_no_history_or_secrets_copied")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN", service_workers="block")
            allowed_posts = {"/auth/login", "/auth/logout", "/api/v2/indicators/preview", "/api/v2/paper/orders:preview"}

            def fence(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.scheme+"://"+parsed.netloc != origin or (request.method not in {"GET", "HEAD", "OPTIONS"} and not (request.method == "POST" and parsed.path in allowed_posts)):
                    report["blocked"].append({"method": request.method, "path": parsed.path if parsed.netloc == urlsplit(origin).netloc else "EXTERNAL_ORIGIN"})
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", fence)
            page = context.new_page()
            page.set_default_timeout(25000)
            expect.set_options(timeout=25000)
            page.on("pageerror", lambda _: report["errors"].append("JAVASCRIPT_ERROR"))
            try:
                step = "password_only_login"
                page.goto(origin+"/login", wait_until="domcontentloaded")
                page.locator('select[name="tenant"]').select_option(account["tenant_slug"])
                page.locator('input[name="username"]').fill(account["username"])
                page.locator('input[name="password"]').fill(account["password"])
                page.get_by_role("button", name="安全登录", exact=True).click()
                expect(page.locator(".topbar")).to_be_visible()
                me = page.evaluate("async()=>{const r=await fetch('/auth/me');return r.json()}")
                assert me["mfa_required"] is False and me["auth_strength"] == "PASSWORD"
                assert me["user"]["id"] == account["user_id"] and me["user"]["tenant_id"] == account["tenant_id"]
                report["checks"].append("real_encrypted_password_login_without_authenticator_password_strength_preserved")

                step = "watchlist_and_isolation"
                expect(page.locator(".watch-select").first).to_be_visible()
                wait_for_ui(page, "document.querySelectorAll('.chart-host canvas').length>=2")
                data = page.evaluate("""async paths=>{const result=[];for(const path of paths){const r=await fetch(path);result.push({status:r.status,data:await r.json()});}return result;}""", ["/api/v2/watchlists", "/api/v2/paper-accounts", "/api/v2/users", f"/api/v2/watchlists/{owner_watch}", f"/api/v2/paper/accounts/{owner_paper}/snapshot"])
                assert all(row["status"] == 200 for row in data[:3])
                assert data[3]["status"] == data[4]["status"] == 404
                assert {row["id"] for row in data[2]["data"]["items"]} == {account["user_id"]}
                watched = [item for group in data[0]["data"]["items"] for item in group["items"]]
                assert len(watched) == counts["watch_items"]
                report["checks"].append("own_watchlist_visible_original_owner_resources_404")

                step = "paper_preview_only"
                accounts = data[1]["data"]["items"]
                assert accounts and all(Decimal(str(row["realized_pnl"])) == 0 for row in accounts)
                instrument = next(row for row in watched if row["symbol"] == "BTC_USDT")
                bars = page.evaluate("async id=>{const r=await fetch('/api/v2/bars?instrument_id='+id+'&timeframe=1m&limit=2&latest=true');return r.json();}", instrument["instrument_id"])
                close = Decimal(str(bars["items"][-1]["close"]))
                payload = {"account_id": accounts[0]["id"], "instrument_id": instrument["instrument_id"], "timeframe": "15m", "side": "LONG", "contracts": 1,
                           "stop_price": str((close*Decimal('.98')).quantize(Decimal('.1'))), "take_profit_price": str((close*Decimal('1.04')).quantize(Decimal('.1'))),
                           "order_type": "MARKET", "time_in_force": "IOC", "idempotency_key": "demo-read-only-preview-v1", "max_slippage_bps": "50", "ttl_seconds": 60}
                preview = page.evaluate("""async payload=>{const c=document.cookie.split('; ').find(v=>v.startsWith('gatescope_csrf='));const r=await fetch('/api/v2/paper/orders:preview',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':c?decodeURIComponent(c.split('=')[1]):''},body:JSON.stringify(payload)});const d=await r.json();return {status:r.status,error:d.error?.code,environment:d.environment};} """, payload)
                report["paper_preview"] = preview
                assert preview["status"] == 200, "PAPER_PREVIEW_UNAVAILABLE"
                report["checks"].append("fresh_password_session_paper_order_preview_no_order_created")

                step = "module_pages"
                routes = ["/strategies/alerts", "/strategies/backtests", "/trading/paper", "/integrations/mcp", "/integrations/data-sources", "/trading/exchange-accounts", "/settings/notifications", "/settings/account"]
                for path in routes:
                    page.goto(origin+path, wait_until="domcontentloaded")
                    expect(page.locator(".topbar")).to_be_visible()
                    assert page.url == origin+path
                    assert page.locator(".mfa-code-inline").count() == 0
                report["checks"].append("all_eight_module_routes_accessible_without_login_mfa")
                step = "mobile"
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(origin+"/markets/chart", wait_until="domcontentloaded")
                expect(page.locator(".topbar")).to_be_visible()
                wait_for_ui(page, "document.querySelectorAll('.chart-host canvas').length>=2")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
                page.screenshot(path=str(evidence/"mobile.png"), full_page=True, mask=[page.locator(".profile-menu")])
                report["checks"].append("mobile_chart_no_horizontal_overflow")
                assert not report["blocked"] and not report["errors"]
            finally:
                report["logout_status"] = page.evaluate("async()=>{const c=document.cookie.split('; ').find(v=>v.startsWith('gatescope_csrf='));const r=await fetch('/auth/logout',{method:'POST',headers:{'X-CSRF-Token':c?decodeURIComponent(c.split('=')[1]):''}});return r.status}")
                context.close()
                browser.close()
        check_database(env, account)
        report["status"] = "PASSED"
    except Exception as error:
        report.update(status="FAILED", step=step, error_type=type(error).__name__)
    (evidence/"summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"evidence": str(evidence), **report}, ensure_ascii=False))
    return 0 if report.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
