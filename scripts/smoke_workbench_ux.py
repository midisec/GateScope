"""Built-asset browser regression. HTTP is intercepted; no production writes.

SMOKE_SAVED_REPORTS=1 reads existing owner reports to test real-data rendering.
"""
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

from gatescope.quant.backtest import CATALOG, ENGINE_VERSION, StrategySpec, run_backtest
from gatescope.quant.research_rules import rule_catalog
from tests.unit.test_research_backtest import make_bars


def reports():
    result = []
    for number, strategy in enumerate(("trend", "breakout"), 1):
        report = run_backtest(make_bars(800), StrategySpec(strategy=strategy))
        result.append({"id": f"00000000-0000-0000-0000-{number:012}", "symbol": "BTC_USDT",
            "instrument_id": "00000000-0000-0000-0000-000000000099", "timeframe": "1h",
            "state": "SUCCEEDED", "error": None, "spec": report["spec"], "bar_limit": 800,
            "from_time": None, "to_time": None, "created_at": "2024-03-01T00:00:00Z", "report": report})
    if os.environ.get("SMOKE_SAVED_REPORTS") == "1":
        import psycopg
        from scripts.prepare_service_envs import read_env
        from gatescope.adapters.postgres import PostgresPool
        from gatescope.adapters.postgres_backtests import BacktestRepository
        values = read_env(Path("data/runtime-secrets/formal-runtime.env"))
        with psycopg.connect(values["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
            owners = conn.execute("SELECT m.tenant_id,u.id FROM users u JOIN memberships m ON m.user_id=u.id WHERE u.normalized_username='owner' AND m.role='OWNER' AND m.status='ACTIVE'").fetchall()
        assert len(owners) == 1
        pool = PostgresPool(values["GATESCOPE_DATABASE_URL"], max_size=1)
        pool.open()
        try:
            repository = BacktestRepository(pool)
            saved = [repository.get(*owners[0], r["id"]) for r in repository.list(*owners[0]) if r["state"] == "SUCCEEDED"]
            chosen = [next(r for r in saved if r["report"]["full"]["metrics"]["trade_count"]), next(r for r in saved if not r["report"]["full"]["metrics"]["trade_count"])]
            result = json.loads(json.dumps(chosen, default=str))
        finally:
            pool.close()
    for run in result:
        run["metrics"] = run["report"]["full"]["metrics"]
        run["bar_count"] = run["report"]["bar_count"]
    return result


def main():
    expect.set_options(timeout=12000)
    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    run, zero = reports()
    report_title = f"{run['symbol']} 回测报告"
    failed = {**zero, "id": "00000000-0000-0000-0000-000000000003", "state": "FAILED", "error": "所选历史 K 线不连续，请缩短区间或补齐行情", "report": None, "metrics": None}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        try:
            for width in map(int, os.environ.get("SMOKE_WIDTHS", "1440,390").split(",")):
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.set_default_timeout(12000)
                errors = []
                page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))
                state = {"new": None, "polls": 0, "detail_fail": False, "submit_fail": False, "alert_fail": True, "rules": [], "submitted_spec": None}

                def handle(route, request, state=state):
                    path = urlsplit(request.url).path
                    if path == "/auth/me":
                        payload = {"user": {"id": "test", "tenant_id": "test", "username": "test", "role": "OWNER"}, "mfa_required": False}
                    elif path == "/api/v2/research/strategies":
                        payload = {"items": CATALOG, "rules": rule_catalog(), "defaults": StrategySpec(availability_mode="HISTORICAL_CLOSE").model_dump(), "engine_version": ENGINE_VERSION}
                    elif path == "/api/v2/research/coverage":
                        payload = {"available_bars": 2399, "delayed_bars": 2321, "limit": 5000}
                    elif path == "/api/v2/watchlists":
                        payload = {"items": [{"id": "watch", "name": "自选", "items": [{"instrument_id": run["instrument_id"], "symbol": "BTC_USDT"}]}]}
                    elif path == "/api/v2/instruments":
                        assert "limit=500" not in request.url
                        payload = {"items": [{"id": run["instrument_id"], "symbol": "BTC_USDT", "market": "futures_usdt", "status": "ACTIVE"}]}
                    elif path == "/api/v2/research/backtests":
                        if request.method == "POST":
                            state["submitted_spec"] = request.post_data_json["spec"]
                            if state["submit_fail"]:
                                state["submit_fail"] = False
                                route.fulfill(status=503, json={"detail": "临时提交失败，请重试"})
                                return
                            state["new"] = {**run, "id": "00000000-0000-0000-0000-000000000004", "state": "QUEUED", "report": None}
                            route.fulfill(status=202, json=state["new"])
                            return
                        state["polls"] += 1
                        if state["new"] and state["polls"] >= 3:
                            state["new"] = {**run, "id": state["new"]["id"]}
                        items = ([state["new"]] if state["new"] else []) + [run, zero, failed]
                        payload = {"items": [{k: v for k, v in r.items() if k != "report"} for r in items]}
                    elif path.startswith("/api/v2/research/backtests/"):
                        if state["detail_fail"]:
                            route.fulfill(status=503, json={"detail": "报告读取暂时失败"})
                            return
                        payload = next(r for r in [run, zero, failed, state["new"]] if r and r["id"] == path.split("/")[-1])
                    elif path == "/api/v2/alert-rules":
                        if request.method == "POST":
                            if state["alert_fail"]:
                                state["alert_fail"] = False
                                route.fulfill(status=409, json={"error": {"message": "规则名称冲突或标的不可用"}})
                                return
                            data = request.post_data_json
                            created = {"id": "rule-1", "name": data["name"], "status": "ACTIVE", "etag": 1, "current_version": 1, "version": {"condition": data["condition"], "routing": data["routing"]}}
                            state["rules"].append(created)
                            route.fulfill(status=201, json=created)
                            return
                        payload = {"items": state["rules"]}
                    elif path == "/api/v2/patterns/catalog":
                        payload = {"items": [{"pattern_id": "double_bottom", "name": "双底", "group": "反转", "direction": "bullish", "experimental": False}]}
                    elif path.startswith("/api/"):
                        payload = {"items": [], "attention_count": 0}
                    else:
                        file = dist / path.lstrip("/") if path.startswith("/assets/") else dist / "index.html"
                        route.fulfill(status=200, content_type=mimetypes.guess_type(file.name)[0] or "text/plain", body=file.read_bytes())
                        return
                    route.fulfill(status=200, json=payload)

                page.route("http://research.test/**", handle)
                page.goto("http://research.test/strategies/backtests")
                expect(page.get_by_role("heading", name=report_title)).to_be_visible()
                assert f"run={run['id']}" in page.url
                assert page.locator(".research-result-heading").bounding_box()["y"] < 360
                page.screenshot(path=f"/tmp/gatescope-research-{width}.png", full_page=True)
                page.reload()
                expect(page.get_by_role("heading", name=report_title)).to_be_visible()
                page.get_by_role("button", name="回撤曲线", exact=True).click()
                expect(page.get_by_label("回测回撤曲线")).to_be_visible()
                expect(page.get_by_text("首次入场", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="历史胜率与概率区间", exact=True)).to_be_visible()
                expect(page.get_by_text("最大峰谷回撤", exact=True)).to_be_visible()
                page.get_by_role("button", name="定位最大回撤", exact=True).click()
                page.screenshot(path=f"/tmp/gatescope-drawdown-{width}.png", full_page=True)
                page.get_by_role("button", name="显示全部", exact=True).click()
                page.get_by_role("button", name="逐笔交易 ·").click()
                expect(page.get_by_role("columnheader", name="入场时间", exact=True)).to_be_visible()
                page.get_by_role("button", name="查看图表", exact=True).first.click()
                expect(page.get_by_role("dialog", name="历史入场记录")).to_be_visible()
                expect(page.get_by_role("dialog").get_by_text("入场时间", exact=True)).to_be_visible()
                expect(page.get_by_role("dialog").get_by_text("退出记账时间", exact=True)).to_be_visible()
                assert page.locator("dialog canvas").count() >= 2
                page.keyboard.press("Escape")
                expect(page.get_by_role("dialog")).to_have_count(0)
                with page.expect_download() as download:
                    page.get_by_role("button", name="导出 CSV", exact=True).click()
                assert download.value.suggested_filename.endswith(".csv")
                page.get_by_role("button", name="历史记录 ·").click()
                page.locator(".research-history-list > button").nth(1).click()
                expect(page.get_by_text("本次回测没有成交", exact=True)).to_be_visible()
                page.get_by_role("button", name="历史记录 ·").click()
                page.locator(".research-history-list > button").nth(2).click()
                expect(page.get_by_role("heading", name="这次回测未完成")).to_be_visible()
                state["detail_fail"] = True
                page.goto(f"http://research.test/strategies/backtests?run={run['id']}")
                expect(page.get_by_role("heading", name="报告加载失败")).to_be_visible()
                state["detail_fail"] = False
                page.get_by_role("button", name="重新加载报告", exact=True).click()
                expect(page.get_by_role("heading", name=report_title)).to_be_visible()
                page.get_by_role("button", name="载入参数", exact=True).click()
                expect(page.get_by_label("最多 K 线")).to_have_value(str(run["bar_limit"]))
                expect(page.get_by_label("数据回放模式")).to_have_value("AS_RECORDED")
                page.get_by_label("数据回放模式").select_option("HISTORICAL_CLOSE")
                page.get_by_role("button", name="多选组合", exact=True).click()
                page.get_by_label("搜索策略或形态").fill("双底")
                page.get_by_role("checkbox", name="双底", exact=False).check()
                expect(page.get_by_role("button", name="移除 双底", exact=True)).to_be_visible()
                page.get_by_label("搜索策略或形态").fill("旗形")
                page.get_by_role("checkbox", name="看涨旗形", exact=False).check()
                expect(page.get_by_role("button", name="移除 双底", exact=True)).to_be_visible()
                page.get_by_label("触发关系").select_option("ALL")
                page.get_by_role("button", name="单选", exact=True).click()
                page.get_by_label("搜索策略或形态").fill("双底")
                page.get_by_role("radio", name="双底", exact=False).check()
                page.get_by_role("button", name="多选组合", exact=True).click()
                page.get_by_label("搜索策略或形态").fill("旗形")
                page.get_by_role("checkbox", name="看涨旗形", exact=False).check()
                page.get_by_label("触发关系").select_option("ANY")
                expect(page.get_by_text("当前可用 2399 根", exact=False)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=f"/tmp/gatescope-composite-picker-{width}.png", full_page=True)
                state["submit_fail"] = True
                page.get_by_role("button", name="开始回测", exact=True).click()
                expect(page.get_by_text("临时提交失败，请重试", exact=True)).to_be_visible()
                state["polls"] = 0
                page.get_by_role("button", name="开始回测", exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                assert state["submitted_spec"]["rule_ids"] == ["pattern:double_bottom", "pattern:bull_flag"]
                assert state["submitted_spec"]["match_mode"] == "ANY"
                assert state["submitted_spec"]["availability_mode"] == "HISTORICAL_CLOSE"
                expect(page.get_by_text("排队中 ·", exact=False)).to_be_visible()
                expect(page.get_by_role("heading", name=report_title)).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.goto("http://research.test/strategies/alerts")
                page.get_by_label("策略名称").fill("浏览器创建测试")
                page.get_by_role("button", name="搜索并选择币种").click()
                expect(page.get_by_text("我的自选 · 输入可搜索全部币种")).to_be_visible()
                page.screenshot(path=f"/tmp/gatescope-alert-picker-{width}.png", full_page=True)
                page.get_by_role("combobox", name="搜索币种").fill("BTC")
                page.get_by_role("option", name="BTC_USDT").click()
                page.get_by_role("button", name="启用策略", exact=True).click()
                expect(page.get_by_text("规则名称冲突或标的不可用", exact=True)).to_be_visible()
                expect(page.get_by_label("策略名称")).to_have_value("浏览器创建测试")
                page.get_by_role("button", name="启用策略", exact=True).click()
                expect(page.get_by_text("「浏览器创建测试」已创建并启用，可在已保存策略中查看。", exact=True)).to_be_visible()
                expect(page.locator(".strategy-card h3")).to_have_text("浏览器创建测试")
                expect(page.locator(".instrument-trigger")).to_contain_text("BTC_USDT")
                page.reload()
                expect(page.locator(".strategy-card h3")).to_have_text("浏览器创建测试")
                assert not errors, errors
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                print(json.dumps({"width": width, "report_default_reload_retry": "ok", "charts_zero_failed_jobs": "ok", "queue_submit_retry": "ok", "alerts_search_create_retry_reload": "ok", "saved_report_source": os.environ.get("SMOKE_SAVED_REPORTS") == "1"}))
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
