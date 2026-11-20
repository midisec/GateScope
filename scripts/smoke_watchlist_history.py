"""Read-only production desktop/mobile acceptance for watched contract history.

Run on the deployment host with its existing Playwright environment. Selects
only an existing watchlist item, switches all eight chart periods, and reads the
automatically refreshed history dialog. Never creates watchlists, studies,
strategies, notifications or orders, and never changes saved layouts/settings.
Only encrypted login, existing MFA verification, logout, and the chart's
read-only indicator preview calculation may POST. Private screenshots and a
redacted summary are stored in a mode-0700 temporary evidence directory.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from prepare_service_envs import SECRET_DIR, read_env
from gatescope.security_secrets import totp_code

AUTH_POSTS = frozenset(("/auth/login", "/auth/mfa/verify", "/auth/logout"))
PREVIEW = "/api/v2/indicators/preview"
PROGRESS = "/api/v2/patterns/watchlist-progress"
BARS = "/api/v2/bars"
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d")
HISTORY_STATES = frozenset(("PENDING", "BACKFILLING", "READY", "SOURCE_LIMIT", "GAP", "RETRYING"))
REPLAY_STATES = frozenset(("WAITING_HISTORY", "QUEUED", "RUNNING", "READY", "FAILED"))


def endpoint_kind(path):
    return {BARS: "bars", PROGRESS: "watchlist_progress", PREVIEW: "indicator_preview"}.get(path, "other_api")


def response_is(response, path, **query):
    parsed = urlsplit(response.url)
    values = parse_qs(parsed.query, keep_blank_values=True)
    return (response.request.method == "GET" and response.status == 200 and parsed.path == path
            and all(values.get(key) == [str(value)] for key, value in query.items()))


def screenshot(page, evidence, name):
    if page.locator('input[name="password"], .mfa-code-inline').count():
        raise RuntimeError("AUTH_SCREEN_NOT_CAPTURED")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=True,
                    mask=[page.locator(".profile-menu")])


def select_existing_watch(page, symbol):
    # Resolve only the authenticated user's existing list. This browser fetch
    # stays inside the same-origin route fence; its body is never recorded.
    payload = page.evaluate("""async () => {
      const response = await fetch('/api/v2/watchlists', {credentials:'same-origin'});
      if (response.status !== 200) throw new Error('WATCHLIST_READ_UNAVAILABLE');
      return response.json();
    }""")
    matches = {str(item["instrument_id"]) for group in payload["items"]
               for item in group["items"] if item["symbol"] == symbol}
    assert len(matches) == 1, "EXISTING_WATCHLIST_SYMBOL_NOT_UNIQUE_OR_MISSING"
    instrument = matches.pop()
    # The visible name is formatted '龙虾 / USDT'. The adjacent remove control
    # retains the exact exchange identifier, but is only used as a locator: it
    # is NEVER clicked. Select the item's normal chart button, not add/remove.
    item = page.locator(".watch-symbol").filter(has=page.get_by_role(
        "button", name=f"将 {symbol} 移出自选", exact=True)).first
    select = item.locator(".watch-select")
    expect(select).to_be_visible()
    select.click()
    expect(select).to_have_class("watch-select selected")
    expect(page.get_by_role("region", name="形态与波浪", exact=True)).to_contain_text(symbol)
    return instrument


def verify_period(page, instrument, timeframe, width):
    button = page.locator(".timeframes").get_by_role("button", name=timeframe, exact=True)
    expect(button).to_be_enabled()
    started = time.perf_counter()
    with page.expect_response(lambda response: response_is(
        response, BARS, instrument_id=instrument, timeframe=timeframe, limit=1500, latest="true"
    )) as loaded:
        if "active" in (button.get_attribute("class") or "").split():
            # Clicking an already selected period does not trigger React state.
            # Use the existing read-only refresh control to prove a real fetch.
            refresh = page.get_by_role("region", name="行情缓存状态", exact=True).get_by_role(
                "button", name="刷新状态", exact=True)
            expect(refresh).to_be_enabled()
            refresh.click()
        else:
            button.click()
    payload = loaded.value.json()
    rows = payload["items"]
    assert rows, "CHART_RETURNED_NO_REAL_BARS"
    assert all(str(row["instrument_id"]) == instrument and row["timeframe"] == timeframe for row in rows)
    assert all(rows[index]["open_time"] < rows[index + 1]["open_time"] for index in range(len(rows) - 1))
    expect(button).to_have_class("active")
    expect(page.locator(".chart-host canvas").first).to_be_visible()
    assert page.locator(".chart-host canvas").first.evaluate("el => el.width > 0 && el.height > 0")
    expect(page.locator(".quality-strip")).to_contain_text(f"数据点 {len(rows)}")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    cache = payload.get("cache") or {}
    assert cache.get("history_error_code") not in ("UNSUPPORTED_ARCHIVE_SYMBOL", "UNSUPPORTED_ARCHIVE_MARKET")
    return {"width": width, "timeframe": timeframe, "http_status": 200, "bar_count": len(rows),
            "canvas_visible": True, "overflow": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000)}


def verify_progress_rows(page, dialog, payload, symbol, instrument):
    expect(dialog.locator(".annual-history-list article")).to_have_count(len(payload["items"]))
    summaries = {}
    for row in payload["items"]:
        if row["symbol"] != symbol or str(row["instrument_id"]) != instrument:
            continue
        timeframe = row["timeframe"]
        assert timeframe in TIMEFRAMES
        card = dialog.get_by_role("article", name=f"{symbol} {timeframe} 同步状态", exact=True)
        expect(card).to_be_visible()
        assert row["history_status"] in HISTORY_STATES and row["state"] in REPLAY_STATES
        assert row.get("history_error_code") not in ("UNSUPPORTED_ARCHIVE_SYMBOL", "UNSUPPORTED_ARCHIVE_MARKET")
        expect(card).not_to_contain_text("历史下载不支持")
        assert row.get("range_policy") in ("LISTING_CLIPPED", "ROLLING_YEAR")
        if symbol == "龙虾_USDT":
            assert row["range_policy"] == "LISTING_CLIPPED" and row.get("listing_time"), "LISTING_RANGE_NOT_CLIPPED"
        if row["range_policy"] == "LISTING_CLIPPED":
            expect(card).to_contain_text("自上市起")
            expect(card).to_contain_text("上市前不计缺失")
        else:
            expect(card).to_contain_text("过去一年")
        expect(card).to_contain_text("实时行情 ·")
        expect(card).to_contain_text("样本回放 ·")
        expect(card).to_contain_text(f"{row['cached_bars']:,} / {row['target_bars']:,} 根")
        progress = card.get_by_role("progressbar", name=f"{symbol} {timeframe} 有效历史覆盖率", exact=True)
        expect(progress).to_have_attribute("value", str(min(row["cached_bars"], row["target_bars"])))
        expect(progress).to_have_attribute("max", str(max(1, row["target_bars"])))
        assert row["target_bars"] > 0 and 0 <= row["cached_bars"] <= row["target_bars"]
        assert 0 <= row["coverage_ratio"] <= 1
        if row.get("missing_bars") is not None:
            assert row["missing_bars"] == row["target_bars"] - row["cached_bars"]
        if row.get("total"):
            expect(card).to_contain_text(f"{row.get('cursor') or 0:,} / {row['total']:,}")
        if row["state"] == "RUNNING":
            expect(card.get_by_role("progressbar", name=f"{symbol} {timeframe} 回放进度", exact=True)).to_have_count(1)
        if row["coverage_ratio"] < 1 and row["state"] == "READY":
            expect(card).to_contain_text("已回放现有缓存；新增历史将在后台补充回放")
        summaries[timeframe] = {
            "timeframe": timeframe, "range_policy": row["range_policy"],
            "cached_bars": row["cached_bars"], "target_bars": row["target_bars"],
            "missing_bars": row.get("missing_bars"), "history_status": row["history_status"],
            "replay_state": row["state"], "replay_cursor": row.get("cursor"), "replay_total": row.get("total"),
            "download_live_replay_separated": True,
            "permanent_symbol_error": False,
        }
    return summaries


def verify_history(page, evidence, width, symbol, instrument):
    opened_at = time.perf_counter()
    with page.expect_response(lambda response: response_is(response, PROGRESS, offset=0, limit=24)) as opened:
        page.get_by_role("button", name="自选历史同步进度", exact=True).click()
    initial_response_ms = round((time.perf_counter() - opened_at) * 1000)
    dialog = page.get_by_role("dialog", name="自选历史 · 同步与回放", exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("关闭页面不影响下载和回放")
    payload = opened.value.json()
    seen = {}
    offset = 0
    # 24 rows/page, at most 12 pages per viewport plus the refresh, keeping
    # both desktop/mobile observations below the existing 30/min limiter.
    # A larger list should be checked in a separate targeted acceptance run.
    for _ in range(12):
        seen.update(verify_progress_rows(page, dialog, payload, symbol, instrument))
        if set(seen) == set(TIMEFRAMES):
            break
        if offset + len(payload["items"]) >= payload["total"]:
            raise AssertionError("WATCHED_PERIODS_MISSING_FROM_PROGRESS")
        offset += 24
        with page.expect_response(lambda response, offset=offset: response_is(response, PROGRESS, offset=offset, limit=24)) as next_page:
            dialog.get_by_role("button", name="自选历史同步下一页", exact=True).click()
        payload = next_page.value.json()
    assert set(seen) == set(TIMEFRAMES), "PROGRESS_PAGINATION_BUDGET_EXCEEDED"
    screenshot(page, evidence, f"{width}-history-progress")
    started = time.perf_counter()
    # Wait for the actual next background response, not a manual refresh. A
    # stationary cursor is valid while another history job is being scheduled.
    with page.expect_response(lambda response: response_is(response, PROGRESS, offset=offset, limit=24), timeout=20000) as refreshed:
        page.wait_for_timeout(10500)
    updated = refreshed.value.json()
    seen.update(verify_progress_rows(page, dialog, updated, symbol, instrument))
    screenshot(page, evidence, f"{width}-history-auto-refresh")
    dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".chart-host canvas").first).to_be_visible()
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    return {"width": width, "rows": [seen[period] for period in TIMEFRAMES], "period_count": 8,
            "initial_response_ms": initial_response_ms,
            "automatic_refresh": "passed", "refresh_observation_ms": round((time.perf_counter() - started) * 1000),
            "dialog_close_returns_to_chart": True, "overflow": False,
            "partial_history_allowed": True, "pages_checked": offset // 24 + 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="龙虾_USDT", help="Exact exchange identifier already in the owner's watchlist")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-watchlist-history-browser-"))
    evidence.chmod(0o700)
    report = {"kind": "READ_ONLY_WATCHLIST_HISTORY_BROWSER", "periods": [], "history": [],
              "errors": [], "api_failures": [], "blocked_requests": [], "api_response_count": 0}
    authenticated = False
    step = "configuration"
    try:
        symbol = args.symbol
        assert 1 <= len(symbol) <= 80 and all(char == "_" or char.isalnum() for char in symbol)
        assert len(symbol.encode("utf-8")) <= 240
        env = read_env(SECRET_DIR / "formal-runtime.env")
        origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.username
                or parsed_origin.password or parsed_origin.path or parsed_origin.query or parsed_origin.fragment):
            raise ValueError("HTTPS_ORIGIN_CONFIGURATION_INVALID")
        owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = env[key]
        enrollment = recover_active_enrollment(owner["username"])
        with sync_playwright() as pw:
            step = "browser_launch"
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
            context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN", service_workers="block")

            def fence(route):
                request, parsed = route.request, urlsplit(route.request.url)
                same_origin = (parsed.scheme, parsed.netloc) == (parsed_origin.scheme, parsed_origin.netloc)
                allowed = request.method in ("GET", "HEAD", "OPTIONS") or (
                    request.method == "POST" and parsed.path in AUTH_POSTS | {PREVIEW})
                if not same_origin or not allowed:
                    report["blocked_requests"].append({"method": request.method,
                        "kind": endpoint_kind(parsed.path) if same_origin else "EXTERNAL_ORIGIN_BLOCKED"})
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", fence)
            page = context.new_page()
            page.set_default_timeout(30000)
            expect.set_options(timeout=30000)
            page.on("pageerror", lambda _: report["errors"].append({"type": "JAVASCRIPT_ERROR"}))

            def response_finished(response):
                path = urlsplit(response.url).path
                if authenticated and path.startswith("/api/"):
                    report["api_response_count"] += 1
                    if response.status >= 400:
                        report["api_failures"].append({"kind": endpoint_kind(path), "status": response.status})

            page.on("response", response_finished)
            try:
                step = "encrypted_login"
                page.goto(origin + "/login", wait_until="domcontentloaded")
                page.locator('select[name="tenant"]').select_option("default")
                page.locator('input[name="username"]').fill(owner["username"])
                page.locator('input[name="password"]').fill(owner["password"])
                page.get_by_role("button", name="安全登录", exact=True).click()
                if enrollment:
                    step = "existing_mfa_verify"
                    code, _ = totp_code(enrollment["secret"])
                    page.locator(".mfa-code-inline").fill(code)
                    page.get_by_role("button", name="验证并进入系统", exact=True).click()
                expect(page.locator(".topbar")).to_be_visible()
                authenticated = True
                if urlsplit(page.url).path != "/markets/chart":
                    page.goto(origin + "/markets/chart", wait_until="domcontentloaded")
                step = "select_existing_watch"
                instrument = select_existing_watch(page, symbol)
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 960})
                    for timeframe in TIMEFRAMES:
                        step = f"chart_{width}_{timeframe}"
                        report["periods"].append(verify_period(page, instrument, timeframe, width))
                    screenshot(page, evidence, f"{width}-chart-all-periods")
                    step = f"history_dialog_{width}"
                    report["history"].append(verify_history(page, evidence, width, symbol, instrument))
                    print(f"READONLY {width}: all 8 chart periods and automatic history progress checked", flush=True)
            except Exception:
                if authenticated and not page.locator('input[name="password"], .mfa-code-inline').count():
                    try:
                        page.screenshot(path=str(evidence / "failed-page.png"), full_page=True,
                                        mask=[page.locator(".profile-menu")])
                    except Exception:
                        pass
                raise
            finally:
                if authenticated or any(cookie["name"] == "gatescope_session" for cookie in context.cookies()):
                    try:
                        status = page.evaluate("""async () => {
                          const csrf = document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith('gatescope_csrf='))?.slice('gatescope_csrf='.length);
                          const response = await fetch('/auth/logout', {method:'POST',credentials:'same-origin',headers:csrf ? {'X-CSRF-Token':csrf} : {}});
                          return response.status;
                        }""")
                        report["logout_status"] = status
                        if status != 200:
                            report["errors"].append({"type": "LOGOUT_NOT_CONFIRMED"})
                    except Exception:
                        report["errors"].append({"type": "LOGOUT_NOT_CONFIRMED"})
                context.close()
                browser.close()
    except Exception as error:
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__)
                 if Path(frame.filename).name == Path(__file__).name]
        report["errors"].append({"step": step, "type": type(error).__name__, "script_lines": lines})
        print(f"READONLY failed: {type(error).__name__}, script lines {lines}; private details suppressed", flush=True)
    finally:
        (evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Private read-only browser evidence:", evidence, flush=True)
    return int(bool(report["errors"] or report["api_failures"] or report["blocked_requests"]))


if __name__ == "__main__":
    raise SystemExit(main())
