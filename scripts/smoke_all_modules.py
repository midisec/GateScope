"""Desktop/mobile UI acceptance against built assets and strict HTTP fixtures.

No production sessions, credentials, notifications or trading endpoints are used.
Run with: uv run --with playwright python scripts/smoke_all_modules.py
"""
import base64
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import expect, sync_playwright

from scripts.smoke_workbench_ux import reports
from gatescope.quant.backtest import CATALOG, ENGINE_VERSION, StrategySpec
from gatescope.quant.research_rules import rule_catalog

ORIGIN = "http://127.0.0.1:17759"
NOW = "2026-09-01T00:00:00Z"
INSTRUMENT = "00000000-0000-0000-0000-000000000099"
DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


def fixtures():
    instrument = {"id": INSTRUMENT, "symbol": "BTC_USDT", "display_name": "BTC / USDT", "market": "futures_usdt", "settle": "USDT", "status": "ACTIVE"}
    bars = [{"open_time": (datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(minutes=i)).isoformat(), "open": "100", "high": "105", "low": "95", "close": str(100 + i % 4), "volume": "20", "quality_flags": []} for i in range(80)]
    accounts = [{"id": f"a{i}", "name": f"模拟账户 {i}", "status": "ACTIVE", "equity": "10000", "base_currency": "USDT", "realized_pnl": "0", "risk_fraction": "0.0025", "max_notional_fraction": "0.1"} for i in range(12)]
    terms = {"side": "LONG", "quantity": "2", "risk_budget": "25", "take_profit_price": "110", "stop_price": "95", "same_bar_policy": "STOP_FIRST"}
    proposals = [{"id": f"p{i}", "symbol": "BTC_USDT", "timeframe": "1h", "exact_terms": terms, "status": "AWAITING_CONFIRMATION", "version": 1, "expires_at": NOW} for i in range(12)]
    trades = [{"public_trade_id": f"PT-{i}", "symbol": "BTC_USDT", "side": "LONG", "state": "OPEN", "net_pnl": None, "entry_fill_price": "100", "last_evaluated_available_at": NOW} for i in range(12)]
    signals = [{"id": "signal-1", "symbol": "BTC_USDT", "timeframe": "1h", "side": "LONG", "evidence_tier": "PAPER_ELIGIBLE", "eligibility_status": "PAPER_ELIGIBLE", "evidence_digest": "fixture-evidence-digest", "metrics": {}, "entry_reference": "100", "take_profit_price": "110", "stop_price": "95"}]
    exchanges = [{"id": f"ex{i}", "alias": f"测试网账户 {i}", "venue": "GATE", "environment": "TESTNET", "market_type": "FUTURES_USDT", "status": "READ_VALIDATED", "credential_version": 1, "trading_ready": False, "capabilities": {}, "warning_codes": [], "etag": 1} for i in range(12)]
    connections = [{"id": f"conn{i}", "telegram_username": f"fixture_{i}", "telegram_user_id": 100 + i, "status": "ACTIVE"} for i in range(12)]
    channels = [{"id": f"ch{i}", "alias": f"通知通道 {i}", "connection_id": f"conn{i}", "status": "ACTIVE"} for i in range(12)]
    routes = [{"id": f"route{i}", "name": f"路由 {i}", "channel_id": "ch0", "event_types": ["RESEARCH_SIGNAL", "RISK_CRITICAL"], "symbols": [], "max_per_minute": 10, "enabled": True, "etag": 1} for i in range(12)]
    clients = [{"id": f"mcp{i}", "name": f"客户端 {i}", "status": "ACTIVE", "tokens": []} for i in range(12)]
    events = [{"id": f"n{i}", "event_type": "SYSTEM", "channel_alias": f"通道 {i}", "status": "FAILED" if i < 5 else "SENT", "created_at": NOW, "updated_at": NOW, "telegram_message_id": 1000 + i, "safe_error_code": None} for i in range(45)]
    test = {"id": "cg-test", "symbol": "BTC", "status": "SUCCEEDED", "result": {"checks": {name: {"ok": True, "http_status": 200, "business_validation": "PASSED", "elapsed_ms": 12} for name in ("coins_markets", "lsr", "account")}}}
    bot = {"configured": True, "source": "WEB_ENCRYPTED", "status": "ACTIVE", "bot_username": "fixture_bot", "allowed_user_ids": [100], "token_configured": True, "token_fingerprint": "fixture", "secret_write_allowed": True, "public_https": True, "wrapping_configured": True}
    config = {"configured": True, "status": "ACTIVE", "origin": "https://vip2.coinglass.site", "key_fingerprint": "fixture", "secret_write_allowed": True, "browser_submission_configured": True, "capability": {}, "last_tested_at": NOW, "latest_test": test}
    return locals()


def main():
    expect.set_options(timeout=10000)
    run = reports()[0]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    spki = base64.urlsafe_b64encode(key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        try:
            for width in map(int, os.environ.get("SMOKE_WIDTHS", "1440,390").split(",")):
                f = fixtures()
                state = {"fail": "", "role": "OWNER", "writes": [], "rules": [], "bars": [], "layout": None, "template": None, "session_failure": False, "preview": [], "statistics": None, "stat_polls": 0}
                errors = []
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))

                def handle(route, request, f=f, state=state, errors=errors):
                    url = urlsplit(request.url)
                    path, query = url.path, parse_qs(url.query)
                    if path.startswith(("/api/", "/auth/")):
                        if state["fail"] == path:
                            route.fulfill(status=503, json={"detail": "测试：服务暂时不可用"})
                            return
                        if request.method not in ("GET", "HEAD"):
                            state["writes"].append((path, request.post_data_json if request.post_data else None))
                    if path == "/auth/me":
                        if state["session_failure"]:
                            route.fulfill(status=503, json={"detail": "测试：会话服务暂时不可用"})
                            return
                        payload = {"user": {"id": "fixture-user", "tenant_id": "fixture-tenant", "username": "tester", "role": state["role"]}, "mfa_required": False, "auth_strength": "MFA"}
                    elif path == "/api/v2/security/submission-key":
                        payload = {"algorithm": "RSA-OAEP-256+A256GCM", "key_version": "fixture-key", "public_key_spki": spki}
                    elif path == "/api/v2/instruments":
                        payload = {"items": [f["instrument"]], "has_more": False}
                    elif path == "/api/v2/watchlists":
                        payload = {"items": [{"id": "watch", "name": "我的自选", "etag": 1, "items": [{"instrument_id": INSTRUMENT, "symbol": "BTC_USDT", "default_timeframe": "15m"}]}]}
                    elif path == "/api/v2/market/watchlist-cache":
                        payload = {"items": []}
                    elif path == "/api/v2/patterns/current":
                        payload = {"items": [], "catalog": [], "bars_scanned": len(f["bars"]), "as_of": None}
                    elif path == "/api/v2/bars":
                        state["bars"].append(query["timeframe"][0])
                        payload = {"items": f["bars"], "quality_flags": [], "data_watermark": f["bars"][-1]["open_time"]}
                    elif path == "/api/v2/chart-layouts":
                        if request.method == "POST":
                            state["layout"] = {**request.post_data_json, "id": "layout", "etag": 1}
                            payload = state["layout"]
                        else:
                            payload = {"items": [state["layout"]] if state["layout"] else []}
                    elif path == "/api/v2/indicator-templates":
                        if request.method == "POST":
                            state["template"] = {**request.post_data_json, "id": "template", "etag": 1}
                            payload = state["template"]
                        else:
                            payload = {"items": [state["template"]] if state["template"] else []}
                    elif path == "/api/v2/indicators/preview":
                        data = request.post_data_json
                        state["preview"].append(data)
                        assert data["from_time"] == f["bars"][0]["open_time"]
                        assert data["to_time"] == f["bars"][-1]["open_time"]
                        payload = {"items": [{"instance_id": n["instance_id"], "indicator_id": n["indicator_id"], "open_time": b["open_time"], "ready": i >= 20, "bar_revision": 1, "values": {n["indicator_id"]: float(b["close"]) if i >= 20 else None}} for n in data["nodes"] for i, b in enumerate(f["bars"])]}
                    elif path in ("/api/v2/indicators/catalog", "/api/v2/patterns/catalog", "/api/v2/strategy-events"):
                        payload = {"items": []}
                    elif path == "/api/v2/research/strategies":
                        payload = {"items": CATALOG, "rules": rule_catalog(), "defaults": StrategySpec(availability_mode="HISTORICAL_CLOSE").model_dump(), "engine_version": ENGINE_VERSION}
                    elif path == "/api/v2/research/coverage":
                        payload = {"available_bars": 2399, "delayed_bars": 2321, "limit": 5000}
                    elif path == "/api/v2/research/backtests":
                        if request.method == "POST":
                            assert request.post_data_json["instrument_id"] == INSTRUMENT
                            assert request.post_data_json["timeframe"] == "1h"
                            state["stat_polls"] = 0
                            state["statistics"] = {**run, "id": "stat-new", "state": "QUEUED", "report": None}
                            route.fulfill(status=202, json=state["statistics"])
                            return
                        if state["statistics"]:
                            state["stat_polls"] += 1
                            if state["stat_polls"] >= 2:
                                state["statistics"] = {**run, "id": "stat-new"}
                        rows = [{**run, "id": f"r{i}", "report": None} for i in range(23)]
                        if state["statistics"]:
                            rows.insert(0, {**state["statistics"], "report": None})
                        if query.get("instrument_id"):
                            rows = [r for r in rows if r["instrument_id"] == query["instrument_id"][0] and r["timeframe"] == query["timeframe"][0]]
                        offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [50])[0])
                        payload = {"items": rows[offset:offset + limit], "total": len(rows), "has_more": offset + limit < len(rows)}
                    elif path.startswith("/api/v2/research/backtests/"):
                        payload = state["statistics"] if path.endswith("/stat-new") else {**run, "id": path.split("/")[-1]}
                    elif path == "/api/v2/alert-rules":
                        payload = {"items": state["rules"]}
                    elif path.startswith("/api/v2/alert-rules/"):
                        rule_id = path.split("/")[-1 if request.method in ("PUT", "DELETE") else -2]
                        rule = next(r for r in state["rules"] if r["id"] == rule_id)
                        assert request.headers["if-match"] == f'"{rule["etag"]}"'
                        rule["etag"] += 1
                        if request.method == "PUT":
                            body = request.post_data_json
                            assert body["condition"]["confirmed_only"] == rule["version"]["condition"]["confirmed_only"]
                            rule.update(name=body["name"], version={"condition": body["condition"], "routing": body["routing"]}, current_version=rule["current_version"] + 1)
                            payload = rule
                        elif request.method == "DELETE":
                            state["rules"].remove(rule)
                            payload = {"deleted": True, "history_preserved": True}
                        else:
                            rule["status"] = "PAUSED" if path.endswith("/pause") else "ACTIVE"
                            payload = rule
                    elif path == "/api/v2/paper-accounts":
                        if request.method == "POST":
                            f["accounts"].insert(0, {**f["accounts"][0], "id": "new-account", "name": request.post_data_json["name"]})
                            payload = f["accounts"][0]
                        else:
                            payload = {"items": f["accounts"]}
                    elif path == "/api/v2/signals":
                        payload = {"items": f["signals"]}
                    elif path.endswith("/execution-decisions"):
                        assert request.post_data_json["paper_account_id"] == "a1"
                        payload = {"id": "decision", "decision": "ABSTAIN", "runtime_gates": [{"gate_code": "FIXTURE_ABSTAIN", "result": "FAIL"}]}
                    elif path == "/api/v2/proposals":
                        payload = {"items": f["proposals"]}
                    elif path.endswith("/cancel"):
                        f["proposals"][0]["status"] = "CANCELLED"
                        payload = {"status": "CANCELLED"}
                    elif path == "/api/v2/trades":
                        payload = {"items": f["trades"]}
                    elif path.startswith("/api/v2/trades/") and path.endswith("/chart"):
                        payload = {"original_bars": f["bars"], "progress_bars": [], "evidence_boundary": NOW, "overlays": {"entry_fill": "100", "stop": "95", "take_profit": "110", "exit_fill": None}}
                    elif path.startswith("/api/v2/trades/") and path.endswith("/timeline"):
                        payload = {"items": [{"sequence": 1, "event_type": "PAPER_OPENED", "occurred_at": NOW}]}
                    elif path == "/api/v2/exchange-accounts":
                        payload = {"items": f["exchanges"], "credential_admission_configured": True}
                    elif path.startswith("/api/v2/exchange-accounts/"):
                        payload = {"accepted": True}
                    elif path in ("/api/v2/execution-certifications", "/api/v2/testnet-drills"):
                        payload = {"items": []}
                    elif path == "/api/v2/telegram/connections":
                        payload = {"items": f["connections"], "configured": True, "bot_username": "fixture_bot"}
                    elif path == "/api/v2/telegram/bot-config":
                        payload = f["bot"]
                    elif path == "/api/v2/telegram/link-tokens":
                        payload = {"deep_link": "https://t.me/fixture_bot?start=fixture-link", "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
                    elif path == "/api/v2/notification-channels":
                        payload = {"items": f["channels"]}
                    elif path.startswith("/api/v2/notification-channels/") and path.endswith("/test"):
                        payload = {"operation_id": "op"}
                    elif path == "/api/v2/operations/op":
                        payload = {"status": "SUCCEEDED"}
                    elif path == "/api/v2/notification-routes":
                        if request.method == "POST":
                            body = request.post_data_json
                            assert body["quiet_hours"] is None
                            f["routes"].insert(0, {**body, "id": "new-route", "etag": 1})
                            payload = f["routes"][0]
                        else:
                            payload = {"items": f["routes"]}
                    elif path.startswith("/api/v2/notification-routes/"):
                        if request.method == "DELETE":
                            f["routes"] = [r for r in f["routes"] if r["id"] != path.split("/")[-1]]
                            payload = {"deleted": True}
                        else:
                            assert request.headers["if-match"] == '"1"'
                            payload = request.post_data_json
                    elif path == "/api/v2/notifications":
                        rows = [e for e in f["events"] if not query.get("status") or e["status"] == query["status"][0]]
                        offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [50])[0])
                        payload = {"items": rows[offset:offset + limit], "total": len(rows), "attention_count": 5}
                    elif path == "/api/v2/mcp/clients":
                        if request.method == "POST":
                            payload = {"id": "new-client", "name": request.post_data_json["name"], "status": "ACTIVE", "tokens": []}
                            f["clients"].insert(0, payload)
                        else:
                            payload = {"items": f["clients"], "supported_scopes": ["mcp:connect", "market:read"]}
                    elif path.endswith("/tokens"):
                        payload = {"token": "fixture-once-only-token", "expires_at": NOW}
                    elif path.endswith(":revoke"):
                        payload = {"revoked": True}
                    elif path == "/api/v2/data-sources":
                        payload = {"items": [], "market_worker": {"healthy": True, "state": "HEALTHY"}, "strategy_worker": {"healthy": True, "state": "HEALTHY"}}
                    elif path == "/api/v2/data-sources/coinglass/config":
                        payload = {"config": f["config"], "test": f["test"]} if request.method == "PUT" else f["config"]
                    elif path == "/api/v2/data-sources/coinglass:test":
                        payload = {"test": f["test"]}
                    elif path == "/auth/mfa-methods":
                        payload = {"items": [{"id": "mfa", "label": "日常验证器", "status": "ACTIVE", "created_at": NOW}]}
                    elif path == "/auth/sessions":
                        payload = {"items": [{"id": f"session{i}", "current": i == 0, "last_seen_at": NOW, "auth_strength": "MFA"} for i in range(12)]}
                    elif path.startswith("/auth/sessions/"):
                        payload = {"revoked": True}
                    elif path.startswith(("/api/", "/auth/")):
                        errors.append(f"Unmocked endpoint: {request.method} {path}")
                        route.fulfill(status=501, json={"detail": "Unmocked endpoint"})
                        return
                    else:
                        file = DIST / path.lstrip("/") if path.startswith("/assets/") else DIST / "index.html"
                        route.fulfill(status=200, content_type=mimetypes.guess_type(file.name)[0] or "text/plain", body=file.read_bytes())
                        return
                    route.fulfill(status=200, json=payload)

                # Intercept ALL requests, including accidental external calls.
                page.route("**/*", handle)

                def visit(path, title, page=page, width=width):
                    print(f"CHECK {width}px {path}", flush=True)
                    page.goto(ORIGIN + path)
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()

                def fits(page=page, width=width, errors=errors):
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (width, page.url)
                    assert not errors, errors
                    page.screenshot(path=f"/tmp/gatescope-ui-{width}-{urlsplit(page.url).path.replace(chr(47), chr(45))}.png", full_page=True)

                def close_dialog(page=page):
                    page.keyboard.press("Escape")
                    expect(page.locator("dialog[open]")).to_have_count(0)
                    assert page.evaluate("document.body.style.overflow") != "hidden"

                visit("/markets/chart", "我的自选")
                with page.expect_response(lambda r: urlsplit(r.url).path == "/api/v2/bars" and parse_qs(urlsplit(r.url).query).get("timeframe") == ["1h"]):
                    page.get_by_role("button", name="1h", exact=True).click()
                page.wait_for_function("document.querySelector('.chart-host canvas') !== null")
                expect(page.locator(".chart-empty")).to_have_count(0)
                assert page.evaluate("document.querySelector('.chart-indicator-legend').getBoundingClientRect().top >= document.querySelector('.chart-interactions').getBoundingClientRect().bottom - 1"), "indicator legend overlaps chart tools"
                assert "1h" in state["bars"]
                page.locator(".chart-host").click(button="right", position={"x": 60, "y": 65})
                expect(page.get_by_role("menu")).to_be_visible()
                page.get_by_role("menuitem", name="添加水平线").click()
                expect(page.locator(".chart-interactions")).to_contain_text("1 条价格标记")
                page.get_by_role("button", name="更多图表工具").click()
                page.get_by_role("menuitem", name="切换对数价格轴", exact=True).click()
                page.get_by_role("button", name="更多图表工具").click()
                page.get_by_role("menuitem", name="清除我的水平线", exact=True).click()
                page.get_by_role("button", name="更多图表工具").click()
                page.get_by_role("menuitem", name="添加 / 设置指标", exact=True).click()
                expect(page.get_by_role("dialog", name="技术指标")).to_be_visible()
                page.get_by_label("ema20 period", exact=True).fill("30")
                page.get_by_label("ema20 period", exact=True).press("Tab")
                close_dialog()
                expect(page.locator(".chart-indicator-legend")).to_contain_text("EMA 30")
                page.get_by_role("button", name="保存", exact=True).click()
                expect(page.locator(".toast")).to_contain_text("图表与指标已保存")
                page.reload()
                expect(page.get_by_role("button", name="1h", exact=True)).to_have_class("active")
                page.locator(".market-symbol-trigger").click()
                expect(page.locator("dialog[open] input")).to_be_focused()
                close_dialog()
                expect(page.locator(".chart-indicator-legend")).to_contain_text("EMA 30")
                assert state["preview"], "indicator requests must use the chart's exact time window"
                page.get_by_role("button", name="更多图表工具").click()
                with page.expect_download() as png:
                    page.get_by_role("menuitem", name="下载图表 PNG", exact=True).click()
                assert png.value.suggested_filename.endswith(".png")
                page.get_by_role("button", name="更多图表工具").click()
                page.get_by_role("menuitem", name="历史胜率与概率", exact=True).click()
                stats = page.get_by_role("dialog", name="历史胜率与概率")
                expect(stats.get_by_role("heading", name="历史胜率与概率区间", exact=True)).to_be_visible()
                page.get_by_role("button", name="计算并保存历史统计", exact=True).click()
                expect(stats.get_by_text("任务已保存，后台", exact=False)).to_be_visible()
                expect(stats.get_by_role("heading", name="历史胜率与概率区间", exact=True)).to_be_visible(timeout=15000)
                fits()
                page.get_by_role("button", name="应用基础指标参数到图表", exact=True).click()
                expect(page.locator("dialog[open]")).to_have_count(0)
                fits()

                state["rules"] = [{"id": f"rule{i}", "symbol": "BTC_USDT", "name": f"告警 {i}", "status": "ACTIVE", "etag": 1, "current_version": 1, "version": {"condition": {"instrument_id": INSTRUMENT, "timeframe": "15m", "minimum_confidence": .7, "pattern_ids": ["double_bottom"], "match_mode": "ANY", "confirmed_only": False, "coincidence_bars": 3, "cooldown_seconds": 900}, "routing": {"channel_ids": []}}} for i in range(12)]
                visit("/strategies/alerts", "策略与概率告警")
                page.get_by_role("button", name="暂停", exact=True).first.click()
                expect(page.get_by_role("button", name="恢复运行", exact=True)).to_be_visible()
                page.get_by_role("button", name="恢复运行", exact=True).click()
                expect(page.get_by_role("button", name="恢复运行", exact=True)).to_have_count(0)
                page.get_by_role("button", name="编辑", exact=True).first.click()
                editor = page.get_by_role("dialog", name="编辑已保存策略")
                editor.get_by_label("策略名称").fill("更新后的告警")
                editor.get_by_label("周期", exact=True).select_option("4h")
                state["fail"] = "/api/v2/alert-rules/rule0"
                editor.get_by_role("button", name="保存更新", exact=True).click()
                expect(editor.get_by_text("测试：服务暂时不可用", exact=True)).to_be_visible()
                expect(editor.get_by_label("策略名称")).to_have_value("更新后的告警")
                state["fail"] = ""
                editor.get_by_role("button", name="保存更新", exact=True).click()
                expect(editor).to_have_count(0)
                page.reload()
                card = page.locator(".strategy-card").filter(has=page.get_by_role("heading", name="更新后的告警", exact=True))
                expect(card).to_contain_text("4h")
                assert state["rules"][0]["current_version"] == 2
                card.get_by_role("button", name="删除", exact=True).click()
                close_dialog()
                expect(card).to_be_visible()
                card.get_by_role("button", name="删除", exact=True).click()
                page.get_by_role("button", name="确认操作", exact=True).click()
                expect(card).to_have_count(0)
                assert len(state["rules"]) == 11
                page.get_by_role("button", name="告警策略下一页").click()
                expect(page.get_by_role("heading", name="告警 7", exact=True)).to_be_visible()
                fits()

                visit("/trading/paper", "PAPER 交易中心")
                page.get_by_role("button", name="模拟账户下一页").click()
                expect(page.locator(".paper-account strong").get_by_text("模拟账户 5", exact=True)).to_be_visible()
                page.get_by_label("提案使用账户").select_option("a1")
                page.get_by_role("button", name="生成精确提案").click()
                expect(page.locator(".toast")).to_contain_text("ABSTAIN")
                page.get_by_role("button", name="新增账户", exact=True).click()
                page.get_by_label("账户名称", exact=True).fill("新增模拟账户")
                page.get_by_role("button", name="创建模拟账户", exact=True).click()
                expect(page.locator("dialog[open]")).to_have_count(0)
                page.get_by_role("button", name="取消", exact=True).first.click()
                expect(page.get_by_role("heading", name="取消提案")).to_be_visible()
                page.get_by_role("button", name="确认操作").click()
                expect(page.locator(".toast")).to_contain_text("提案已取消")
                page.locator(".trade-list > button").first.click()
                expect(page.get_by_text("PAPER_OPENED")).to_be_visible()
                close_dialog()
                fits()

                visit("/trading/exchange-accounts", "交易所账户")
                page.get_by_role("button", name="停用", exact=True).first.click()
                expect(page.get_by_role("heading", name="停用交易所账户")).to_be_visible()
                close_dialog()  # cancelled: no credential disable request
                page.get_by_role("button", name="轮换密钥", exact=True).first.click()
                expect(page.get_by_role("dialog")).to_be_visible()
                close_dialog()
                page.get_by_label("账户别名").fill("浏览器加密测试")
                page.get_by_label("API Key", exact=True).fill("fixture-api-key-12345678")
                page.get_by_label("API Secret", exact=True).fill("fixture-api-secret-12345678")
                page.locator('input[name="acknowledge"]').check()
                page.get_by_role("button", name="加密保存并测试连接").click()
                expect(page.locator(".toast")).to_contain_text("凭据已加密提交")
                assert "encrypted_submission" in state["writes"][-1][1]
                expect(page.get_by_label("API Secret", exact=True)).to_have_value("")
                fits()

                visit("/settings/notifications", "Telegram 与通知")
                page.get_by_role("button", name="绑定 Telegram", exact=True).click()
                expect(page.locator("dialog[open] .link-qr")).to_be_visible()
                expect(page.locator("dialog[open] .link-launch")).to_have_attribute("href", "https://t.me/fixture_bot?start=fixture-link")
                close_dialog()
                for _ in range(2):
                    page.get_by_role("button", name="发送测试", exact=True).first.click()
                    expect(page.get_by_text("已送达", exact=True).first).to_be_visible()
                page.get_by_role("button", name="新建路由").click()
                page.get_by_label("路由名称").fill("新路由")
                page.locator('dialog[open] select[name="channel"]').select_option("ch0")
                page.get_by_role("button", name="保存路由").click()
                expect(page.locator("dialog[open]")).to_have_count(0)
                expect(page.get_by_text("新路由 · 启用", exact=True)).to_be_visible()
                page.get_by_role("button", name="编辑", exact=True).first.click()
                expect(page.get_by_label("路由名称")).to_have_value("新路由")
                close_dialog()
                page.get_by_role("button", name="通知路由下一页").click()
                expect(page.get_by_text("路由 5 · 启用", exact=True)).to_be_visible()
                fits()

                visit("/integrations/mcp", "Codex MCP")
                page.get_by_role("button", name="MCP 客户端下一页").click()
                expect(page.locator(".mcp-client strong").get_by_text("客户端 5", exact=True)).to_be_visible()
                page.get_by_label("客户端名称", exact=True).fill("新客户端")
                page.get_by_role("button", name="创建隔离客户端").click()
                expect(page.locator(".toast")).to_contain_text("已创建")
                page.get_by_label("敏感操作 MFA").fill("123456")
                page.get_by_role("button", name="用 MFA 签发").click()
                expect(page.get_by_text("fixture-once-only-token", exact=True)).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("dialog[open]")).to_have_count(1)
                page.get_by_label("我已安全保存").check()
                page.get_by_role("button", name="清除显示并关闭").click()
                expect(page.get_by_text("fixture-once-only-token", exact=True)).to_have_count(0)
                fits()

                visit("/integrations/data-sources", "数据源与运行状态")
                expect(page.get_by_role("heading", name="连通性检测")).to_be_visible()
                page.get_by_role("button", name="重新检测").click()
                expect(page.locator(".toast")).to_contain_text("三个接口均检测成功")
                page.reload()
                expect(page.locator(".provider-check.passed")).to_have_count(3)
                page.get_by_label("API Key", exact=True).fill("cg_" + "f" * 64)
                page.get_by_label("当前登录密码").fill("fixture-password-123456789")
                page.get_by_label("MFA 动态码", exact=True).fill("123456")
                page.get_by_role("button", name="轮换 Key 并检测").click()
                expect(page.locator(".toast")).to_contain_text("已连接")
                assert "encrypted_submission" in state["writes"][-1][1]
                fits()

                visit("/settings/account", "用户设置")
                page.get_by_role("button", name="活动会话下一页").click()
                page.get_by_role("button", name="退出此设备", exact=True).first.click()
                expect(page.get_by_role("heading", name="退出其他设备")).to_be_visible()
                close_dialog()
                fits()

                visit("/notifications/inbox", "消息通知")
                page.get_by_role("button", name="消息下一页").click()
                expect(page.locator(".notification-feed article")).to_have_count(20)
                page.get_by_role("button", name="消息下一页").click()
                expect(page.locator(".notification-feed article")).to_have_count(5)
                expect(page.get_by_role("button", name="消息下一页")).to_be_disabled()
                page.get_by_label("投递状态").select_option("FAILED")
                expect(page.get_by_role("navigation", name="消息分页")).to_contain_text("共 5 条")
                page.get_by_role("button", name="详情", exact=True).first.click()
                expect(page.get_by_role("heading", name="通知详情")).to_be_visible()
                close_dialog()
                fits()

                visit("/strategies/backtests", "BTC_USDT 回测报告")
                page.get_by_role("button", name="历史记录", exact=False).first.click()
                expect(page.locator(".research-history-list > button")).to_have_count(10)
                page.get_by_role("button", name="回测历史下一页").click()
                page.get_by_role("button", name="回测历史下一页").click()
                expect(page.locator(".research-history-list > button")).to_have_count(4)
                fits()

                # Error paths must be visible and recoverable, not mistaken for empty lists.
                for path, title, endpoint in [
                    ("/trading/paper", "PAPER 交易中心", "/api/v2/paper-accounts"),
                    ("/trading/exchange-accounts", "交易所账户", "/api/v2/exchange-accounts"),
                    ("/settings/notifications", "Telegram 与通知", "/api/v2/telegram/connections"),
                    ("/integrations/mcp", "Codex MCP", "/api/v2/mcp/clients"),
                    ("/integrations/data-sources", "数据源与运行状态", "/api/v2/data-sources"),
                    ("/settings/account", "用户设置", "/auth/sessions"),
                    ("/notifications/inbox", "消息通知", "/api/v2/notifications"),
                ]:
                    state["fail"] = endpoint
                    visit(path, title)
                    expect(page.get_by_role("alert")).to_contain_text("测试：服务暂时不可用")
                    state["fail"] = ""
                    page.get_by_role("button", name="重新加载", exact=True).click()
                    expect(page.get_by_role("alert")).to_have_count(0)
                    fits()
                state["role"] = "VIEWER"
                visit("/integrations/mcp", "当前账号无此模块权限")
                visit("/strategies/alerts", "策略与概率告警")
                expect(page.get_by_role("button", name="观察者无创建权限")).to_be_disabled()
                fits()
                visit("/trading/paper", "PAPER 交易中心")
                expect(page.get_by_role("button", name="确认冻结条款")).to_have_count(0)
                expect(page.get_by_role("button", name="新增账户", exact=True)).to_have_count(0)
                state["session_failure"] = True
                page.reload()
                expect(page.get_by_role("button", name="重新加载", exact=True)).to_be_visible()
                state["session_failure"] = False
                page.get_by_role("button", name="重新加载", exact=True).click()
                expect(page.get_by_role("heading", name="PAPER 交易中心")).to_be_visible()
                assert not errors, errors
                print(f"PASS {width}px: all modules, paging, dialogs, read failures, role boundaries, encrypted fixture submissions")
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
