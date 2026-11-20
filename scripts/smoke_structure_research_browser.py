"""Read-only real HTTPS acceptance for same-engine research and shadow records.

Run on the deployment host, after migration/build, with its existing Playwright
environment. Optional --run-id locks acceptance to one completed real research
run. No research, signal, order, Telegram test, setting or strategy is submitted.
Only encrypted login, existing MFA verification and logout may write remotely.
Screenshots and the redacted report stay in a mode-0700 temporary directory.
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
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from prepare_service_envs import SECRET_DIR, read_env
from gatescope.security_secrets import totp_code

AUTH_POSTS = frozenset(("/auth/login", "/auth/mfa/verify", "/auth/logout"))
SHADOW = "/api/v2/research/shadow-trades"
RESEARCH = "/api/v2/research/backtests"


def response_is(response, path, **query):
    parsed = urlsplit(response.url)
    values = parse_qs(parsed.query, keep_blank_values=True)
    return (response.request.method == "GET" and response.status == 200 and parsed.path == path
            and all(values.get(key, [""]) == [str(value)] for key, value in query.items()))


def read(page, origin, path):
    if not path.startswith("/api/v2/research/"):
        raise ValueError("READ_SCOPE_INVALID")
    result = page.request.get(origin + path)
    if result.status != 200:
        raise RuntimeError("READ_RESPONSE_UNAVAILABLE")
    return result.json()


def number(page, value, percent=False):
    return page.evaluate("""([value, percent]) => typeof value !== 'number' || !Number.isFinite(value)
      ? '—' : percent ? `${(value * 100).toFixed(2)}%`
      : value.toLocaleString(undefined, {maximumFractionDigits: 8})""", [value, percent])


def fact(page, scope, label, value):
    field = scope.locator("dl > div").filter(has=page.get_by_text(label, exact=True)).locator("dd")
    expect(field).to_have_text(value)


def screenshot(page, evidence, name):
    # Never capture login/MFA fields, even if a session expires during a check.
    if page.locator('input[name="password"], .mfa-code-inline').count():
        raise RuntimeError("AUTH_SCREEN_NOT_CAPTURED")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    page.screenshot(path=str(evidence / f"{name}.png"), full_page=True,
                    mask=[page.locator(".profile-menu")])


def click_read(page, control, path, **query):
    with page.expect_response(lambda response: response_is(response, path, **query)) as pending:
        control.click()
    return pending.value.json()


def select_read(page, control, value, path, **query):
    with page.expect_response(lambda response: response_is(response, path, **query)) as pending:
        control.select_option(value)
    return pending.value.json()


def verify_builder(page, evidence, width):
    page.get_by_role("button", name="＋ 同源形态回测", exact=True).click()
    dialog = page.get_by_role("dialog", name="新建同源形态回测", exact=True)
    form = dialog.get_by_role("form", name="同源形态回测配置", exact=True)
    expect(form).to_be_visible()
    form.get_by_label("K 线周期", exact=True).select_option("1m")
    expect(form.get_by_label("最多读取 K 线", exact=False)).to_have_value("525600")
    form.get_by_label("K 线周期", exact=True).select_option("4h")
    expect(form.get_by_label("最多读取 K 线", exact=False)).to_have_value("2190")
    form.get_by_label("入场事件阶段", exact=True).select_option("FORMED")
    expect(form).to_contain_text("仍需满足原始突破确认规则")
    form.get_by_label("模拟方向", exact=True).select_option("SHORT")
    form.get_by_label("最低几何评分（非胜率）", exact=True).fill("0.75")
    form.get_by_label("滚动验证分段", exact=True).fill("5")
    form.get_by_label("单边手续费（bp）", exact=True).fill("6")
    form.get_by_label("单边滑点（bp）", exact=True).fill("7")
    form.get_by_label("每 8 小时资金费估算（bp）", exact=True).fill("2")
    form.get_by_role("button", name="清空选择", exact=True).click()
    expect(form.locator(".structure-patterns input:checked")).to_have_count(0)
    form.get_by_label("搜索同源形态", exact=True).fill("双底")
    expect(form.locator(".structure-patterns input[type=checkbox]").first).to_be_visible()
    form.locator(".structure-patterns input[type=checkbox]").first.check()
    form.get_by_label("搜索同源形态", exact=True).fill("smoke-no-matching-shape")
    expect(form).to_contain_text("没有匹配项，已选形态仍保留")
    form.get_by_label("搜索同源形态", exact=True).fill("")
    form.get_by_role("button", name="全选形态", exact=True).click()
    assert form.locator(".structure-patterns input:checked").count() > 20
    form.locator(".instrument-trigger").click()
    picker = form.get_by_role("combobox", name="搜索币种", exact=True)
    # Wait for this query's debounced response, not the previous watchlist
    # options that can still be visible immediately after typing.
    with page.expect_response(lambda response: response_is(response, "/api/v2/instruments", q="BTC", limit=12)):
        picker.fill("BTC")
    expect(form.get_by_role("listbox", name="币种搜索结果").get_by_role("option").first).to_be_visible()
    picker.press("ArrowDown")
    options = form.get_by_role("listbox", name="币种搜索结果").get_by_role("option")
    active_index = 1 if options.count() > 1 else 0
    expect(picker).to_have_attribute("aria-activedescendant", options.nth(active_index).get_attribute("id"))
    picker.press("Enter")
    expect(form.locator(".instrument-trigger")).to_contain_text("BTC")
    expect(form.get_by_role("button", name="开始同源回测", exact=True)).to_be_enabled()
    screenshot(page, evidence, f"{width}-structure-form-no-submit")
    dialog.get_by_role("button", name="关闭弹窗", exact=True).click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    return {"status": "passed", "submitted": False, "controls_checked": 13}


def verify_research(page, origin, evidence, width, run_id):
    query = {"view": "structure", **({"structure_run": run_id} if run_id else {})}
    page.goto(origin + "/strategies/backtests?" + urlencode(query), wait_until="domcontentloaded")
    expect(page.locator(".structure-toolbar")).to_be_visible()
    checked = {"width": width, "form": verify_builder(page, evidence, width)}
    if not run_id:
        # Real completed reports only: never manufacture an artifact for acceptance.
        rows = read(page, origin, RESEARCH + "?engine_mode=STRUCTURE&limit=100&offset=0")["items"]
        available = next((item for item in rows if item["state"] == "SUCCEEDED"), None)
        if not available:
            checked["report"] = "not_covered_no_completed_real_run"
            screenshot(page, evidence, f"{width}-structure-no-completed-report")
            return checked
        run_id = str(available["id"])
        page.goto(origin + "/strategies/backtests?" + urlencode({"view": "structure", "structure_run": run_id}), wait_until="domcontentloaded")
    run = read(page, origin, RESEARCH + "/" + run_id)
    assert run["state"] == "SUCCEEDED" and run.get("report", {}).get("structure_research")
    assert run["spec"]["engine_mode"] == "STRUCTURE"
    report = run["report"]
    checked.update(run_id=run_id, run_state=run["state"], bars=report["bar_count"])
    expect(page.get_by_role("heading", name="冻结形态计划 · TP / SL 模拟结果", exact=True)).to_be_visible()
    for title in ("全部候选与未筛选对照", "入场环境分层", "滚动验证", "成本压力测试", "相对对照组的差异"):
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    summary_panel = page.locator("section").filter(has=page.get_by_role("heading", name="冻结形态计划 · TP / SL 模拟结果", exact=True))
    summary = report["structure_research"]["summary"]
    fact(page, summary_panel, "已结算模拟案例", f"{summary['sample_count']} / {summary['case_count']}")
    fact(page, summary_panel, "模拟净胜率", number(page, summary["net_win_rate"], True) if summary["sample_count"] else "尚无结算样本")
    for dimension in ("trend", "volatility", "volume", "higher_alignment", "score_bucket"):
        page.get_by_label("环境分层维度", exact=True).select_option(dimension)
        expect(page.get_by_label("环境分层维度", exact=True)).to_have_value(dimension)
    screenshot(page, evidence, f"{width}-structure-report")
    navigation = page.get_by_role("navigation", name="同源研究结果", exact=True)
    cases_path = RESEARCH + "/" + run_id + "/cases"
    cases = click_read(page, navigation.get_by_role("button", name="候选与逐笔计划", exact=True), cases_path, offset=0)
    panel = page.locator("section").filter(has=page.get_by_role("heading", name="逐个候选 · 冻结模拟记录", exact=True))
    expect(panel.locator("tbody tr")).to_have_count(len(cases["items"]))
    checked["case_total"] = cases["total"]
    if cases["items"]:
        index = next((i for i, item in enumerate(cases["items"]) if item["case"].get("plan", {}).get("status") == "READY"), 0)
        original = cases["items"][index]
        panel.get_by_role("button", name="原始计划", exact=True).nth(index).click()
        dialog = page.get_by_role("dialog", name="同源案例 · 原始计划", exact=True)
        frozen_plan = original["case"].get("plan") or {}
        fact(page, dialog, "原始止损 / 目标", number(page, frozen_plan.get("stop_price")) + " / " + number(page, frozen_plan.get("target_price")))
        dialog.locator("summary").filter(has_text="冻结环境与计划依据").click()
        saved = json.loads(dialog.locator("details[open] pre").inner_text())
        assert saved["plan"] == frozen_plan
        expect(dialog).to_contain_text("未提交真实订单")
        screenshot(page, evidence, f"{width}-structure-frozen-plan")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)
        checked["frozen_case"] = "passed"
    else:
        expect(panel).to_contain_text("暂无匹配案例")
        checked["frozen_case"] = "not_covered_no_real_cases"
    if cases["has_more"]:
        next_page = click_read(page, panel.get_by_role("button", name="同源案例下一页", exact=True), cases_path, offset=20)
        assert not ({item["candidate_id"] for item in cases["items"]} & {item["candidate_id"] for item in next_page["items"]})
        click_read(page, panel.get_by_role("button", name="同源案例首页", exact=True), cases_path, offset=0)
        checked["case_pagination"] = "passed"
    else:
        expect(panel.get_by_role("button", name="同源案例下一页", exact=True)).to_be_disabled()
        checked["case_pagination"] = "single_page_boundary_passed"
    for state in ("TARGET", "UNAVAILABLE", ""):
        filtered = select_read(page, panel.get_by_label("同源案例状态筛选", exact=True), state, cases_path, state=state, offset=0)
        expect(panel.locator("tbody tr")).to_have_count(len(filtered["items"]))
        if state:
            assert all(item["case"]["state"] == state for item in filtered["items"])
    events_path = RESEARCH + "/" + run_id + "/events"
    events = click_read(page, navigation.get_by_role("button", name="原始识别事件", exact=True), events_path, offset=0)
    panel = page.locator("section").filter(has=page.get_by_role("heading", name="原始识别事件", exact=True))
    expect(panel.locator("tbody tr")).to_have_count(len(events["items"]))
    checked["event_total"] = events["total"]
    if events["items"]:
        details = panel.locator("tbody tr").first.locator("details")
        details.locator("summary").click()
        assert json.loads(details.locator("pre").inner_text()) == events["items"][0]
        screenshot(page, evidence, f"{width}-structure-original-event")
        details.locator("summary").click()
    if events["has_more"]:
        second = click_read(page, panel.get_by_role("button", name="原始识别事件下一页", exact=True), events_path, offset=20)
        first_keys = {(item["candidate_id"], item["sequence"]) for item in events["items"]}
        assert not first_keys.intersection((item["candidate_id"], item["sequence"]) for item in second["items"])
        click_read(page, panel.get_by_role("button", name="原始识别事件首页", exact=True), events_path, offset=0)
        checked["event_pagination"] = "passed"
    else:
        expect(panel.get_by_role("button", name="原始识别事件下一页", exact=True)).to_be_disabled()
        checked["event_pagination"] = "single_page_boundary_passed"
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="冻结形态计划 · TP / SL 模拟结果", exact=True)).to_be_visible()
    assert parse_qs(urlsplit(page.url).query).get("structure_run") == [run_id]
    refreshed = read(page, origin, RESEARCH + "/" + run_id)
    assert refreshed["report"]["artifact_digest"] == report["artifact_digest"]
    with page.expect_download() as download:
        page.get_by_role("button", name="下载验证报告", exact=True).click()
    exported = json.loads(Path(download.value.path()).read_text())
    assert exported["id"] == run_id and exported["report"] == report
    checked["report_download"] = "passed"
    checked["report_deeplink_and_immutable_digest"] = "passed"
    return checked


def verify_shadow(page, origin, evidence, width):
    with page.expect_response(lambda response: response_is(response, SHADOW, offset=0)) as loaded:
        page.goto(origin + "/trading/shadow", wait_until="domcontentloaded")
    first = loaded.value.json()
    expect(page.get_by_role("heading", name="通知影子账本", exact=True)).to_be_visible()
    expect(page.locator(".shadow-page")).to_contain_text("两组可能来自同一信号，不合并计算胜率")
    expect(page.locator(".shadow-page")).to_contain_text("净收益已扣滑点、手续费和资金费")
    summary = read(page, origin, SHADOW + "/summary")
    assert summary["environment"] == "SHADOW" and summary["forward_only"] is True
    expect(page.locator(".shadow-page")).to_contain_text("后台观察：")
    checked = {"width": width, "record_total": first["total"], "worker_state": summary["worker"]["state"]}
    filters = page.get_by_role("region", name="影子记录筛选", exact=True)
    for cohort in ("SIGNAL", "DELIVERED", ""):
        payload = select_read(page, filters.get_by_label("观察组", exact=True), cohort, SHADOW, cohort=cohort, offset=0)
        expect(page.locator(".shadow-table tbody tr")).to_have_count(len(payload["items"]))
        if cohort:
            assert all(item["cohort"] == cohort for item in payload["items"])
    for state in ("CLOSED", "EXPIRED", ""):
        payload = select_read(page, filters.get_by_label("记录状态", exact=True), state, SHADOW, state=state, offset=0)
        expect(page.locator(".shadow-table tbody tr")).to_have_count(len(payload["items"]))
        if state:
            assert all(item["state"] == state for item in payload["items"])
    displayed = payload
    checked["record_total"] = displayed["total"]
    for dimension in ("trend", "volatility", "volume", "higher_alignment", "score_bucket"):
        page.get_by_label("影子环境维度", exact=True).select_option(dimension)
        expect(page.get_by_label("影子环境维度", exact=True)).to_have_value(dimension)
    screenshot(page, evidence, f"{width}-shadow-overview")
    if displayed["items"]:
        # Bind to the actual clicked row's detail response: a background refresh
        # may insert a legitimate new signal ahead of the previous first row.
        with page.expect_response(lambda response: response.request.method == "GET" and response.status == 200
                                  and urlsplit(response.url).path.startswith(SHADOW + "/")
                                  and urlsplit(response.url).path != SHADOW + "/summary"
                                  and parse_qs(urlsplit(response.url).query).get("event_offset") == ["0"]) as opened:
            page.locator(".shadow-table tbody tr").first.get_by_role("button").click()
        detail = opened.value.json()
        identifier = str(detail["id"])
        dialog = page.get_by_role("dialog", name="模拟观察记录 · 原始计划", exact=True)
        expect(dialog).to_contain_text("模拟记录，不是真实订单")
        fact(page, dialog, "冻结止损 / 目标", number(page, detail["stop_price"]) + " / " + number(page, detail["target_price"]))
        fact(page, dialog, "总成本（含滑点）", number(page, detail["costs_return"], True))
        expect(dialog).to_contain_text("K 线开盘模拟成交")
        expect(dialog).to_contain_text("使用固定滑点与资金费估算")
        expect(dialog.locator(".research-timeline > li")).to_have_count(len(detail["events"]))
        dialog.locator("summary").filter(has_text="查看冻结计划").click()
        assert json.loads(dialog.locator("details[open] pre").inner_text()) == detail["plan"]
        screenshot(page, evidence, f"{width}-shadow-frozen-plan")
        if detail["events_has_more"]:
            next_events = click_read(page, dialog.get_by_role("button", name="影子事件下一页", exact=True), SHADOW + "/" + identifier, event_offset=20)
            assert not ({event["sequence"] for event in detail["events"]} & {event["sequence"] for event in next_events["events"]})
            click_read(page, dialog.get_by_role("button", name="影子事件首页", exact=True), SHADOW + "/" + identifier, event_offset=0)
            checked["event_pagination"] = "passed"
        else:
            expect(dialog.get_by_role("button", name="影子事件下一页", exact=True)).to_be_disabled()
            checked["event_pagination"] = "single_page_boundary_passed"
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("dialog", name="模拟观察记录 · 原始计划", exact=True)).to_be_visible()
        assert parse_qs(urlsplit(page.url).query).get("trade") == [identifier]
        after = read(page, origin, SHADOW + "/" + identifier)
        assert after["plan"] == detail["plan"] and after["source_event"] == detail["source_event"]
        page.get_by_role("button", name="关闭弹窗", exact=True).click()
        checked["detail_frozen_plan_costs_and_deeplink"] = "passed"
    else:
        expect(page.get_by_role("heading", name="暂无匹配的影子记录", exact=True)).to_be_visible()
        expect(page.locator(".shadow-page")).to_contain_text("暂无记录不等于没有信号")
        expect(page.get_by_role("button", name="影子记录下一页", exact=True)).to_be_disabled()
        checked["detail_frozen_plan_costs_and_deeplink"] = "not_covered_no_forward_records_no_signal_fabricated"
    if displayed["has_more"]:
        click_read(page, page.get_by_role("button", name="影子记录下一页", exact=True), SHADOW, offset=20)
        click_read(page, page.get_by_role("button", name="影子记录首页", exact=True), SHADOW, offset=0)
        checked["list_pagination"] = "passed"
    else:
        checked["list_pagination"] = "single_page_boundary_passed"
    checked["cohort_state_environment_filters"] = "passed"
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Completed real same-engine research UUID; never creates a run")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-structure-research-browser-"))
    evidence.chmod(0o700)
    report = {"kind": "READ_ONLY_REAL_BROWSER", "research": [], "shadow": [], "errors": [], "blocked_requests": [], "api_failures": []}
    browser = context = page = None
    authenticated = False
    step = "configuration"
    try:
        run_id = str(UUID(args.run_id)) if args.run_id else None
        env = read_env(SECRET_DIR / "formal-runtime.env")
        origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme != "https" or parsed_origin.username or parsed_origin.password or parsed_origin.path:
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
                allowed = request.method in ("GET", "HEAD", "OPTIONS") or request.method == "POST" and parsed.path in AUTH_POSTS
                if not same_origin or not allowed:
                    report["blocked_requests"].append({"method": request.method, "path": parsed.path if same_origin else "EXTERNAL_ORIGIN_BLOCKED"})
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
                if authenticated and path.startswith("/api/") and response.status >= 400:
                    report["api_failures"].append({"path": path, "status": response.status})

            page.on("response", response_finished)
            try:
                step = "encrypted_login"
                # Login in place on the research deep-link, so no chart POST or
                # other unrelated workspace request is needed even momentarily.
                page.goto(origin + "/strategies/backtests?view=structure", wait_until="domcontentloaded")
                expect(page.locator('input[name="password"]')).to_be_visible()
                # Anonymous HTTP may redirect to /login. Restore only this
                # browser URL after Login mounts; refreshSession then retains
                # the research destination instead of mounting the chart.
                page.evaluate("history.replaceState({}, '', '/strategies/backtests?view=structure')")
                page.locator('select[name="tenant"]').select_option("default")
                page.locator('input[name="username"]').fill(owner["username"])
                page.locator('input[name="password"]').fill(owner["password"])
                page.get_by_role("button", name="安全登录", exact=True).click()
                if enrollment:
                    step = "existing_mfa_verify"
                    code, _ = totp_code(enrollment["secret"])
                    page.locator(".mfa-code-inline").fill(code)
                    page.get_by_role("button", name="验证并进入系统", exact=True).click()
                expect(page.locator(".structure-toolbar")).to_be_visible()
                authenticated = True
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 960})
                    step = f"research_{width}"
                    started = time.perf_counter()
                    report["research"].append(verify_research(page, origin, evidence, width, run_id))
                    report["research"][-1]["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
                    print(f"READONLY {width}: same-engine report/form/cases/events checks completed", flush=True)
                    step = f"shadow_{width}"
                    report["shadow"].append(verify_shadow(page, origin, evidence, width))
                    print(f"READONLY {width}: real shadow state and available record checks completed", flush=True)
            except Exception:
                if authenticated and not page.locator('input[name="password"], .mfa-code-inline').count():
                    try:
                        page.screenshot(path=str(evidence / "failed-page.png"), full_page=True,
                                        mask=[page.locator(".profile-menu")])
                    except Exception:
                        pass
                raise
            finally:
                # Logout our own session even if a UI assertion fails. This
                # explicit same-origin fetch remains covered by the auth fence.
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
        lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).name == Path(__file__).name]
        report["errors"].append({"step": step, "type": type(error).__name__, "script_lines": lines})
        # Do not print error text, URLs, request bodies, cookies or MFA material.
        print(f"READONLY failed: {type(error).__name__}, script lines {lines}; private details suppressed", flush=True)
    finally:
        (evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Private read-only browser evidence:", evidence, flush=True)
    return int(bool(report["errors"] or report["blocked_requests"] or report["api_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
