"""Fault regressions against the real built App; every browser request is mocked.

Run from the project root with the existing Playwright environment:
    python -m scripts.smoke_event_repairs_browser
Optional: SMOKE_DIST=/path/to/web/dist BROWSER_EXECUTABLE=/path/to/chrome

Uses synthetic fixtures from smoke_all_modules, never its live/report helpers.
No HTTP server, production session, real API call, or browser download is needed.
Desktop 1440px and mobile 390px each exercise selection races, the real 2-second
indicator retry, and manual recovery after the bounded automatic retry budget.
Screenshots contain fixture data only and are written to a temporary directory.
"""
import json
import mimetypes
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from scripts.smoke_all_modules import INSTRUMENT, fixtures

ORIGIN = "http://event-repairs.test"
ROOT = Path(__file__).resolve().parents[1]
DIST = Path(os.environ.get("SMOKE_DIST", ROOT / "web" / "dist")).resolve()
ETH = "00000000-0000-0000-0000-000000000098"
PREVIEW = "/api/v2/indicators/preview"
TIMEOUT = 10000


def wait_until(page, condition, label, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout / 1000
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out: {label}")
        # Playwright must pump its dispatcher to process deferred route events.
        page.wait_for_timeout(25)


class MarketFixture:
    def __init__(self, scenario):
        source = fixtures()
        self.instruments = {
            INSTRUMENT: source["instrument"],
            ETH: {**source["instrument"], "id": ETH, "symbol": "ETH_USDT", "display_name": "ETH / USDT"},
        }
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.bars = {}
        for identifier, price in ((INSTRUMENT, 100), (ETH, 200)):
            self.bars[identifier] = [{
                **bar,
                "open_time": (start + timedelta(hours=i)).isoformat(),
                "close_time": (start + timedelta(hours=i + 1)).isoformat(),
                "available_at": (start + timedelta(hours=i + 1)).isoformat(),
                "open": str(price), "high": str(price + 5), "low": str(price - 5),
                "close": str(price + i % 4), "revision": 1, "is_final": True,
            } for i, bar in enumerate(source["bars"])]
        self.scenario = scenario
        self.hold_btc = False
        self.held = []
        self.previews = []
        self.bar_requests = []
        self.errors = []
        self.preview_failure = "first" if scenario == "automatic" else "always" if scenario == "manual" else "none"

    def market_page(self, identifier, limit):
        bars = self.bars[identifier]
        return {"items": bars[-limit:], "quality_flags": [], "data_watermark": bars[-1]["available_at"], "cache": {
            "active": True, "cached_bars": len(bars), "target_bars": len(bars), "missing_bars": 0,
            "history_status": "READY", "last_live_at": bars[-1]["available_at"], "stale": False,
        }}

    def handle(self, route, request):
        url = urlsplit(request.url)
        path, query = url.path, parse_qs(url.query)
        # Fail closed. There is deliberately no route.continue_() or route.fetch().
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            self.errors.append(f"Unexpected external request: {request.method} {url.netloc}{path}")
            route.abort()
            return
        if request.method not in ("GET", "HEAD") and not (path == PREVIEW and request.method == "POST"):
            self.errors.append(f"Unexpected write: {request.method} {path}")
            route.abort()
            return
        if path == "/auth/me":
            payload = {"user": {"id": "event-fixture", "tenant_id": "event-fixture", "username": "tester", "role": "OWNER"},
                       "mfa_required": False, "auth_strength": "MFA"}
        elif path == "/api/v2/instruments":
            needle = query.get("q", [""])[0].upper()
            if self.hold_btc and needle == "BTC_USDT":
                self.held.append(route)
                return
            payload = {"items": [item for item in self.instruments.values() if needle in item["symbol"]], "has_more": False}
        elif path == "/api/v2/watchlists":
            payload = {"items": [{"id": "fixture-watch", "name": "我的自选", "etag": 1, "items": [
                {"instrument_id": key, "symbol": item["symbol"], "default_timeframe": "1h"}
                for key, item in self.instruments.items()
            ]}]}
        elif path == "/api/v2/chart-layouts":
            payload = {"items": [{"id": "fixture-layout", "name": "默认工作台", "etag": 1,
                                  "spec": {"instrument_id": ETH, "symbol": "ETH_USDT", "timeframe": "1h"}}]}
        elif path == "/api/v2/market/watchlist-cache":
            payload = {"items": []}
        elif path == "/api/v2/notifications":
            payload = {"items": [], "attention_count": 0}
        elif path in ("/api/v2/indicators/catalog", "/api/v2/indicator-templates"):
            payload = {"items": []}
        elif path == "/api/v2/patterns/current":
            payload = {"items": [], "catalog": [], "as_of": None, "bars_scanned": 80, "version": "fixture",
                       "total": 0, "tracked_total": 0, "offset": 0, "limit": 12, "phase_counts": {}, "tracking_status": "READY"}
        elif path == "/api/v2/bars":
            identifier = query["instrument_id"][0]
            self.bar_requests.append(identifier)
            payload = self.market_page(identifier, int(query["limit"][0]))
        elif path == PREVIEW:
            data = request.post_data_json
            bars = self.bars[data["instrument_id"]]
            assert data["from_time"] == bars[0]["open_time"]
            assert data["to_time"] == bars[-1]["open_time"], "indicator request changed the frozen closing-bar boundary"
            failed = self.preview_failure == "always" or self.preview_failure == "first" and not self.previews
            self.previews.append({"at": time.monotonic(), "body": data, "status": 503 if failed else 200})
            if failed:
                route.fulfill(status=503, json={"detail": "测试：指标服务暂时不可用"})
                return
            payload = {"items": [{"instance_id": node["instance_id"], "indicator_id": node["indicator_id"],
                                   "open_time": bar["open_time"], "ready": i >= 20, "bar_revision": 1,
                                   "values": {node["indicator_id"]: 202.125 if i >= 20 else None}}
                                  for node in data["nodes"] for i, bar in enumerate(bars)]}
        elif path.startswith(("/api/", "/auth/")):
            self.errors.append(f"Unhandled fixture API: {request.method} {path}")
            route.fulfill(status=501, json={"detail": "Unhandled fixture API"})
            return
        elif path in ("/", "/markets/chart") or path.startswith("/assets/"):
            asset = (DIST / path.lstrip("/")).resolve() if path.startswith("/assets/") else DIST / "index.html"
            if not asset.is_relative_to(DIST) or not asset.is_file():
                self.errors.append(f"Missing or invalid built asset: {path}")
                route.abort()
                return
            route.fulfill(status=200, content_type=mimetypes.guess_type(asset.name)[0] or "application/octet-stream", body=asset.read_bytes())
            return
        elif path == "/favicon.ico":
            route.fulfill(status=204, body="")
            return
        else:
            self.errors.append(f"Unhandled resource: {path}")
            route.abort()
            return
        route.fulfill(status=200, json=payload)

    def assert_fixed_bars(self):
        assert self.previews
        boundaries = {(item["body"]["instrument_id"], item["body"]["timeframe"],
                       item["body"]["from_time"], item["body"]["to_time"]) for item in self.previews}
        assert len(boundaries) == 1, "indicator recovery depended on a new symbol, period, or closed candle"


def fits(page, width, stage):
    dimensions = page.evaluate("""() => ({viewport: innerWidth, document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth, dialogs: [...document.querySelectorAll('dialog[open]')].map(d => ({
            width: d.clientWidth, scroll: d.scrollWidth, left: d.getBoundingClientRect().left,
            right: d.getBoundingClientRect().right}))})""")
    assert dimensions["document"] <= dimensions["viewport"] + 1, (width, stage, dimensions)
    assert dimensions["body"] <= dimensions["viewport"] + 1, (width, stage, dimensions)
    assert all(d["scroll"] <= d["width"] + 1 and d["left"] >= -1 and d["right"] <= width + 1
               for d in dimensions["dialogs"]), (width, stage, dimensions)


def indicators_ready(page):
    expect(page.locator(".chart-indicator-legend b").first).to_have_text("202.13")
    expect(page.get_by_role("button", name="重试指标", exact=True)).to_have_count(0)
    expect(page.locator(".chart-host canvas").first).to_be_visible()


def selection_race(page, fixture, width):
    indicators_ready(page)
    fixture.hold_btc = True
    page.locator(".watch-select").filter(has_text="BTC").click()
    wait_until(page, lambda: len(fixture.held) == 1, "held BTC watchlist lookup")
    page.get_by_role("button", name="搜索并添加币种", exact=True).click()
    page.get_by_placeholder("输入 BTC、ETH、SOL…").fill("ETH")
    choice = page.locator(".coin-result-main").filter(has_text="ETH")
    expect(choice).to_be_visible()
    fits(page, width, "search-dialog-during-delayed-selection")
    choice.click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    expect(page.locator(".market-symbol-trigger strong")).to_have_text("ETH / USDT")
    fixture.hold_btc = False
    for held in fixture.held:
        held.fulfill(status=200, json={"items": [fixture.instruments[INSTRUMENT]], "has_more": False})
    fixture.held.clear()
    # Let promise continuations, React effects and a paint settle after release.
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(page.locator(".market-symbol-trigger strong")).to_have_text("ETH / USDT")
    expect(page.locator(".timeframes button.active")).to_have_text("1h")
    assert INSTRUMENT not in fixture.bar_requests, "old selection triggered a BTC chart request"
    fits(page, width, "selection-race-restored")


def automatic_retry(page, fixture, width):
    expect(page.get_by_text("指标暂未更新：", exact=False)).to_be_visible()
    fits(page, width, "automatic-retry-error")
    indicators_ready(page)
    assert len(fixture.previews) == 2, fixture.previews
    assert [item["status"] for item in fixture.previews] == [503, 200]
    elapsed = fixture.previews[1]["at"] - fixture.previews[0]["at"]
    assert 1.8 <= elapsed < 9, f"expected the real 2-second bounded retry, observed {elapsed:.3f}s"
    fixture.assert_fixed_bars()
    fits(page, width, "automatic-retry-recovered")


def manual_retry(page, fixture, width):
    expect(page.get_by_text("指标暂未更新：", exact=False)).to_be_visible()
    # Real 2s + 4s timers: stop after initial request and two automatic retries.
    expect(page.get_by_text("可手动重试", exact=False)).to_be_visible(timeout=TIMEOUT)
    assert len(fixture.previews) == 3, fixture.previews
    assert all(item["status"] == 503 for item in fixture.previews)
    fits(page, width, "manual-retry-exhausted")
    fixture.preview_failure = "none"
    button = page.get_by_role("button", name="重试指标", exact=True)
    expect(button).to_be_enabled()
    button.click()
    indicators_ready(page)
    assert len(fixture.previews) == 4 and fixture.previews[-1]["status"] == 200
    fixture.assert_fixed_bars()
    fits(page, width, "manual-retry-recovered")


def main():
    if not (DIST / "index.html").is_file():
        raise SystemExit(f"Build web assets first or set SMOKE_DIST: {DIST}")
    expect.set_options(timeout=TIMEOUT)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-event-repairs-"))
    print(f"Fixture browser evidence: {evidence}", flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"), timeout=20000)
        try:
            for width in (1440, 390):
                for name, check in (("selection", selection_race), ("automatic", automatic_retry), ("manual", manual_retry)):
                    fixture = MarketFixture(name)
                    context = browser.new_context(viewport={"width": width, "height": 950}, locale="zh-CN", service_workers="block")
                    context.route("**/*", fixture.handle)
                    context.route_web_socket("**/*", lambda ws, f=fixture: (f.errors.append("Unexpected WebSocket"), ws.close()))
                    page = context.new_page()
                    page.set_default_timeout(TIMEOUT)
                    page.set_default_navigation_timeout(TIMEOUT)
                    page.on("pageerror", lambda error, f=fixture: f.errors.append(str(error)))
                    try:
                        print(f"CHECK {width}px {name}", flush=True)
                        page.goto(ORIGIN + "/markets/chart", wait_until="domcontentloaded")
                        expect(page.get_by_role("heading", name="我的自选", exact=True)).to_be_visible()
                        expect(page.locator(".market-symbol-trigger strong")).to_have_text("ETH / USDT")
                        check(page, fixture, width)
                        assert not fixture.errors, fixture.errors
                        page.screenshot(path=str(evidence / f"{width}-{name}.png"), full_page=True)
                        print(json.dumps({"width": width, "scenario": name, "result": "PASS", "preview_requests": len(fixture.previews),
                                          "overflow": False, "all_network_mocked": True}), flush=True)
                    except Exception:
                        page.screenshot(path=str(evidence / f"{width}-{name}-failed.png"), full_page=True)
                        if fixture.errors:
                            print(json.dumps({"fixture_errors": fixture.errors}), flush=True)
                        raise
                    finally:
                        context.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
