"""Real HTTPS desktop/mobile UI acceptance on the deployment host.

Never prints credentials, cookies, MFA material or upstream secret URLs.
Read-only by default; --external-tests explicitly permits one marked Telegram
notification and one Coinglass connectivity check. Never changes settings or
submits backtests or orders.
"""
import argparse
import json
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import expect, sync_playwright

from prepare_service_envs import SECRET_DIR, read_env
from bootstrap_formal_owner_mfa import recover_active_enrollment
from gatescope.security_secrets import totp_code
from gatescope.quant.artifacts import report_digest


def _structure_read(response, path, **query):
    parsed = urlsplit(response.url)
    params = parse_qs(parsed.query)
    return response.request.method == "GET" and response.status == 200 and parsed.path == path and all(
        params.get(key) == [str(value)] for key, value in query.items()
    )


def _sample_text(value):
    return "—" if value is None else f"{value:,}"


def verify_structure_history(page, panel, width, evidence):
    """Check real API-backed UI without submitting replay, settings or orders."""
    checked = {"timeframe": "1h", "current_reference": "not_covered_no_terminal_candidates",
               "initial_prediction": "not_covered_no_original_prediction"}
    hourly = page.locator(".timeframes").get_by_role("button", name="1h", exact=True)
    if "active" not in (hourly.get_attribute("class") or "").split():
        with page.expect_response(lambda r: _structure_read(r, "/api/v2/patterns/current", timeframe="1h")):
            hourly.click()
    expect(hourly).to_have_class("active")
    phase_picker = panel.get_by_label("结构状态", exact=True)
    current = None
    for phase in ("CONFIRMED", "INVALIDATED", "EXPIRED"):
        with page.expect_response(lambda r, phase=phase: _structure_read(
            r, "/api/v2/patterns/current", timeframe="1h", phase=phase
        )) as response:
            phase_picker.select_option(phase)
        current = response.value.json()
        expect(panel.locator(".pattern-card")).to_have_count(len(current.get("items", [])))
        if current.get("items"):
            break
    history = current.get("history_status") if current else None
    assert history is not None, "Current structures must expose replay progress separately from tracking counts"
    checked["history_state"] = history["state"]
    checked["history_cursor"] = history.get("cursor")
    checked["history_total"] = history.get("total")
    if history.get("total"):
        expect(panel).to_contain_text(f"{_sample_text(history.get('cursor'))} / {_sample_text(history['total'])} 根")
    if history.get("through_time") is not None:
        expect(panel).to_contain_text("已回放至")
    events = current.get("items", []) if current else []
    for index, event in enumerate(events[:3]):
        confidence = panel.locator(".pattern-card").nth(index).locator(".structure-confidence")
        reference = event.get("historical_reference") or {}
        expect(confidence).to_contain_text("形成阶段的历史确认率")
        expect(confidence).not_to_contain_text("突破确认概率")
        expect(confidence).not_to_contain_text("首次判断时")
        expect(confidence).to_contain_text(re.compile(
            r"有效样本 n=" + re.escape(_sample_text(reference.get("sample_count"))) + r"(?:\s|·|$)"
        ))
        if reference.get("reference_is_live_prediction") is False:
            expect(confidence).to_contain_text("回放补充对照 · 非当时预测")
        if reference.get("reference_as_of") is not None:
            expect(confidence).to_contain_text("样本截止")
        if reference.get("sample_count") is None:
            expect(confidence).not_to_contain_text("有效样本 n=0")
        if reference.get("reference_state") == "READY":
            failures = reference["sample_count"] - reference["successes"]
            expect(confidence).to_contain_text(
                f"确认 {_sample_text(reference['successes'])} / 失败 {_sample_text(failures)}"
            )
    if events:
        checked["current_reference"] = "passed"
        checked["terminal_cards_checked"] = min(3, len(events))

    # Prefer a candidate whose original prediction differs from the supplemental
    # reference: both counts must survive the actual card -> original-view flow.
    originals = [(index, event) for index, event in enumerate(events[:3]) if event.get("initial_prediction") is not None]
    originals.sort(key=lambda item: item[1]["initial_prediction"].get("sample_count") == (
        item[1].get("historical_reference") or {}
    ).get("sample_count"))
    for index, event in originals:
        card = panel.locator(".pattern-card").nth(index)
        if not card.is_enabled():
            continue
        card.click()
        expect(page.locator("dialog, .pattern-selection").first).to_be_visible()
        dialog = page.get_by_role("dialog")
        if dialog.count():
            original_view = dialog.locator(".structure-confidence")
            expect(dialog.locator("canvas").first).to_be_visible()
        else:
            original_details = panel.locator("details").filter(has=page.get_by_text(
                f"{event['name']} · 首次判断原始样本", exact=True
            )).first
            original_details.locator("summary").click()
            original_view = original_details.locator(".structure-confidence")
        expect(original_view).to_be_visible()
        expect(original_view).to_contain_text("首次判断时")
        expect(original_view).not_to_contain_text("回放补充对照")
        expect(original_view).to_contain_text(re.compile(
            r"有效样本 n=" + re.escape(_sample_text(event["initial_prediction"].get("sample_count"))) + r"(?:\s|·|$)"
        ))
        checked["initial_prediction"] = "passed"
        checked["original_and_reference_counts_differ"] = event["initial_prediction"].get("sample_count") != (
            event.get("historical_reference") or {}
        ).get("sample_count")
        page.screenshot(path=str(evidence / f"{width}-structure-original-reference.png"), full_page=True)
        if dialog.count():
            dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
        else:
            panel.get_by_role("button", name="清空叠加", exact=True).click()
        break

    with page.expect_response(lambda r: _structure_read(r, "/api/v2/patterns/study", timeframe="1h")) as response:
        panel.get_by_role("tab", name="历史表现", exact=True).click()
    study = response.value.json()
    overview = panel.locator('[aria-label="历史样本概览"]')
    expect(overview).to_be_visible()
    report = study.get("report")
    checked["study_state"] = study["state"]
    checked["partial"] = bool(report and report.get("partial"))
    rows = [row for row in (report or {}).get("rows", []) if row["phase"] == "CONFIRMED" and row["horizon"] == 20]
    expected_counts = {
        "识别总量": (report or {}).get("candidate_count"),
        "有效收益样本": sum(row["sample_count"] for row in rows) if rows else None,
        "重叠排除": sum(row["overlap_excluded"] for row in rows) if rows else None,
        "等待走完": sum(row["pending"] for row in rows) if rows else None,
    }
    for label, value in expected_counts.items():
        field = overview.locator("dl > div").filter(has=page.get_by_text(label, exact=True)).locator("dd")
        expect(field).to_have_text(_sample_text(value))
    checked["sample_overview"] = expected_counts
    if study["state"] in ("QUEUED", "RUNNING"):
        progress = panel.locator(".pattern-progress progress")
        expect(progress).to_be_visible()
        assert 0 <= float(progress.get_attribute("value")) <= float(progress.get_attribute("max"))
        checked["replay_progress_visible"] = True
    if report and report.get("partial"):
        expect(overview).to_contain_text("部分完成")
    elif report and study["state"] == "READY":
        expect(overview).to_contain_text("本地回放已完成")
    if report and report.get("completion_rows") is not None:
        picker = panel.get_by_label("候选统计阶段", exact=True)
        expect(picker).to_have_value("FORMED")
        checked["default_completion_stage"] = "FORMED"
        picker.select_option("FORMING")
        expect(picker).to_have_value("FORMING")
        metas = report.get("catalog") or current.get("catalog", [])
        unsupported = next((meta for meta in metas if meta.get("supports_forming") is False), None)
        assert unsupported is not None, "Catalog must explicitly identify unsupported forming stages"
        panel.get_by_label("搜索形态或波浪", exact=True).fill(unsupported["name"])
        table = panel.get_by_role("region", name="候选完成统计表", exact=True)
        unavailable_row = table.locator("tbody tr").filter(has=page.get_by_role("rowheader", name=unsupported["name"], exact=False)).first
        expect(unavailable_row).to_contain_text("此形态不支持形成中预判，请查看已形成阶段")
        expect(unavailable_row).to_contain_text("—")
        expect(unavailable_row).not_to_contain_text("0.0%")
        checked["unsupported_forming_stage"] = "passed"
        assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
        page.screenshot(path=str(evidence / f"{width}-structure-history-forming-na.png"), full_page=True)
        panel.get_by_label("搜索形态或波浪", exact=True).fill("")
        picker.select_option("FORMED")
    else:
        checked["default_completion_stage"] = "not_covered_report_not_ready"
        checked["unsupported_forming_stage"] = "not_covered_report_not_ready"
    assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
    page.screenshot(path=str(evidence / f"{width}-structure-history-summary.png"), full_page=True)
    panel.get_by_role("tab", name="当前结构", exact=True).click()
    panel.get_by_label("结构状态", exact=True).select_option("all")
    return checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--external-tests", action="store_true", help="Send one marked TG test and run one configured Coinglass connectivity test")
    args = parser.parse_args()
    if args.label.startswith("structure-history") and args.external_tests:
        parser.error("structure-history is read-only; --external-tests is not permitted")
    os.umask(0o077)
    evidence = Path(tempfile.mkdtemp(prefix=f"gatescope-live-{args.label}-"))
    env = read_env(SECRET_DIR / "formal-runtime.env")
    origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
    owner = json.loads((SECRET_DIR / "owner-login.json").read_text())
    for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
        os.environ[key] = env[key]
    enrollment = recover_active_enrollment(owner["username"])
    report = {"label": args.label, "pages": [], "switches": [], "errors": [], "blocked_writes": [], "requests": [], "external_tests": []}
    external_submissions = set()
    step = "launch"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"))
        context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="zh-CN")
        context.add_init_script("""window.__longTasks=[];new PerformanceObserver(list=>{for(const e of list.getEntries())if(window.__longTasks.length<512)window.__longTasks.push(e.duration)}).observe({type:'longtask',buffered:true});""")

        def fence(route):
            request = route.request
            path = urlsplit(request.url).path
            external_kind = "telegram" if path.startswith("/api/v2/notification-channels/") and path.endswith("/test") else "coinglass" if path == "/api/v2/data-sources/coinglass:test" else None
            if request.method == "POST" and args.external_tests and external_kind and external_kind not in external_submissions:
                external_submissions.add(external_kind)
                route.continue_()
                return
            if request.method not in ("GET", "HEAD", "OPTIONS") and path not in ("/auth/login", "/auth/mfa/verify", "/auth/logout", "/api/v2/indicators/preview"):
                report["blocked_writes"].append(path)
                route.abort()
            else:
                route.continue_()

        context.route("**/*", fence)
        page = context.new_page()
        page.set_default_timeout(20000)
        expect.set_options(timeout=20000)
        page.on("pageerror", lambda error: report["errors"].append({"type": "javascript", "name": error.name}))

        def response_finished(response):
            path = urlsplit(response.url).path
            if path.startswith("/api/"):
                report["requests"].append({"path": path, "status": response.status, "query_period": parse_qs(urlsplit(response.url).query).get("timeframe", [None])[0], "limit": parse_qs(urlsplit(response.url).query).get("limit", [None])[0]})

        page.on("response", response_finished)
        try:
            step = "login"
            page.goto(origin + "/login", wait_until="domcontentloaded")
            page.locator('select[name="tenant"]').select_option("default")
            page.locator('input[name="username"]').fill(owner["username"])
            page.locator('input[name="password"]').fill(owner["password"])
            page.get_by_role("button", name="安全登录", exact=True).click()
            if enrollment:
                step = "mfa"
                code, _ = totp_code(enrollment["secret"])
                page.locator(".mfa-code-inline").fill(code)
                page.get_by_role("button", name="验证并进入系统", exact=True).click()
            expect(page.locator(".topbar")).to_be_visible()
            print("LIVE browser: encrypted login and MFA passed", flush=True)
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 960})
                for path in ("/markets/chart", "/strategies/alerts", "/strategies/backtests", "/trading/paper", "/trading/exchange-accounts", "/settings/notifications", "/integrations/mcp", "/integrations/data-sources", "/settings/account", "/notifications/inbox"):
                    step = f"{width}:{path}"
                    started = time.perf_counter()
                    page.goto(origin + path, wait_until="domcontentloaded")
                    expect(page.locator(".topbar")).to_be_visible()
                    if path == "/markets/chart":
                        expect(page.locator(".chart-host canvas").first).to_be_visible()
                        expect(page.locator(".watch-select").first).to_be_visible()
                    page.wait_for_timeout(700)
                    summary = page.evaluate("""() => ({overflow:document.documentElement.scrollWidth>innerWidth+2,longTasks:window.__longTasks.length,maxLongTask:Math.round(Math.max(0,...window.__longTasks)),resources:performance.getEntriesByType('resource').filter(e=>e.name.includes('/assets/')).map(e=>({file:e.name.split('/').pop(),duration:Math.round(e.duration),bytes:e.encodedBodySize}))})""")
                    summary.update(width=width, path=path, elapsed_ms=round((time.perf_counter()-started)*1000))
                    report["pages"].append(summary)
                    expect(page.get_by_role("heading", name="页面暂时无法显示")).to_have_count(0)
                    page.screenshot(path=str(evidence / f"{width}-{path.replace('/', '-')}.png"), full_page=True)
                    print(f"LIVE {width} {path}: {summary['elapsed_ms']} ms overflow={summary['overflow']} long_tasks={summary['longTasks']}", flush=True)
                    if args.label.startswith("pattern-notices") and path == "/strategies/alerts":
                        expect(page.locator('select[name="phase"]')).to_have_value("confirmed")
                        card = page.locator(".strategy-card").filter(has_text="TG形态做单观察").first
                        expect(card).to_be_visible()
                        card.get_by_role("button", name="编辑", exact=True).click()
                        dialog = page.get_by_role("dialog")
                        expect(dialog.locator('select[name="phase"]')).to_have_value("confirmed")
                        dialog.locator('select[name="phase"]').select_option("formed")
                        expect(dialog.locator('select[name="phase"]')).to_have_value("formed")
                        dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
                        expect(page.get_by_role("dialog")).to_have_count(0)
                        summary["pattern_notice_phase_editor"] = True
                        print(f"LIVE {width}: configured shape rules and phase editor passed; no settings saved", flush=True)
                    if args.label.startswith("structure") and path == "/markets/chart":
                        panel=page.get_by_role("region",name="形态与波浪",exact=True)
                        expect(panel).to_contain_text("跟踪")
                        panel.get_by_label("形态类别").select_option("wave")
                        expect(panel.get_by_label("形态类别")).to_have_value("wave")
                        panel.get_by_label("形态类别").select_option("all")
                        panel.get_by_label("搜索形态或波浪").fill("不存在的结构")
                        expect(panel).to_contain_text("筛选 0 /")
                        panel.get_by_label("搜索形态或波浪").fill("")
                        page.wait_for_timeout(1800)
                        choices=panel.locator(".pattern-card:not([disabled])")
                        if choices.count():
                            choices.first.click()
                            if page.get_by_role("dialog").count():
                                expect(page.get_by_role("dialog").locator("canvas").first).to_be_visible()
                                page.get_by_role("button",name="关闭弹窗").click()
                            else:
                                expect(page.locator(".chart-pattern-legend")).to_be_visible()
                                page.locator(".chart-panel").scroll_into_view_if_needed()
                                page.screenshot(path=str(evidence/f"{width}-structure-overlay.png"))
                                panel.get_by_role("button",name="清空叠加",exact=True).click()
                        panel.get_by_role("tab",name="候选档案",exact=True).click()
                        expect(panel.get_by_label("档案来源")).to_be_visible()
                        panel.get_by_role("tab",name="当前结构",exact=True).click()
                        print(f"LIVE {width}: structure filters, zero-result count, archive and available overlay passed",flush=True)
                        if args.label.startswith("structure-history"):
                            step = f"{width}:structure-history read-only acceptance"
                            summary["structure_history"] = verify_structure_history(page, panel, width, evidence)
                            print(f"LIVE {width}: history overview/progress and available original-reference/stage checks completed; no replay or settings submitted", flush=True)
                    if args.label.startswith("performance") and path == "/strategies/backtests":
                        step = f"{width}:performance screening"
                        expect(page.locator(".performance-screen tbody tr")).to_have_count(4)
                        page.get_by_role("button", name="历史记录", exact=False).click()
                        picker = page.get_by_label("验证结果筛选")
                        for selection in ("PASS", "REJECTED", "UNASSESSED", ""):
                            with page.expect_response(lambda r, selection=selection: urlsplit(r.url).path == "/api/v2/research/backtests" and parse_qs(urlsplit(r.url).query).get("screening", [""]) == [selection] and r.status == 200) as result:
                                picker.select_option(selection)
                            payload = result.value.json()
                            expected = len(payload["items"])
                            expect(page.locator(".research-history-list > button")).to_have_count(expected)
                            if selection == "PASS":
                                assert all(item["screening"]["shortlisted"] for item in payload["items"])
                        page.locator(".research-history-list > button").first.click()
                        expect(page.locator(".performance-screen tbody tr")).to_have_count(4)
                        page.get_by_role("button", name="新建回测", exact=False).click()
                        expect(page.locator("dialog")).to_be_visible()
                        for name in ("趋势回踩再入", "放量通道突破", "波动过滤动量"):
                            radio = page.get_by_role("radio", name=name, exact=False)
                            radio.check()
                            expect(radio).to_be_checked()
                        page.get_by_role("button", name="关闭弹窗", exact=True).click()
                        expect(page.locator("dialog")).to_have_count(0)
                        summary["performance_screen_and_filters_passed"] = True
                        print(f"LIVE {width}: four gates, all four filters and three strategy selections passed; no research submitted", flush=True)
                    if args.label.startswith("discovery") and path == "/strategies/backtests":
                        step = f"{width}:discovery research results"
                        page.get_by_role("button", name="策略挖掘", exact=True).click()
                        expect(page.get_by_role("heading", name="基础成本达标方案", exact=True)).to_be_visible()
                        expect(page.get_by_text("3 套基础成本达标 · 0 套可部署", exact=True)).to_be_visible()
                        assert parse_qs(urlsplit(page.url).query).get("view") == ["discovery"]
                        details = page.get_by_role("button", name="查看对冲交易", exact=True)
                        expect(details).to_have_count(3)
                        for index in range(3):
                            details.nth(index).click()
                            dialog = page.locator("dialog")
                            expect(dialog).to_be_visible()
                            expect(dialog.locator("table").nth(1).locator("tbody tr")).to_have_count(10)
                            first_trade = dialog.locator("table").nth(1).locator("tbody tr").first.inner_text()
                            page.get_by_role("button", name="对冲交易下一页", exact=True).click()
                            assert first_trade != dialog.locator("table").nth(1).locator("tbody tr").first.inner_text()
                            page.get_by_role("button", name="对冲交易首页", exact=True).click()
                            assert first_trade == dialog.locator("table").nth(1).locator("tbody tr").first.inner_text()
                            page.screenshot(path=str(evidence / f"{width}-hedge-{index}.png"), full_page=True)
                            page.keyboard.press("Escape")
                            expect(dialog).to_have_count(0)
                        page.get_by_text("全部资金费率测试（含失败）", exact=True).click()
                        expect(page.locator("details").filter(has=page.get_by_text("全部资金费率测试（含失败）", exact=True)).locator("tbody tr")).to_have_count(24)
                        rejected_xrp = page.locator(".discovery-reports details tbody tr").filter(has=page.get_by_text("XRPUSDT · 定期对冲", exact=True)).filter(has=page.get_by_text("1×", exact=True))
                        expect(rejected_xrp).to_contain_text("保证金不足")
                        with page.expect_download() as download:
                            page.get_by_role("button", name="下载挖掘报告", exact=True).click()
                        exported = json.loads(Path(download.value.path()).read_text())
                        assert len(exported["candidates"]) == 3 and exported["deployable_count"] == 0
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_role("heading", name="基础成本达标方案", exact=True)).to_be_visible()
                        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/v2/research/discoveries" and r.status == 200), page.expect_response(lambda r: urlsplit(r.url).path == "/api/v2/research/backtests" and r.status == 200) as refreshed_history:
                            page.get_by_role("button", name="刷新", exact=True).click()
                        history_count = refreshed_history.value.json()["total"]
                        expect(page.get_by_role("button", name=f"历史记录 · {history_count}", exact=True)).to_be_visible()
                        expect(page.get_by_role("heading", name="基础成本达标方案", exact=True)).to_be_visible()
                        assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
                        page.screenshot(path=str(evidence / f"{width}-discovery.png"), full_page=True)
                        summary["discovery_details_pagination_download_passed"] = True
                        print(f"LIVE {width}: three findings, hedge pagination, 24 trials, download and deep-link reload passed; no trading writes", flush=True)
                    if args.label.startswith("directional") and path == "/strategies/backtests":
                        step=f"{width}:non-hedged strategy research"
                        page.get_by_role("button",name="策略挖掘",exact=True).click()
                        expect(page.get_by_role("heading",name="非对冲 · 指标 / 形态 / 波浪策略挖掘",exact=True)).to_be_visible()
                        identifier=page.get_by_label("研究批次",exact=True).input_value()
                        study=page.request.get(origin+f"/api/v2/research/discoveries/{identifier}").json()
                        assert study["kind"]=="DIRECTIONAL" and study["frozen_count"]==56 and study["qualified_count"]==0
                        assert "details" not in study and report_digest(study)==study["artifact_digest"]
                        for category in ("指标","形态","波浪"):
                            page.get_by_label("方向研究类别",exact=True).select_option(category)
                            expected=sum(r["category"]==category for r in study["rows"])
                            expect(page.locator(".directional-discovery > section table tbody tr")).to_have_count(min(10,expected))
                            page.get_by_role("button",name="查看验证交易",exact=True).first.click()
                            dialog=page.locator("dialog")
                            expect(dialog.locator('[aria-label="方向策略日净值"] canvas').first).to_be_visible()
                            page.get_by_role("button",name="日回撤",exact=True).click()
                            expect(dialog.locator('[aria-label="方向策略日回撤"] canvas').first).to_be_visible()
                            first=dialog.locator(".directional-trades tbody tr").first.inner_text()
                            page.get_by_role("button",name="方向验证交易下一页",exact=True).click()
                            assert first!=dialog.locator(".directional-trades tbody tr").first.inner_text()
                            page.get_by_role("button",name="方向验证交易首页",exact=True).click()
                            page.get_by_role("button",name="入场依据",exact=True).first.click()
                            expect(dialog.locator(".trade-rule-facts")).to_be_visible()
                            expect(dialog.locator(".trade-rule-facts")).to_be_in_viewport()
                            if category == "波浪":
                                expect(dialog.locator(".trade-rule-facts tbody tr").first).to_be_visible()
                            page.screenshot(path=str(evidence/f"{width}-directional-{category}.png"),full_page=True)
                            page.keyboard.press("Escape")
                            expect(dialog).to_have_count(0)
                        page.get_by_label("方向研究类别",exact=True).select_option("")
                        page.get_by_label("方向研究验证结果",exact=True).select_option("PASS")
                        expect(page.get_by_text("没有符合当前筛选的方案。",exact=True)).to_be_visible()
                        page.get_by_label("方向研究验证结果",exact=True).select_option("")
                        first=page.locator(".directional-discovery > section table tbody tr").first.inner_text()
                        page.get_by_role("button",name="方向研究结果下一页",exact=True).click()
                        assert first!=page.locator(".directional-discovery > section table tbody tr").first.inner_text()
                        page.get_by_role("button",name="方向研究结果首页",exact=True).click()
                        if width==1440:
                            with page.expect_download() as download:
                                page.get_by_role("button",name="下载挖掘报告",exact=True).click()
                            exported=json.loads(Path(download.value.path()).read_text())
                            assert len(exported["details"])==56 and report_digest(exported)==exported["artifact_digest"]
                        page.reload(wait_until="domcontentloaded")
                        expect(page.get_by_role("heading",name="非对冲 · 指标 / 形态 / 波浪策略挖掘",exact=True)).to_be_visible()
                        with page.expect_response(lambda r:urlsplit(r.url).path=="/api/v2/research/discoveries" and r.status==200):
                            page.get_by_role("button",name="刷新",exact=True).click()
                        expect(page.get_by_role("heading",name="指标 / 形态 / 波浪验证",exact=True)).to_be_visible()
                        assert not page.evaluate("document.documentElement.scrollWidth>innerWidth+2")
                        page.screenshot(path=str(evidence/f"{width}-directional-summary.png"),full_page=True)
                        summary["directional_filters_trades_evidence_pagination_passed"]=True
                        print(f"LIVE {width}: non-hedged results, all categories, detail curves, trades, original evidence, filters, pagination and reload passed",flush=True)
                    if args.external_tests and width == 1440 and path == "/settings/notifications":
                        step = "telegram single test delivery"
                        sender = page.get_by_role("button", name="发送测试", exact=True).first
                        if sender.count() and sender.is_enabled():
                            sender.click()
                            expect(page.get_by_text("Telegram 测试消息已送达", exact=True)).to_be_visible(timeout=45000)
                            report["external_tests"].append({"name":"telegram","status":"delivered"})
                            print("LIVE Telegram: one marked test message delivered", flush=True)
                        else:
                            report["external_tests"].append({"name":"telegram","status":"no_active_channel"})
                    if args.external_tests and width == 1440 and path == "/integrations/data-sources":
                        step = "coinglass connectivity"
                        tester = page.get_by_role("button", name="重新检测", exact=True)
                        if tester.count() and tester.is_enabled():
                            tester.click()
                            expect(page.get_by_text("三个接口均检测成功", exact=True)).to_be_visible(timeout=90000)
                            report["external_tests"].append({"name":"coinglass_lsr_account","status":"passed"})
                            print("LIVE Coinglass/LSR/account: all three checks passed", flush=True)
                        else:
                            report["external_tests"].append({"name":"coinglass","status":"not_configured"})
                    if path != "/markets/chart":
                        continue
                    menu = page.locator(".profile-menu")
                    menu.locator("summary").click()
                    page.keyboard.press("Escape")
                    summary["profile_escape_closes"] = not bool(menu.get_attribute("open") is not None)
                    if args.label != "baseline":
                        assert summary["profile_escape_closes"], "Profile menu must dismiss on Escape"
                    if menu.get_attribute("open") is not None:
                        menu.locator("summary").click()
                    for timeframe in ("1m", "5m", "1h", "4h", "8h", "1d", "15m", "30m"):
                        step = f"{width}:switch:{timeframe}"
                        button = page.locator(".timeframes").get_by_role("button", name=timeframe, exact=True)
                        started = time.perf_counter()
                        with page.expect_response(lambda r, timeframe=timeframe: urlsplit(r.url).path == "/api/v2/bars" and parse_qs(urlsplit(r.url).query).get("timeframe") == [timeframe] and r.status == 200) as market_response:
                            button.click()
                        expect(button).to_have_class("active")
                        expect(page.locator(".chart-host canvas").first).to_be_visible()
                        switch={"width":width,"period":timeframe,"ms":round((time.perf_counter()-started)*1000)}
                        if args.label.startswith("background"):
                            response = market_response.value
                            payload = response.json()
                            switch.update(read_source=payload.get("read_source"),bars=len(payload.get("items",[])),
                                content_encoding=response.headers.get("content-encoding"),decoded_bytes=len(response.body()))
                            assert switch["bars"]>0 and switch["content_encoding"]=="gzip"
                            sizes=page.evaluate("url=>performance.getEntriesByName(url).map(e=>({encoded:e.encodedBodySize,decoded:e.decodedBodySize})).at(-1)",response.url)
                            switch["transfer_sizes"]=sizes
                            query=parse_qs(urlsplit(response.url).query)
                            began=time.perf_counter()
                            current=page.request.get(origin+f"/api/v2/patterns/current?instrument_id={query['instrument_id'][0]}&timeframe={timeframe}&limit=12").json()
                            switch["structures_ms"]=round((time.perf_counter()-began)*1000)
                            references=[e.get("historical_reference",{}) for e in current.get("items",[])]
                            switch["recorded_reference_count"]=max((r.get("recorded_count",0) for r in references),default=0)
                            example=next((f for r in references for f in r.get("failure_examples",[]) if f["source"]=="RECORDED"),None)
                            if example:
                                detail=page.request.get(origin+f"/api/v2/patterns/events/{example['context_id']}/{example['id']}").json()
                                assert detail["price_basis"]=="ORIGINAL_REVISION_THEN_FOLLOWUP" and detail["bars"]
                                assert detail["transitions"][-1]["record"]["phase"] in ("INVALIDATED","EXPIRED")
                                switch["real_failure_original_evidence"]=True
                        report["switches"].append(switch)
                    page.locator(".timeframes").get_by_role("button", name="1d", exact=True).click()
                    page.wait_for_timeout(11500)
                    page.get_by_role("button", name="搜索并添加币种", exact=True).click()
                    expect(page.locator("dialog")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(page.locator("dialog")).to_have_count(0)
            step = "logout"
            page.locator(".profile-menu summary").click()
            page.get_by_role("button", name="退出登录", exact=True).click()
            expect(page.get_by_role("button", name="安全登录", exact=True)).to_be_visible()
            print("LIVE browser: own session logged out; no settings or trading writes", flush=True)
        except Exception as error:
            frames = [frame.lineno for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name == Path(__file__).name]
            report["errors"].append({"step": step, "type": type(error).__name__, "script_lines": frames, "page_path": urlsplit(page.url).path})
            if urlsplit(page.url).path != "/login":
                try:
                    page.screenshot(path=str(evidence / "failed-page.png"), full_page=True)
                except Exception:
                    pass
            print(f"LIVE browser failed at {step}: {type(error).__name__}; private details suppressed", flush=True)
        finally:
            (evidence / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print("Private browser evidence:", evidence, flush=True)
            context.close()
            browser.close()
    return 1 if report["errors"] or report["blocked_writes"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
