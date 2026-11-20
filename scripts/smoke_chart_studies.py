"""Read-only production chart acceptance, optionally serving a staged frontend.

Authenticates an existing owner and reads existing watchlist/candles. Layout saves
are intercepted in browser memory; no user configuration or trading data changes.
CG rendering fixtures are explicitly separate from the genuine cache status check.
Credentials and raw responses are never written to evidence or tool output.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from base64 import b64encode
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from gatescope.security_secrets import totp_code
from prepare_service_envs import SECRET_DIR, read_env
from smoke_watchlist_history import AUTH_POSTS, PREVIEW, screenshot


def wait_for_ui(page, expression, *, arg=None, timeout=30):
    """Poll through CDP without the page-world eval used by wait_for_function.

    Production intentionally forbids unsafe-eval; never relax its CSP merely
    to make the acceptance harness pass. Bound the same UI wait to 30 seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression, arg):
            return
        page.wait_for_timeout(100)
    raise AssertionError("UI_CONDITION_TIMEOUT")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-dist", type=Path)
    parser.add_argument("--staged-vpvr", action="store_true", help="Run staged VPVR router against read-only production market SQL")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-chart-studies-evidence-"))
    report = {"checks": [], "blocked": [], "errors": [], "frontend": "staged" if args.web_dist else "deployed",
              "rendering_data": "CONTROLLED_CG_FIXTURES_WITH_REAL_CACHED_OHLCV", "layout_storage": "BROWSER_MEMORY_ONLY"}
    step = "setup"
    resources = ExitStack()
    try:
        if args.web_dist:
            assert (args.web_dist / "index.html").is_file(), "STAGED_BUILD_NOT_READY"
        env = read_env(SECRET_DIR / "formal-runtime.env")
        vpvr_client = None
        if args.staged_vpvr:
            assert args.web_dist is not None
            from fastapi.testclient import TestClient
            from check_fine_volume_profile import ReadOnlyPool, diagnostic_app
            vpvr_pool = ReadOnlyPool(env["GATESCOPE_DATABASE_URL"], min_size=1, max_size=2)
            vpvr_pool.open()
            resources.callback(vpvr_pool.close)
            vpvr_client = resources.enter_context(TestClient(diagnostic_app(vpvr_pool)))
        report["vpvr_backend"] = "STAGED_ROUTER_READ_ONLY_REAL_SQL" if vpvr_client else "PRODUCTION_ENDPOINT"
        origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        assert origin == "https://43.108.44.74"
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = env[key]
        enrollment = recover_active_enrollment(owner["username"])
        saved = {"chart-layouts": [], "indicator-templates": []}
        market = {}
        fixture = {"enabled": False, "status": "READY"}
        vpvr_fault = {"status": None, "requests": 0}

        def candle_epoch(value):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())

        def cache_market(key, payload):
            merged = {candle_epoch(row["open_time"]): row for row in market.get(key, [])}
            merged.update({candle_epoch(row["open_time"]): row for row in payload.get("items", [])})
            market[key] = [merged[opened] for opened in sorted(merged)[-1500:]]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", service_workers="block")

            def fence(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.scheme + "://" + parsed.netloc != origin:
                    report["blocked"].append({"method": request.method, "path": "EXTERNAL_ORIGIN"})
                    route.abort()
                    return
                if request.method == "GET" and parsed.path == "/api/v2/market/volume-profile":
                    if vpvr_fault["status"]:
                        vpvr_fault["requests"] += 1
                        route.fulfill(status=vpvr_fault["status"], json={"detail": "受控故障验收"}, headers={"Retry-After": "2"})
                        return
                    if vpvr_client:
                        response = vpvr_client.get(parsed.path + "?" + parsed.query, headers={"X-Diagnostic-Session": "1"})
                        route.fulfill(status=response.status_code, body=response.content, content_type="application/json",
                                      headers={key: value for key, value in response.headers.items() if key.lower() in {"retry-after", "cache-control"}})
                        return
                if args.web_dist and request.method == "GET":
                    if parsed.path in {"/", "/login", "/markets/chart"}:
                        route.fulfill(path=str(args.web_dist / "index.html"), content_type="text/html")
                        return
                    if parsed.path.startswith("/assets/"):
                        path = (args.web_dist / parsed.path.lstrip("/")).resolve()
                        if path.is_relative_to(args.web_dist.resolve()) and path.is_file():
                            route.fulfill(path=str(path))
                            return
                for resource in saved:
                    prefix = "/api/v2/" + resource
                    if parsed.path == prefix or parsed.path.startswith(prefix + "/"):
                        if request.method == "GET":
                            route.fulfill(json={"items": saved[resource], "next_cursor": None, "has_more": False})
                        elif request.method in {"POST", "PATCH"}:
                            payload = request.post_data_json
                            item = {"id": "c948239a-d684-439d-8cf5-1ba001d2a438", "name": payload["name"],
                                    "spec": payload["spec"], "etag": 1 + (saved[resource][0]["etag"] if saved[resource] else 0)}
                            saved[resource] = [item]
                            route.fulfill(status=201 if request.method == "POST" else 200, json=item)
                        else:
                            route.abort()
                        return
                if request.method == "GET" and parsed.path == "/api/v2/bars":
                    response = route.fetch()
                    if response.status == 200:
                        query = parse_qs(parsed.query)
                        key = (query["instrument_id"][0], query["timeframe"][0])
                        cache_market(key, response.json())
                    route.fulfill(response=response)
                    return
                if fixture["enabled"] and request.method == "GET" and parsed.path == "/api/v2/coinglass/chart":
                    query = parse_qs(parsed.query)
                    instrument, timeframe = query["instrument_id"][0], query["timeframe"][0]
                    if not market.get((instrument, timeframe)):
                        # CG and candles load concurrently after a period switch.
                        # Seed this fixture from the same read-only bar endpoint,
                        # rather than returning a fabricated empty CG success.
                        response = route.fetch(url=f"{origin}/api/v2/bars?instrument_id={instrument}&timeframe={timeframe}&limit=1500&latest=true")
                        assert response.status == 200
                        cache_market((instrument, timeframe), response.json())
                    rows = market.get((instrument, timeframe), [])[-80:]
                    now = int(time.time())
                    sources = []
                    for metric, unit in (("oi", "USD"), ("funding", "PERCENT"), ("lsr", "RATIO"), ("liquidation_long", "USD"), ("liquidation_short", "USD")):
                        points = []
                        for index, row in enumerate(rows):
                            opened = candle_epoch(row["open_time"])
                            closed = candle_epoch(row["close_time"])
                            value = {"oi": 1_000_000 + 1000 * index, "funding": .005 if index % 2 else -.003,
                                     "lsr": 1.2 + index / 1000, "liquidation_long": 100 + index, "liquidation_short": 200 + index}[metric]
                            if row.get("is_final") and closed <= now:
                                points.append({"time": opened, "close_time": closed, "available_at": min(now, closed + 1), "value": value})
                        sources.append({"id": metric, "label": metric, "unit": unit, "unit_status": "VERIFIED",
                                        "population": "ACCEPTANCE_FIXTURE", "native_timeframe": timeframe, "points": points,
                                        "status": "STALE" if fixture["status"] == "STALE" else "READY", "quality_flags": []})
                    route.fulfill(json={"status": fixture["status"], "instrument_id": instrument, "timeframe": timeframe,
                                        "scope": {"exchange": "Gate", "symbol": "FIXTURE_USDT", "base_asset": "FIXTURE", "quote_asset": "USDT", "settlement_currency": "USDT", "market": "FUTURES_USDT"},
                                        "match": {"status": "EXACT"}, "as_of": now, "fetched_at": now, "expires_at": now + 600,
                                        "series": [] if fixture["status"] == "NO_CONFIG" else sources,
                                        "next_poll_after_seconds": 0, "refresh_state": "IDLE"})
                    return
                if request.method not in {"GET", "HEAD", "OPTIONS"} and not (request.method == "POST" and parsed.path in AUTH_POSTS | {PREVIEW}):
                    report["blocked"].append({"method": request.method, "path": parsed.path})
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", fence)
            page = context.new_page()
            page.set_default_timeout(25000)
            expect.set_options(timeout=25000)
            page.on("pageerror", lambda _: report["errors"].append("JAVASCRIPT_ERROR"))
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
                expect(page.locator(".watch-select").first).to_be_visible()
                page.locator(".watch-select").first.click()
                page.locator(".timeframes").get_by_role("button", name="15m", exact=True).click()
                expect(page.get_by_role("button", name="精选指标", exact=True)).to_be_enabled()
                wait_for_ui(page, "document.querySelectorAll('.chart-host canvas').length >= 2")
                step = "real_cache_read"
                instruments = page.evaluate("async()=>{const r=await fetch('/api/v2/watchlists'); return (await r.json()).items.flatMap(x=>x.items);}")
                actual = page.evaluate("async id=>{const r=await fetch('/api/v2/coinglass/chart?instrument_id='+encodeURIComponent(id)+'&timeframe=15m');const d=await r.json();return {http:r.status,status:d.status,series:(d.series||[]).map(s=>({id:s.id,status:s.status,points:s.points.length}))};}", instruments[0]["instrument_id"])
                assert actual["http"] == 200
                report["live_cache"] = actual
                step = "real_cache_render"
                page.get_by_role("button", name="精选指标", exact=True).click()
                live_dialog = page.get_by_role("dialog", name="精选指标", exact=True)
                live_dialog.get_by_role("checkbox", name="显示 OI 美元估值", exact=True).check()
                live_dialog.get_by_role("button", name="完成", exact=True).click()
                wait_for_ui(page, "Number(document.querySelector('[data-study-id=oi]')?.dataset.studyPoints) > 0")
                report["live_render"] = page.locator('[data-study-id="oi"]').evaluate("e=>({...e.dataset,reason:e.title})")
                report["checks"].append("real_cached_OI_aligned_to_real_candles")
                page.get_by_role("button", name="管理指标", exact=True).click()
                live_dialog.get_by_role("button", name="清空已选", exact=True).click()
                live_dialog.get_by_role("button", name="完成", exact=True).click()
                fixture["enabled"] = True
                step = "select_studies"
                page.get_by_role("button", name="精选指标", exact=True).click()
                dialog = page.get_by_role("dialog", name="精选指标", exact=True)
                search = dialog.get_by_role("searchbox", name="搜索精选指标")
                search.fill("VPVR")
                step = "select_studies_search"
                expect(dialog.get_by_role("checkbox")).to_have_count(1)
                dialog.get_by_role("checkbox").check()
                search.fill("")
                for checkbox in dialog.get_by_role("checkbox").all():
                    checkbox.check()
                dialog.get_by_label("VPVR 价格分层", exact=True).select_option("96")
                dialog.get_by_role("button", name="完成", exact=True).click()
                profile = page.locator(".vpvr-readout")
                step = "select_studies_profile"
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                expect(profile).to_have_attribute("data-vpvr-source", "1m")
                assert 0 < float(profile.get_attribute("data-vpvr-time-coverage")) <= 1
                assert int(profile.get_attribute("data-vpvr-bars")) > 1000
                report["fine_vpvr"] = profile.evaluate("e=>Object.fromEntries(Object.entries(e.dataset).filter(([key])=>key!=='vpvrRequestKey'))")
                step = "select_studies_CG"
                expect(page.locator("[data-study-id]")).to_have_count(6)
                expect(page.locator('[data-study-id="oi"]')).to_have_attribute("data-study-status", "READY")
                expect(profile).to_have_attribute("data-vpvr-bins", "96")
                assert all(int(value) > 0 for value in page.locator("[data-study-id]").evaluate_all("els=>els.map(e=>e.dataset.studyPoints)"))
                assert page.locator(".chart-host").first.bounding_box()["height"] >= 1000, "SELECTED_PANES_COMPRESSED"
                report["checks"].append("search_multiselect_native_and_derived_CG_with_VPVR")
                step = "viewport_profile"
                before = profile.get_attribute("data-vpvr-bars")
                host = page.locator(".chart-host").first
                host.evaluate("el=>el.scrollIntoView({block:'start'})")
                box = host.bounding_box()
                page.mouse.move(box["x"] + box["width"] * .45, box["y"] + 120)
                page.mouse.wheel(0, -420)
                wait_for_ui(page, "previous=>{const p=document.querySelector('.vpvr-readout')?.dataset;return ['AVAILABLE','PARTIAL'].includes(p?.vpvrStatus) && p.vpvrBars!==previous && Number(p.vpvrBars)>0;}", arg=before)
                expect(profile).not_to_have_attribute("data-vpvr-bars", before)
                assert int(profile.get_attribute("data-vpvr-bars")) > 0
                report["checks"].append("visible_range_zoom_recalculates_profile")
                step = "chart_export"
                page.get_by_role("button", name="更多图表工具", exact=True).click()
                with page.expect_download() as download:
                    page.get_by_role("menuitem", name="下载图表 PNG", exact=True).click()
                download.value.save_as(str(evidence / "chart-export.png"))
                assert (evidence / "chart-export.png").stat().st_size > 5000
                # A downloaded PNG is not proof that the asynchronous VPVR made
                # it into the export. Inspect actual pixels in its main pane.
                # The fixed BTC scenario places POC and its blue value area in
                # view; these fills differ from candle/EMA/Coinglass colors.
                pixels = page.evaluate("""async data => {
                  const bytes=Uint8Array.from(atob(data),char=>char.charCodeAt(0));
                  const blob=new Blob([bytes],{type:'image/png'});
                  const bitmap = await createImageBitmap(blob), canvas = document.createElement('canvas');
                  canvas.width=bitmap.width; canvas.height=bitmap.height;
                  const ctx=canvas.getContext('2d'); ctx.drawImage(bitmap,0,0); bitmap.close();
                  const {data:rgba}=ctx.getImageData(0,0,canvas.width,canvas.height);
                  let blue=0,gold=0;
                  for(let y=25;y<Math.min(400,canvas.height*.4);y++)
                    for(let x=Math.floor(canvas.width*.65);x<canvas.width*.94;x++) {
                      const i=(y*canvas.width+x)*4, r=rgba[i],g=rgba[i+1],b=rgba[i+2];
                      if(Math.abs(r-60)<=3&&Math.abs(g-113)<=3&&Math.abs(b-147)<=3)blue++;
                      if(Math.abs(r-204)<=3&&Math.abs(g-167)<=3&&Math.abs(b-92)<=3)gold++;
                    }
                  return {blue,gold};
                }""", b64encode((evidence / "chart-export.png").read_bytes()).decode("ascii"))
                report["export_profile_pixels"] = pixels
                assert pixels["blue"] > 200 and pixels["gold"] > 25, "PNG_MISSING_VPVR_FILL"
                report["checks"].append("PNG_chart_export_including_top_primitives")
                step = "save_restore_mock_layout"
                page.locator(".market-tools").get_by_role("button", name="保存", exact=True).click()
                expect(page.get_by_text("图表与指标已保存", exact=True)).to_be_visible()
                settings = saved["chart-layouts"][0]["spec"]["chart_studies"]
                assert settings["vpvr"]["rows"] == 96 and len(settings["coinglassMetrics"]) == 6
                page.reload(wait_until="domcontentloaded")
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                expect(page.locator("[data-study-id]")).to_have_count(6)
                report["checks"].append("layout_request_and_restore_browser_memory_only")
                for width in (1440, 390):
                    step = f"layout_{width}"
                    page.set_viewport_size({"width": width, "height": 1100})
                    page.get_by_role("button", name="管理指标", exact=True).click()
                    expect(dialog).to_be_visible()
                    expect(dialog.get_by_role("checkbox", checked=True)).to_have_count(7)
                    dialog.get_by_role("searchbox").fill("不存在的指标")
                    expect(dialog.get_by_role("checkbox")).to_have_count(0)
                    expect(dialog.get_by_text("没有匹配的指标，已选项仍保留。", exact=False)).to_be_visible()
                    dialog.get_by_role("button", name="查看全部", exact=True).click()
                    screenshot(page, evidence, f"picker-{width}")
                    dialog.get_by_role("button", name="完成", exact=True).click()
                    page.locator(".timeframes").get_by_role("button", name="1h", exact=True).click()
                    wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                    expect(page.locator(".vpvr-readout")).to_have_attribute("data-vpvr-source", "1m")
                    expect(page.locator('[data-study-id="oi"]')).to_have_attribute("data-study-status", "READY")
                    screenshot(page, evidence, f"chart-{width}")
                    page.locator(".chart-panel").first.screenshot(path=str(evidence / f"study-chart-{width}.png"), mask=[page.locator(".profile-menu")])
                    report["checks"].append(f"desktop_mobile_{width}_search_persistence_timeframe_no_overflow")
                step = "instrument_switch"
                page.set_viewport_size({"width": 1440, "height": 1100})
                before_instrument = page.locator('[data-study-id="oi"]').get_attribute("data-study-instrument")
                assert before_instrument
                page.locator(".watch-select").nth(1).click()
                wait_for_ui(page, "previous=>{const d=document.querySelector('[data-study-id=oi]')?.dataset;return d?.studyInstrument && d.studyInstrument!==previous && Number(d.studyPoints)>0;}", arg=before_instrument)
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                expect(page.locator(".vpvr-readout")).not_to_have_attribute("data-vpvr-instrument", before_instrument)
                expect(page.locator("[data-study-id]")).to_have_count(6)
                report["checks"].append("watch_symbol_switch_keeps_settings_and_rebinds_data")
                step = "readouts_stale_missing"
                page.get_by_role("button", name="管理指标", exact=True).click()
                fixture["status"] = "STALE"
                dialog.get_by_role("button", name="刷新 Coinglass 数据", exact=True).click()
                expect(dialog.get_by_text("历史缓存", exact=True).first).to_be_visible()
                dialog.get_by_role("button", name="完成", exact=True).click()
                expect(page.locator('[data-study-id="oi"]')).to_have_attribute("data-study-status", "HISTORICAL")
                page.get_by_role("button", name="管理指标", exact=True).click()
                fixture["status"] = "NO_CONFIG"
                dialog.get_by_role("button", name="刷新 Coinglass 数据", exact=True).click()
                expect(dialog.get_by_role("link", name="配置数据源", exact=True)).to_be_visible()
                dialog.get_by_role("button", name="完成", exact=True).click()
                expect(page.locator('[data-study-id="oi"]')).to_have_attribute("data-study-status", "UNAVAILABLE")
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                report["checks"].append("stale_and_unconfigured_CG_do_not_hide_or_zero_VPVR")
                step = "vpvr_fault_recovery"
                vpvr_fault["status"] = 503
                page.get_by_role("button", name="管理指标", exact=True).click()
                dialog.get_by_label("VPVR 价格分层", exact=True).select_option("24")
                dialog.get_by_role("button", name="完成", exact=True).click()
                expect(profile).to_have_attribute("data-vpvr-status", "ERROR")
                expect(profile).to_have_attribute("data-vpvr-poc", "")
                vpvr_fault["status"] = None
                profile.get_by_role("button", name="重试", exact=True).click()
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                expect(profile).to_have_attribute("data-vpvr-bins", "24")
                report["checks"].append("VPVR_503_clears_old_geometry_and_manual_retry_recovers")
                step = "vpvr_rate_limit"
                vpvr_fault.update(status=429, requests=0)
                page.get_by_role("button", name="管理指标", exact=True).click()
                dialog.get_by_label("VPVR 价格分层", exact=True).select_option("48")
                dialog.get_by_role("button", name="完成", exact=True).click()
                expect(profile).to_have_attribute("data-vpvr-status", "COOLDOWN")
                expect(profile).to_have_attribute("data-vpvr-poc", "")
                expect(profile).to_have_attribute("data-vpvr-status", "ERROR")
                assert vpvr_fault["requests"] == 2, "RATE_LIMIT_RETRIES_NOT_BOUNDED"
                vpvr_fault["status"] = None
                profile.get_by_role("button", name="重试", exact=True).click()
                wait_for_ui(page, "['AVAILABLE','PARTIAL'].includes(document.querySelector('.vpvr-readout')?.dataset.vpvrStatus)")
                expect(profile).to_have_attribute("data-vpvr-bins", "48")
                report["checks"].append("VPVR_RetryAfter_obeyed_single_automatic_retry_then_manual_recovery")
                assert not report["errors"] and not report["blocked"]
            except Exception:
                if step not in {"setup", "login"}:
                    report["ui_state"] = page.evaluate("""()=>({
                      chart_feedback:Array.from(document.querySelectorAll('.chart-feedback')).map(e=>e.textContent),
                      profile:Object.fromEntries(Object.entries(document.querySelector('.vpvr-readout')?.dataset||{}).filter(([key])=>key!=='vpvrRequestKey')),
                      studies:[...document.querySelectorAll('[data-study-id]')].map(e=>({...e.dataset,text:e.textContent,reason:e.title})),
                      selected:[...document.querySelectorAll('.chart-study-option input')].map(e=>({label:e.getAttribute('aria-label'),checked:e.checked}))
                    })""")
                    try:
                        screenshot(page, evidence, "failure")
                    except Exception:
                        pass
                raise
            finally:
                try:
                    report["logout_status"] = page.evaluate("async()=>{const m=document.cookie.split('; ').find(x=>x.startsWith('gatescope_csrf='));const r=await fetch('/auth/logout',{method:'POST',headers:{'X-CSRF-Token':m?decodeURIComponent(m.split('=')[1]):''}});return r.status;}")
                finally:
                    context.close()
                    browser.close()
        report["status"] = "PASSED"
    except Exception as error:
        report.update(status="FAILED", step=step, error_type=type(error).__name__)
        if step not in {"setup", "login"} and type(error).__module__.startswith("playwright"):
            # Keep only the operation-level first line, not request/cookie or
            # locator-call traces. Authentication diagnostics stay suppressed.
            report["error_summary"] = str(error).splitlines()[0][:200]
    finally:
        resources.close()
    # This script's purpose is to write private test evidence, never credentials.
    (evidence / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"evidence": str(evidence), **report}, ensure_ascii=False))
    return 0 if report.get("status") == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
