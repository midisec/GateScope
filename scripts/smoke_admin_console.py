"""Owner browser acceptance: read-only admin data, never user/config mutations.

Optional staged assets/router use real production reads with READ ONLY SQL.
Only login, MFA verification, logout and read-only indicator preview may POST.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from apps.api.admin_console import build_admin_console_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.identity import Role
from gatescope.formal_auth import FormalSession, _parse_session_token
from gatescope.security_secrets import totp_code
from bootstrap_formal_owner_mfa import recover_active_enrollment
from prepare_service_envs import read_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=Path("/home/ubuntu/workspace/crypto_gate_trade"))
    parser.add_argument("--web-dist", type=Path)
    parser.add_argument("--staged-backend", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-admin-evidence-"))
    report = {"checks": [], "blocked": [], "errors": [], "api": [], "user_mutations": 0,
              "mode": "STAGED_READ_ONLY_REAL_DATA" if args.staged_backend else "DEPLOYED"}
    step = "setup"
    with ExitStack() as stack:
        try:
            secrets_dir = args.runtime_root/"data/runtime-secrets"
            env = read_env(secrets_dir/"formal-runtime.env")
            owner = json.loads((secrets_dir/"owner-login.json").read_text())
            origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
            assert origin == "https://43.108.44.74"
            for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
                os.environ[key] = env[key]
            enrollment = recover_active_enrollment(owner["username"])
            actor = {"session": None}
            diagnostic = None
            if args.staged_backend:
                assert args.web_dist and (args.web_dist/"index.html").is_file()
                pool = PostgresPool(env["GATESCOPE_DATABASE_URL"], min_size=1, max_size=2)
                pool.open()
                stack.callback(pool.close)
                application = FastAPI()

                @application.middleware("http")
                async def session_context(request: Request, call_next):
                    request.state.auth_session = actor["session"]
                    request.state.trace_id = "admin-readonly-acceptance"
                    assert request.method == "GET"
                    return await call_next(request)

                application.include_router(build_admin_console_router(pool, SimpleNamespace(pool=pool)))
                diagnostic = stack.enter_context(TestClient(application))
            pw = stack.enter_context(sync_playwright())
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
            stack.callback(browser.close)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN", service_workers="block")
            stack.callback(context.close)
            allowed_posts = {"/auth/login", "/auth/mfa/verify", "/auth/logout", "/api/v2/indicators/preview"}

            def fence(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.scheme+"://"+parsed.netloc != origin or (request.method not in {"GET", "HEAD", "OPTIONS"} and not (request.method == "POST" and parsed.path in allowed_posts)):
                    report["blocked"].append({"method": request.method, "path": parsed.path if parsed.netloc == urlsplit(origin).netloc else "EXTERNAL"})
                    route.abort()
                    return
                if diagnostic and request.method == "GET" and parsed.path.startswith("/api/v2/admin/"):
                    start = time.monotonic()
                    result = diagnostic.get(parsed.path+"?"+parsed.query)
                    report["api"].append({"path": parsed.path, "status": result.status_code, "ms": round((time.monotonic()-start)*1000)})
                    route.fulfill(status=result.status_code, body=result.content, content_type="application/json")
                    return
                if args.web_dist and request.method == "GET":
                    if parsed.path == "/login" or parsed.path == "/markets/chart" or parsed.path.startswith("/admin/"):
                        route.fulfill(path=str(args.web_dist/"index.html"), content_type="text/html")
                        return
                    if parsed.path.startswith("/assets/"):
                        asset = (args.web_dist/parsed.path.lstrip("/")).resolve()
                        if asset.is_relative_to(args.web_dist.resolve()) and asset.is_file():
                            route.fulfill(path=str(asset))
                            return
                route.continue_()

            context.route("**/*", fence)
            page = context.new_page()
            page.set_default_timeout(25000)
            expect.set_options(timeout=25000)
            page.on("pageerror", lambda _: report["errors"].append("JAVASCRIPT_ERROR"))
            step = "owner_login"
            page.goto(origin+"/login", wait_until="domcontentloaded")
            page.locator('select[name="tenant"]').select_option("default")
            page.locator('input[name="username"]').fill(owner["username"])
            page.locator('input[name="password"]').fill(owner["password"])
            page.get_by_role("button", name="安全登录", exact=True).click()
            if enrollment:
                step = "owner_mfa"
                code, _ = totp_code(enrollment["secret"])
                page.locator(".mfa-code-inline").fill(code)
                page.get_by_role("button", name="验证并进入系统", exact=True).click()
                expect(page.locator(".setup-shell")).to_have_count(0)
            expect(page.locator(".topbar")).to_be_visible()
            step = "owner_session"
            me = page.evaluate("async()=>{const r=await fetch('/auth/me');return r.json()}")
            assert me["user"]["role"] == "OWNER" and me["mfa_required"] is False
            token = next(c["value"] for c in context.cookies() if c["name"] == "gatescope_session")
            tenant, user, sid = _parse_session_token(token)
            now = datetime.now(timezone.utc)
            actor["session"] = FormalSession(sid, tenant, user, me["user"]["username"], Role.OWNER,
                datetime.fromisoformat(me["expires_at"]), now, now, me["auth_strength"], True)
            report["checks"].append("actual_owner_password_and_existing_mfa_login")
            try:
                for part, title in (("overview", "管理概览"), ("users", "用户管理"), ("resources", "资源统计"), ("audit", "操作审计")):
                    step = "route_"+part
                    page.goto(origin+"/admin/"+part, wait_until="domcontentloaded")
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                    expect(page.locator(".admin-scope").first).to_be_visible()
                    expect(page.locator(".admin-main .page-loading")).to_have_count(0)
                    expect(page.locator(".admin-main .page-error")).to_have_count(0)
                    assert page.locator(".topbar").count() == 0
                    if part == "users":
                        page.get_by_role("button", name="详情", exact=True).first.click()
                        expect(page.get_by_role("dialog")).to_be_visible()
                        page.get_by_role("button", name="关闭弹窗").click()
                        page.get_by_role("button", name="创建用户", exact=True).click()
                        expect(page.get_by_role("dialog").locator('input[name="initial_password"]')).to_be_visible()
                        page.get_by_role("dialog").get_by_role("button", name="取消", exact=True).click()
                        page.locator('input[name="q"]').fill("no-such-member-admin-acceptance")
                        page.get_by_role("button", name="查询", exact=True).click()
                        expect(page.get_by_text("没有符合条件的用户", exact=True)).to_be_visible()
                    page.screenshot(path=str(evidence/(part+"-desktop.png")), full_page=True, mask=[page.locator(".admin-identity")])
                    report["checks"].append(part+"_loaded_no_trading_shell")
                step = "mobile"
                page.set_viewport_size({"width": 390, "height": 844})
                for part in ("overview", "users", "resources", "audit"):
                    page.goto(origin+"/admin/"+part, wait_until="domcontentloaded")
                    expect(page.locator(".admin-scope").first).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
                    page.screenshot(path=str(evidence/(part+"-mobile.png")), full_page=True)
                report["checks"].append("all_four_mobile_routes_no_document_overflow")
                assert not report["blocked"] and not report["errors"]
            finally:
                page.evaluate("async()=>{const c=document.cookie.split('; ').find(v=>v.startsWith('gatescope_csrf='));await fetch('/auth/logout',{method:'POST',headers:{'X-CSRF-Token':c?decodeURIComponent(c.split('=')[1]):''}})}")
            report["status"] = "PASSED"
        except Exception as error:
            report.update(status="FAILED", step=step, error_type=type(error).__name__)
        (evidence/"summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"evidence": str(evidence), **report}, ensure_ascii=False))
    return 0 if report.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
