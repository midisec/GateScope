"""Built UI regression: no external requests, orders, or production writes."""

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

DIST = Path(os.environ.get("SMOKE_DIST", "web/dist")).resolve()
INSTRUMENT = "30000000-0000-0000-0000-000000000033"
STEPS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400}


def snapshot(period):
    now = datetime.now(timezone.utc)
    step = STEPS[period]
    opened = datetime.fromtimestamp(int(now.timestamp()) // step * step, timezone.utc)
    price = {"15m": 101, "30m": 303, "1h": 202, "4h": 404, "8h": 808, "1d": 909}[period]
    count = 365 if period == "1d" else 1500
    final = [
        {
            "open_time": (opened - timedelta(seconds=step * (count - i))).isoformat(),
            "open": str(price),
            "high": str(price + 2),
            "low": str(price - 2),
            "close": str(price),
            "volume": "10",
            "revision": 1,
            "is_final": True,
            "quality_flags": [],
        }
        for i in range(count)
    ]
    live = {**final[-1], "open_time": opened.isoformat(), "is_final": False, "revision": 0}
    return {
        "items": final,
        "quality_flags": [],
        "data_watermark": now.isoformat(),
        "cache": {
            "active": True,
            "cached_bars": 3500,
            "missing_bars": 4500,
            "target_bars": 8000,
            "history_status": "BACKFILLING",
            "last_live_at": now.isoformat(),
            "stale": False,
            "live_bar": live,
        },
    }


def main():
    expect.set_options(timeout=12000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        try:
            for width in (1440, 390):
                page = browser.new_page(viewport={"width": width, "height": 950})
                errors, held, requests = [], [], []
                state = {"hold": "", "fail": False, "limits": []}
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))

                def handle(route, request, state=state, held=held, requests=requests):
                    path = urlsplit(request.url).path
                    if path == "/auth/me":
                        payload = {
                            "user": {"id": "test", "tenant_id": "test", "username": "test", "role": "OWNER"},
                            "mfa_required": False,
                        }
                    elif path == "/api/v2/instruments":
                        payload = {
                            "items": [
                                {
                                    "id": INSTRUMENT,
                                    "symbol": "BTC_USDT",
                                    "market": "futures_usdt",
                                    "settle": "usdt",
                                    "status": "ACTIVE",
                                }
                            ]
                        }
                    elif path == "/api/v2/watchlists":
                        payload = {
                            "items": [
                                {
                                    "id": "watch",
                                    "etag": 1,
                                    "name": "我的自选",
                                    "items": [
                                        {
                                            "instrument_id": INSTRUMENT,
                                            "symbol": "BTC_USDT",
                                            "default_timeframe": "15m",
                                        }
                                    ],
                                }
                            ]
                        }
                    elif path == "/api/v2/market/watchlist-cache":
                        payload = {
                            "items": [
                                {
                                    "instrument_id": INSTRUMENT,
                                    "timeframe": "15m",
                                    "price": "101",
                                    "stale": False,
                                }
                            ]
                        }
                    elif path == "/api/v2/bars":
                        period = parse_qs(urlsplit(request.url).query)["timeframe"][0]
                        limit = int(parse_qs(urlsplit(request.url).query)["limit"][0])
                        state["limits"].append(limit)
                        requests.append(period)
                        if state["fail"]:
                            route.fulfill(status=503, json={"detail": "行情网络暂不可用"})
                            return
                        if state["hold"] == period:
                            held.append((route, period))
                            return
                        payload = snapshot(period)
                        payload["items"] = payload["items"][-limit:]
                    elif path == "/api/v2/indicators/preview":
                        data = request.post_data_json
                        bars = snapshot(data["timeframe"])["items"]
                        assert data["to_time"] == bars[-1]["open_time"], (
                            "forming candle leaked into indicator request"
                        )
                        payload = {"items": []}
                    elif path.startswith(("/api/", "/auth/")):
                        payload = {"items": [], "attention_count": 0}
                    else:
                        file = DIST / path.lstrip("/") if path.startswith("/assets/") else DIST / "index.html"
                        route.fulfill(
                            status=200,
                            content_type=mimetypes.guess_type(file.name)[0] or "text/plain",
                            body=file.read_bytes(),
                        )
                        return
                    route.fulfill(status=200, json=payload)

                page.route("**/*", handle)
                page.goto("http://market.test/markets/chart")
                expect(page.get_by_text("后台持续同步", exact=True)).to_be_visible()
                expect(page.get_by_text("末根未收盘 · 指标仅按收盘计算", exact=False)).to_be_visible()
                expect(page.locator(".market-quote > strong")).to_contain_text("101")
                page.wait_for_timeout(5500)
                assert 8 in state["limits"], "steady polling must use bounded increments"
                expect(page.get_by_text("数据点 1500", exact=True)).to_be_visible()
                # A real reload discards React memory. Hold the API to prove the
                # persisted preview paints without waiting for a market response.
                state["hold"] = "15m"
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_text("数据点 480", exact=True)).to_be_visible()
                expect(page.locator(".market-quote > strong")).to_contain_text("101")
                expect(page.get_by_text("后台持续同步", exact=True)).to_have_count(0)
                for route, period in held:
                    route.fulfill(status=200, json=snapshot(period))
                held.clear()
                state["hold"] = ""
                expect(page.get_by_text("数据点 1500", exact=True)).to_be_visible()
                state["hold"] = "1h"
                page.get_by_role("button", name="1h", exact=True).click()
                page.wait_for_timeout(5500)
                assert requests.count("1h") == 1, "overlapping chart requests"
                page.get_by_role("button", name="4h", exact=True).click()
                expect(page.locator(".market-quote > strong")).to_contain_text("404")
                for route, period in held:
                    route.fulfill(status=200, json=snapshot(period))
                held.clear()
                expect(page.locator(".market-quote > strong")).to_contain_text("404")
                state["hold"] = ""
                page.get_by_role("button", name="30m", exact=True).click()
                expect(page.locator(".market-quote > strong")).to_contain_text("303")
                page.get_by_role("button", name="8h", exact=True).click()
                expect(page.locator(".market-quote > strong")).to_contain_text("808")
                state["hold"] = "4h"
                page.get_by_role("button", name="4h", exact=True).click()
                expect(page.locator(".market-quote > strong")).to_contain_text("404")
                for route, period in held:
                    route.fulfill(status=200, json=snapshot(period))
                held.clear()
                state["hold"] = ""
                expect(page.get_by_role("button", name="刷新状态", exact=True)).to_be_enabled()
                state["fail"] = True
                page.get_by_role("button", name="刷新状态", exact=True).click()
                expect(page.get_by_text("行情延迟 · 显示缓存", exact=True)).to_be_visible()
                expect(page.locator(".market-quote > strong")).to_contain_text("404")
                state["fail"] = False
                page.evaluate("window.dispatchEvent(new Event('online'))")
                expect(page.get_by_text("后台持续同步", exact=True)).to_be_visible()
                state["limits"].clear()
                page.get_by_role("button", name="1d", exact=True).click()
                expect(page.locator(".market-quote > strong")).to_contain_text("909")
                page.wait_for_timeout(11000)
                assert state["limits"].count(1500) == 1, (
                    "short daily history must not trigger full download every poll"
                )
                assert state["limits"].count(8) >= 1
                menu = page.locator(".profile-menu")
                menu.locator("summary").click()
                page.keyboard.press("Escape")
                expect(menu).not_to_have_attribute("open", "")
                menu.locator("summary").click()
                page.locator(".topbar .brand").click()
                expect(menu).not_to_have_attribute("open", "")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                price_box = page.locator(".watch-price").first.bounding_box()
                remove_box = page.get_by_role("button", name="将 BTC_USDT 移出自选").bounding_box()
                assert price_box["x"] + price_box["width"] <= remove_box["x"] - 1
                assert not errors, errors
                page.screenshot(path=f"/tmp/gatescope-market-cache-{width}.png", full_page=True)
                print(
                    json.dumps(
                        {
                            "width": width,
                            "reload_preview_before_network": "ok",
                            "single_flight": "ok",
                            "period_race": "ok",
                            "cached_switch": "ok",
                            "offline_recovery": "ok",
                            "forming_isolation": "ok",
                        }
                    ),
                    flush=True,
                )
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
