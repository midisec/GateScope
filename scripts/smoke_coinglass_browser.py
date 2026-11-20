"""Production desktop/mobile acceptance. Only creates independent review records.

Uses an existing owner's encrypted login and MFA. No configuration, order,
strategy, watchlist, notification or historical pattern writes are permitted.
Screenshots remain private; output is limited to status/count evidence.
"""

import json
import logging
import os
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from prepare_service_envs import read_env, SECRET_DIR
from smoke_watchlist_history import AUTH_POSTS, PREVIEW, select_existing_watch, screenshot
from gatescope.security_secrets import totp_code


def fetch(page, path):
    return page.evaluate(
        """async path=>{const r=await fetch(path,{credentials:'same-origin'});return {status:r.status,data:await r.json()};}""",
        path,
    )


def main():
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-coinglass-browser-"))
    report = {"checks": [], "errors": [], "blocked": []}
    step = "configuration"
    try:
        env = read_env(SECRET_DIR / "formal-runtime.env")
        origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        assert origin == "https://43.108.44.74"
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in (
            "GATESCOPE_MIGRATION_DATABASE_URL",
            "GATESCOPE_IDENTITY_KEK",
            "GATESCOPE_IDENTITY_KEY_VERSION",
        ):
            os.environ[key] = env[key]
        enrollment = recover_active_enrollment(owner["username"])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
            context = browser.new_context(
                viewport={"width": 1440, "height": 960}, locale="zh-CN", service_workers="block"
            )

            def fence(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.scheme + "://" + parsed.netloc != origin or (
                    request.method not in {"GET", "HEAD", "OPTIONS"}
                    and not (
                        request.method == "POST"
                        and parsed.path in AUTH_POSTS | {PREVIEW, "/api/v2/coinglass/reviews"}
                    )
                ):
                    report["blocked"].append({"method": request.method, "path": parsed.path})
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", fence)
            page = context.new_page()
            page.set_default_timeout(30000)
            expect.set_options(timeout=30000)
            page.on("pageerror", lambda _: report["errors"].append({"type": "JAVASCRIPT_ERROR"}))
            try:
                step = "login"
                page.goto(origin + "/login", wait_until="domcontentloaded")
                page.locator('select[name="tenant"]').select_option("default")
                page.locator('input[name="username"]').fill(owner["username"])
                page.locator('input[name="password"]').fill(owner["password"])
                page.get_by_role("button", name="安全登录", exact=True).click()
                if enrollment:
                    code, _ = totp_code(enrollment["secret"])
                    page.locator(".mfa-code-inline").fill(code)
                    page.get_by_role("button", name="验证并进入系统", exact=True).click()
                expect(page.locator(".topbar")).to_be_visible()
                for symbol in ("BTC_USDT", "龙虾_USDT"):
                    step = "chart_" + symbol
                    page.set_viewport_size({"width": 1440, "height": 960})
                    instrument = select_existing_watch(page, symbol)
                    page.locator(".timeframes").get_by_role("button", name="15m", exact=True).click()
                    cg_button = page.get_by_role("button", name="Coinglass 副图", exact=True)
                    if cg_button.get_attribute("aria-pressed") != "true":
                        cg_button.click()
                    bundle = None
                    for _ in range(18):
                        received = fetch(
                            page, f"/api/v2/coinglass/chart?instrument_id={instrument}&timeframe=15m"
                        )
                        assert received["status"] == 200
                        bundle = received["data"]
                        if bundle["status"] in {"READY", "PARTIAL", "UNSUPPORTED", "ERROR"}:
                            break
                        page.wait_for_timeout(5000)
                    assert bundle and bundle["instrument_id"] == instrument and bundle["timeframe"] == "15m"
                    assert bundle["match"]["status"] == "EXACT", "EXACT_PROVIDER_MATCH_REQUIRED"
                    assert sum(len(s["points"]) for s in bundle["series"]) > 0, "PROVIDER_DATA_NOT_READY"
                    controls = page.get_by_role("region", name="Coinglass 衍生品数据")
                    controls.get_by_role("button", name="刷新数据", exact=True).click()
                    expect(controls).to_contain_text(symbol)
                    for width in (1440, 390):
                        page.set_viewport_size({"width": width, "height": 960})
                        expect(page.locator(".chart-host canvas").first).to_be_visible()
                        assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
                        if width == 390:
                            expect(controls.get_by_label("手机衍生品副图")).to_be_visible()
                        screenshot(page, evidence, f"chart-{symbol}-{width}")
                        page.locator(".chart-panel").screenshot(
                            path=str(evidence / f"plot-{symbol}-{width}.png")
                        )
                        report["checks"].append(
                            {
                                "kind": "chart",
                                "symbol": symbol,
                                "width": width,
                                "status": bundle["status"],
                                "series_points": {s["id"]: len(s["points"]) for s in bundle["series"]},
                            }
                        )
                    step = "forming_review_" + symbol
                    page.get_by_label("结构状态", exact=True).select_option("FORMING")
                    buttons = page.locator(".pattern-card").get_by_role(
                        "button", name="Coinglass 数据支撑", exact=True
                    )
                    expect(buttons.first).to_be_visible()
                    with page.expect_response(
                        lambda r: (
                            urlsplit(r.url).path == "/api/v2/coinglass/reviews" and r.request.method == "POST"
                        )
                    ) as response:
                        buttons.first.click()
                    result = response.value
                    report["last_review_http_status"] = result.status
                    assert result.status == 201, "FORMING_REVIEW_CREATION_FAILED"
                    saved = result.json()
                    review = saved["review"]
                    assert review["original"]["phase"] == "FORMING" and review["adjusted_win_rate"] is None
                    dialog = page.get_by_role("dialog")
                    expect(dialog).to_contain_text("原始结构 · 保持不变")
                    expect(dialog).to_contain_text("不是已确认的交易方向或止盈区")
                    retained = fetch(page, "/api/v2/coinglass/reviews/" + saved["review_id"])
                    assert retained["status"] == 200 and retained["data"] == saved
                    screenshot(page, evidence, f"review-{symbol}-mobile")
                    scenario_checked = False
                    if review["state"] == "READY" and review["original"]["forecast"].get("target"):
                        target = review["original"]["forecast"]["target"]
                        dialog.get_by_label("观察参考价", exact=True).fill(str(target["price"]))
                        with page.expect_response(
                            lambda r: (
                                urlsplit(r.url).path == "/api/v2/coinglass/reviews"
                                and r.request.method == "POST"
                            )
                        ) as scenario_response:
                            dialog.get_by_role("button", name="保存独立观察情景", exact=True).click()
                        assert scenario_response.value.status == 201
                        scenario_review = scenario_response.value.json()["review"]
                        assert scenario_review["scenario"]["risk_reward"] is None
                        assert scenario_review["original"]["forecast"] == review["original"]["forecast"]
                        dialog.get_by_role("button", name="在图表查看情景", exact=True).click()
                        expect(page.get_by_role("button", name="清除情景", exact=True)).to_be_visible()
                        page.locator(".chart-panel").screenshot(path=str(evidence / f"scenario-{symbol}.png"))
                        page.get_by_role("button", name="清除情景", exact=True).click()
                        scenario_checked = True
                    else:
                        dialog.get_by_role("button", name="返回形态", exact=True).click()
                    report["checks"].append(
                        {
                            "kind": "review",
                            "symbol": symbol,
                            "state": review["state"],
                            "metric_count": len(review["metric_evidence"]),
                            "immutable_round_trip": True,
                            "scenario_checked": scenario_checked,
                        }
                    )
                step = "mcp_preview"
                page.goto(origin + "/integrations/mcp", wait_until="domcontentloaded")
                page.get_by_text("调试单次调用（可选）", exact=True).click()
                panel = page.get_by_role("region", name="MCP 行情数据预览")
                expect(panel).to_be_visible()
                panel.locator(".instrument-trigger").click()
                panel.get_by_role("option").filter(has_text="BTC_USDT").first.click()
                for width, tf in ((1440, "1m"), (390, "15m")):
                    page.set_viewport_size({"width": width, "height": 960})
                    panel.get_by_label("MCP 输出周期").select_option(tf)
                    panel.get_by_label("MCP 输出条数").select_option("100")
                    with page.expect_response(
                        lambda r: urlsplit(r.url).path == "/api/v2/mcp/market-preview"
                    ) as response:
                        panel.get_by_role("button", name="读取最新缓存", exact=True).click()
                    assert response.value.status == 200
                    payload = response.value.json()
                    assert payload["timeframe"] == tf and 0 < len(payload["candles"]) <= 100
                    assert len(payload["indicators"]) == 8
                    assert all(
                        v["ready"] or v["bars_evaluated"] < v["warmup_bars"] for v in payload["indicators"]
                    )
                    assert payload["realtime"]["used_for_indicators"] is False
                    screenshot(page, evidence, f"mcp-preview-{width}")
                    report["checks"].append(
                        {
                            "kind": "mcp_preview",
                            "width": width,
                            "timeframe": tf,
                            "bars": len(payload["candles"]),
                            "indicators": 8,
                            "ready_indicators": sum(v["ready"] for v in payload["indicators"]),
                            "quality_flags": payload["quality_flags"],
                        }
                    )
            except Exception:
                if not page.locator('input[name="password"],.mfa-code-inline').count():
                    report["overflow_elements"] = page.evaluate(
                        """()=>Array.from(document.querySelectorAll('body *')).map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).filter(e=>e.right>innerWidth+2).slice(0,12)"""
                    )
                    try:
                        screenshot(page, evidence, "failed")
                    except Exception:
                        pass
                raise
            finally:
                try:
                    report["logout_status"] = page.evaluate(
                        """async()=>{const csrf=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('gatescope_csrf='))?.slice(15);const r=await fetch('/auth/logout',{method:'POST',credentials:'same-origin',headers:csrf?{'X-CSRF-Token':csrf}:{}});return r.status;}"""
                    )
                except Exception:
                    report["logout_status"] = None
                context.close()
                browser.close()
    except Exception as exc:
        report["errors"].append(
            {
                "step": step,
                "type": type(exc).__name__,
                "lines": [
                    f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename == __file__
                ],
            }
        )
    report["ok"] = not report["errors"] and not report["blocked"] and report.get("logout_status") == 200
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({**report, "private_evidence": str(evidence)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
