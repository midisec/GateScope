"""Read-only production browser acceptance for current-pattern screening.

Requires the deployed screening UI. Uses existing watched BTC/Chinese contracts;
never creates watchlists, replay jobs, strategies, orders, notifications, or
configuration. The only POSTs allowed are encrypted login, existing MFA, own
logout, and the chart's read-only indicator preview. Evidence is private and
contains safe counts/statuses only, never full candidates or authentication data.

Accessible UI labels can be overridden with --labels-json while the feature is
being implemented. This changes selectors only, never the acceptance assertions.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import tempfile
import time
import traceback
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright

from bootstrap_formal_owner_mfa import recover_active_enrollment
from prepare_service_envs import SECRET_DIR, read_env
from smoke_watchlist_history import AUTH_POSTS, PREVIEW, screenshot, select_existing_watch
from gatescope.security_secrets import totp_code

CURRENT = "/api/v2/patterns/current"
TIMEFRAME = "1m"
LABELS = {
    "panel": "形态与波浪",
    "form": "置信度筛选",
    "minimum": "最低评分",
    "maximum": "最高评分",
    "apply": "应用筛选",
    "reset": "重置筛选",
    "preset_all": "全部评分",
    "preset_70": "70 分以上",
    "preset_85": "85 分以上",
    "preset_unscored": "仅未评分",
    "applied_feedback": "已应用",
    "invalid_range": "最低评分不能高于最高评分",
    "summary_open": "筛选结果详情",
    "summary_dialog": "筛选结果详情",
    "summary_total": "全部匹配数量",
    "summary_patterns": "匹配类型列表",
    "summary_directions": "方向占比（非涨跌概率）",
    "summary_pattern_article": "{name} 匹配与历史参考",
    "summary_pattern_count": "类型匹配数量",
    "summary_pattern_share": "类型占比",
    "summary_pattern_next": "匹配形态类型下一页",
    "summary_view_matching": "查看 {name} 匹配结构",
    "historical_reference": "历史参考 · 同类全部评分",
    "card_detail": "查看 {name} 结构详情",
    "detail_title": "{name} · 结构详情",
    "original_target": "目标位置与形成条件",
    "detail_history": "历史形成与确认概率",
    "detail_net_win_rate": "同类历史净胜率",
    "detail_overlay": "在图表显示原理线",
    "detail_back": "返回列表",
    "list_next": "当前结构下一页",
    "list_first": "当前结构首页",
    "search": "搜索形态或波浪",
    "close": "关闭弹窗",
    "empty": "当前条件下暂无匹配结构",
}


def endpoint_kind(path):
    return {CURRENT: "current_patterns", PREVIEW: "indicator_preview",
            "/api/v2/patterns/study": "historical_study",
            "/api/v2/bars": "bars"}.get(path, "other_api")


def viewport_screenshot(page, evidence, name):
    """Readable viewport evidence with the same auth guard and profile masking."""
    if page.locator('input[name="password"], .mfa-code-inline').count():
        raise RuntimeError("AUTH_SCREEN_NOT_CAPTURED")
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    page.screenshot(path=str(evidence / f"{name}-viewport.png"), full_page=False,
                    mask=[page.locator(".profile-menu")])


def query_matches(values, expected):
    for key, value in expected.items():
        actual = values.get(key)
        if value is None:
            if actual is not None:
                return False
        elif isinstance(value, bool):
            if actual not in ([str(value).lower()], None if not value else []):
                return False
        elif isinstance(value, (int, float)):
            try:
                if actual is None or len(actual) != 1 or float(actual[0]) != value:
                    return False
            except ValueError:
                return False
        elif actual != [str(value)]:
            return False
    return True


def current_response(response, instrument, **filters):
    parsed = urlsplit(response.url)
    return (response.request.method == "GET" and response.status == 200 and parsed.path == CURRENT
            and query_matches(parse_qs(parsed.query, keep_blank_values=True),
                              {"instrument_id": instrument, "timeframe": TIMEFRAME, **filters}))


def study_response(response, instrument):
    parsed = urlsplit(response.url)
    return (response.request.method == "GET" and response.status == 200
            and parsed.path == "/api/v2/patterns/study"
            and query_matches(parse_qs(parsed.query), {"instrument_id": instrument, "timeframe": TIMEFRAME}))


def count_field(page, scope, title):
    return scope.locator("dl > div").filter(has=page.get_by_text(title, exact=True)).locator("dd")


def percent_value(page, value):
    return page.evaluate("value => `${(value * 100).toFixed(1)}%`", value)


def historical_values(page, study, version, pattern):
    """Expected labels keep absent/low-count evidence distinct from zero rates."""
    report = study.get("report")
    if report and report.get("version") != version:
        return ("模型不一致",) * 3
    if not report:
        return ("等待历史统计",) * 3
    partial = bool(report.get("partial"))

    def rate(row, field):
        count = (row or {}).get("sample_count")
        if not isinstance(count, int) or count < 0:
            return "回放中 · 暂无统计" if partial else "暂无统计"
        if count == 0:
            return "回放中 · 暂无样本" if partial else "无有效样本"
        if count < 20:
            return "样本不足 20"
        value = row.get(field)
        if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1:
            return percent_value(page, value)
        return "胜率未就绪" if field == "win_rate" else "概率未就绪"

    stages = report.get("completion_rows") or []
    formed = next((item for item in stages if item["pattern"] == pattern
                   and item["phase"] == "FORMING" and item["score_bucket"] == "ALL"), {})
    confirmation = next((item for item in stages if item["pattern"] == pattern
                         and item["phase"] == "FORMED" and item["score_bucket"] == "ALL"), None)
    returns = next((item for item in report.get("rows", []) if item["pattern"] == pattern
                    and item["phase"] == "CONFIRMED" and item["horizon"] == 20), None)
    supported = next((item for item in report.get("catalog", []) if item["id"] == pattern), {}).get("supports_forming") is not False
    return (rate(formed.get("formation"), "probability") if supported else "不支持形成中预判",
            rate(confirmation, "probability"), rate(returns, "win_rate"))


def assert_summary(payload):
    total = payload["total"]
    summary = payload["summary"]
    assert isinstance(total, int) and total >= 0 and summary["total"] == total
    assert 0 <= len(payload["items"]) <= min(payload["limit"], total)
    assert summary["scored_count"] + summary["unscored_count"] == total
    assert set(summary["directions"]) == {"bullish", "bearish", "neutral"}
    assert sum(summary["directions"].values()) == total
    assert sum(summary["phase_counts"].values()) == total
    patterns = summary["patterns"]
    assert len({item["pattern"] for item in patterns}) == len(patterns)
    assert sum(item["count"] for item in patterns) == total
    assert math.isclose(sum(item["share"] for item in patterns), 1 if total else 0, abs_tol=1e-9)
    for item in patterns:
        assert item["count"] > 0 and math.isclose(item["share"], item["count"] / total, abs_tol=1e-9)
        assert sum(item["directions"].values()) == item["count"]
        low, high = item["score_min"], item["score_max"]
        assert low is None and high is None or (
            isinstance(low, (int, float)) and isinstance(high, (int, float)) and 0 <= low <= high <= 1)
    return {"matched_total": total, "visible_count": len(payload["items"]),
            "pattern_types": len(patterns), "scored_count": summary["scored_count"],
            "unscored_count": summary["unscored_count"],
            "directions_balance": True, "pattern_shares_balance": True,
            "summary_is_before_pagination": total > len(payload["items"])}


def assert_items(page, panel, payload, *, minimum=None, maximum=None, unscored=False, pattern=None):
    expect(panel.locator(".pattern-card")).to_have_count(len(payload["items"]))
    for item in payload["items"]:
        value = item["confidence"]
        if unscored:
            assert value is None
        elif minimum is not None or maximum is not None:
            assert isinstance(value, (int, float)) and math.isfinite(value)
            assert minimum is None or value >= minimum
            assert maximum is None or value <= maximum
        if pattern:
            assert item["pattern"] == pattern
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 2")
    return assert_summary(payload)


def apply_range(page, panel, instrument, labels, low, high):
    form = panel.get_by_label(labels["form"], exact=True)
    form.get_by_label(labels["minimum"], exact=True).fill(str(low))
    form.get_by_label(labels["maximum"], exact=True).fill(str(high))
    with page.expect_response(lambda response: current_response(
        response, instrument, confidence_min=low / 100, confidence_max=high / 100,
        unscored_only=False, offset=0,
    )) as loaded:
        form.get_by_role("button", name=labels["apply"], exact=True).click()
    expect(panel).to_contain_text(labels["applied_feedback"])
    return loaded.value.json()


def preset(page, panel, instrument, labels, name, mode, **filters):
    form = panel.get_by_label(labels["form"], exact=True)
    with page.expect_response(lambda response: current_response(response, instrument, offset=0, **filters)) as loaded:
        form.get_by_role("button", name=labels[name], exact=True).click()
        if mode == "draft":
            form.get_by_role("button", name=labels["apply"], exact=True).click()
    return loaded.value.json()


def verify_detail(page, panel, labels, payload, evidence, name):
    if not payload["items"]:
        return "not_covered_no_real_matching_candidates"
    card = panel.locator(".pattern-card").first
    event = payload["items"][0]
    card.get_by_role("button", name=labels["card_detail"].format(name=event["name"]), exact=True).click()
    dialog = page.get_by_role("dialog", name=labels["detail_title"].format(name=event["name"]), exact=True)
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(labels["original_target"])
    expect(dialog).to_contain_text(labels["detail_history"])
    expect(dialog).to_contain_text(labels["detail_net_win_rate"])
    original = event.get("initial_forecast") or event.get("forecast") or {}
    target = (original.get("target") or {}).get("price")
    if target is not None:
        formatted = page.evaluate("value => value.toLocaleString(undefined, {maximumSignificantDigits: 8})", target)
        current_forecast = event.get("forecast") or {}
        original_field = dialog.locator(".forecast-original") if original.get("id") != current_forecast.get("id") else dialog.locator(".structure-forecast > div").first
        expect(original_field).to_contain_text(formatted)
    # Available overlays may be toggled locally, but no layout is saved. A
    # historical off-screen candidate may validly disable the overlay action.
    overlay = dialog.get_by_role("button", name=labels["detail_overlay"], exact=True)
    overlay_status = "not_covered_action_unavailable"
    can_overlay = bool(overlay.count() and overlay.is_enabled())
    screenshot(page, evidence, name + "-candidate-detail")
    viewport_screenshot(page, evidence, name + "-candidate-detail")
    back = dialog.get_by_role("button", name=labels["detail_back"], exact=True)
    back.scroll_into_view_if_needed()
    # Do not scroll the close control into view: it must remain reachable while
    # reading the lower part of this long detail, including on a phone.
    expect(dialog.get_by_role("button", name=labels["close"], exact=True)).to_be_in_viewport(ratio=1)
    viewport_screenshot(page, evidence, name + "-candidate-detail-scrolled")
    back.click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(panel).to_be_visible()
    if can_overlay:
        card.get_by_role("button", name=labels["card_detail"].format(name=event["name"]), exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name=labels["detail_overlay"], exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(panel.get_by_label("已选结构", exact=True)).to_contain_text(event["name"])
        overlay_status = "local_overlay_action_passed"
    clear = panel.get_by_role("button", name="清空叠加", exact=True)
    if clear.count() and clear.is_visible():
        clear.click()
    return {"status": "passed", "original_target": "checked" if target is not None else "not_published",
            "overlay": overlay_status, "close_available_at_bottom": True}


def verify_summary_dialog(page, panel, labels, payload, instrument, evidence, name, filters, *, drillthrough=True):
    with page.expect_response(lambda response: study_response(response, instrument)) as history:
        panel.get_by_role("button", name=labels["summary_open"], exact=True).click()
    study = history.value.json()
    dialog = page.get_by_role("dialog", name=labels["summary_dialog"], exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label(labels["summary_total"], exact=True)).to_have_text(f"{payload['total']:,}")
    expect(dialog.get_by_label(labels["summary_directions"], exact=True)).to_be_visible()
    expect(dialog.get_by_role("region", name=labels["historical_reference"], exact=True)).to_be_visible()
    types = dialog.get_by_label(labels["summary_patterns"], exact=True)
    expect(types).to_be_visible()
    patterns = {item["pattern"]: item for item in payload["summary"]["patterns"]}
    checked = set()
    pages_checked = 0
    for page_index in range(8):
        pages_checked += 1
        for identifier, item in patterns.items():
            article = types.get_by_role("article", name=labels["summary_pattern_article"].format(name=item["name"]), exact=True)
            if not article.count():
                continue
            expect(article.get_by_label(labels["summary_pattern_count"], exact=True)).to_have_text(f"{item['count']:,} 个")
            expect(article.get_by_label(labels["summary_pattern_share"], exact=True)).to_have_text(f"占匹配 {percent_value(page,item['share'])}")
            formation, confirmation, win_rate = historical_values(page, study, payload["version"], identifier)
            expect(count_field(page, article, "历史形成概率")).to_have_text(formation)
            expect(count_field(page, article, "历史确认概率")).to_have_text(confirmation)
            expect(count_field(page, article, "历史净胜率 · 20 根")).to_have_text(win_rate)
            checked.add(identifier)
        next_page = dialog.get_by_role("button", name=labels["summary_pattern_next"], exact=True)
        if not next_page.count() or not next_page.is_enabled():
            break
        next_page.click()
        first_next = list(patterns.values())[(page_index + 1) * 8]
        expect(types.get_by_role("article", name=labels["summary_pattern_article"].format(
            name=first_next["name"]), exact=True)).to_be_visible()
    assert checked == set(patterns), "SUMMARY_TYPE_ROWS_NOT_ALL_RENDERED"
    expect(dialog.get_by_role("button", name=labels["close"], exact=True)).to_be_in_viewport(ratio=1)
    screenshot(page, evidence, name + "-summary")
    viewport_screenshot(page, evidence, name + "-summary")
    checked_result = {"total": payload["total"], "types_checked": len(checked),
                      "type_pages_checked": pages_checked,
                      "close_available_after_pagination": True,
                      "type_pagination": "passed_multiple_pages" if pages_checked > 1 else "single_page_boundary_passed"}
    if patterns and drillthrough:
        # The visible final page determines the actual clicked type. Do not
        # assume catalog order or fabricate a candidate when none exists.
        chosen = next((item for item in patterns.values() if types.get_by_role(
            "button", name=labels["summary_view_matching"].format(name=item["name"]), exact=True).count()), None)
        assert chosen is not None
        with page.expect_response(lambda response: current_response(
            response, instrument, pattern=chosen["pattern"], offset=0, **filters,
        )) as loaded:
            types.get_by_role("button", name=labels["summary_view_matching"].format(name=chosen["name"]), exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        selected = loaded.value.json()
        assert_items(page, panel, selected, minimum=filters.get("confidence_min"),
                     maximum=filters.get("confidence_max"), pattern=chosen["pattern"])
        checked_result["view_matching"] = "passed"
    else:
        dialog.get_by_role("button", name=labels["close"], exact=True).click()
        checked_result["view_matching"] = "not_repeated_all_types_overview" if patterns else "not_covered_no_real_types"
    return checked_result


def verify_screen(page, instrument, symbol_index, width, labels, evidence, preset_mode, report):
    panel = page.get_by_role("region", name=labels["panel"], exact=True)
    form = panel.get_by_label(labels["form"], exact=True)
    expect(form).to_be_visible()
    name = f"{width}-watch-{symbol_index}"
    checked = {"width": width, "watch_index": symbol_index, "timeframe": TIMEFRAME}
    # A custom range proves independent lower+upper handling, including upper
    # bound serialization, instead of testing only a lower-bound preset.
    payload = apply_range(page, panel, instrument, labels, 70, 95)
    checked["range"] = assert_items(page, panel, payload, minimum=.7, maximum=.95)
    # Independently widen the page limit with a fenced GET. If the underlying
    # snapshot did not change, matching totals/distribution must be identical;
    # a server accidentally summarizing only the visible 12 cards will fail.
    url = CURRENT + "?" + urlencode({"instrument_id": instrument, "timeframe": TIMEFRAME,
                                     "confidence_min": .7, "confidence_max": .95, "limit": 96, "offset": 0})
    wider = page.evaluate("""async url => {
      const response = await fetch(url, {credentials:'same-origin'});
      if (response.status !== 200) throw new Error('SCREENING_WIDE_READ_FAILED');
      return response.json();
    }""", url)
    assert_summary(wider)
    if wider.get("as_of") == payload.get("as_of"):
        assert wider["total"] == payload["total"] and wider["summary"] == payload["summary"]
        checked["page_limit_invariance"] = "passed_12_vs_96"
    else:
        checked["page_limit_invariance"] = "not_compared_live_snapshot_advanced"
    checked["candidate_detail"] = verify_detail(page, panel, labels, payload, evidence, name)
    next_page = panel.get_by_role("button", name=labels["list_next"], exact=True)
    if payload["total"] > payload["limit"]:
        with page.expect_response(lambda response: current_response(
            response, instrument, confidence_min=.7, confidence_max=.95, offset=payload["limit"],
        )) as next_result:
            next_page.click()
        second = next_result.value.json()
        assert_items(page, panel, second, minimum=.7, maximum=.95)
        if second.get("as_of") == payload.get("as_of"):
            assert not ({item["id"] for item in payload["items"]} & {item["id"] for item in second["items"]})
        with page.expect_response(lambda response: current_response(
            response, instrument, confidence_min=.7, confidence_max=.95, offset=0,
        )) as first_again:
            panel.get_by_role("button", name=labels["list_first"], exact=True).click()
        payload = first_again.value.json()
        checked["pagination"] = "passed_full_summary_and_distinct_pages"
    else:
        if next_page.count():
            expect(next_page).to_be_disabled()
        checked["pagination"] = "not_covered_single_real_page_boundary_checked"
    form.get_by_label(labels["minimum"], exact=True).fill("90")
    form.get_by_label(labels["maximum"], exact=True).fill("70")
    apply = form.get_by_role("button", name=labels["apply"], exact=True)
    if apply.is_enabled():
        apply.click()
    expect(panel).to_contain_text(labels["invalid_range"])
    page.wait_for_timeout(400)
    assert report["invalid_range_requests"] == 0
    checked["invalid_range"] = "rejected_before_request_with_feedback"
    # Restore a different range so the applied request is never a React no-op.
    payload = apply_range(page, panel, instrument, labels, 70, 100)
    checked["summary"] = verify_summary_dialog(page, panel, labels, payload, instrument, evidence, name,
                                                {"confidence_min": .7, "confidence_max": 1})
    with page.expect_response(lambda response: current_response(
        response, instrument, confidence_min=None, confidence_max=None, unscored_only=False, pattern=None, offset=0,
    )) as reset:
        form.get_by_role("button", name=labels["reset"], exact=True).click()
    assert_items(page, panel, reset.value.json())
    for label, minimum in (("preset_70", .7), ("preset_85", .85)):
        payload = preset(page, panel, instrument, labels, label, preset_mode,
                         confidence_min=minimum, unscored_only=False)
        checked[label] = assert_items(page, panel, payload, minimum=minimum)
    payload = preset(page, panel, instrument, labels, "preset_unscored", preset_mode,
                     confidence_min=None, confidence_max=None, unscored_only=True)
    checked["unscored"] = assert_items(page, panel, payload, unscored=True)
    checked["unscored"]["actual_unknown_examples"] = bool(payload["items"])
    payload = preset(page, panel, instrument, labels, "preset_all", preset_mode,
                     confidence_min=None, confidence_max=None, unscored_only=False)
    checked["all_scores"] = assert_items(page, panel, payload)
    if len(payload["summary"]["patterns"]) > 8:
        # The all-score universe can contain more types than the 70+ subset.
        # Exercise actual type pagination without another drillthrough request;
        # the 70+ summary above already checked the return-to-list action.
        checked["all_types_summary"] = verify_summary_dialog(
            page, panel, labels, payload, instrument, evidence, name + "-all", {}, drillthrough=False)
    else:
        checked["all_types_summary"] = "not_covered_no_multi_page_real_types"
    search = panel.get_by_label(labels["search"], exact=True)
    search_marker = "SMOKE_SCREENING_NO_MATCH_739452"
    with page.expect_response(lambda response: current_response(response, instrument, q=search_marker, offset=0)) as empty:
        search.fill(search_marker)
    result = empty.value.json()
    assert result["total"] == 0
    checked["empty"] = assert_items(page, panel, result)
    expect(panel).to_contain_text(labels["empty"])
    screenshot(page, evidence, name + "-empty")
    with page.expect_response(lambda response: current_response(response, instrument, q="", offset=0)) as restored:
        search.fill("")
    assert_items(page, panel, restored.value.json())
    screenshot(page, evidence, name + "-screening")
    return checked


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC_USDT,龙虾_USDT", help="Comma-separated exact existing watch symbols")
    parser.add_argument("--labels-json", default="{}", help="JSON overrides for the documented accessible LABELS keys")
    parser.add_argument("--preset-mode", choices=("instant", "draft"), default="instant")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    evidence = Path(tempfile.mkdtemp(prefix="gatescope-pattern-screening-browser-"))
    evidence.chmod(0o700)
    report = {"kind": "READ_ONLY_PATTERN_SCREENING_BROWSER", "checks": [], "errors": [],
              "api_failures": [], "blocked_requests": [], "invalid_range_requests": 0,
              "api_response_count": 0, "current_response_count": 0,
              "api_request_timings": [], "timing_unavailable_count": 0,
              "timing_basis": "browser_request_responseEnd_ms_not_an_SLO_benchmark"}
    authenticated = False
    step = "configuration"
    try:
        overrides = json.loads(args.labels_json)
        assert isinstance(overrides, dict) and set(overrides) <= set(LABELS)
        assert all(isinstance(value, str) and 0 < len(value) <= 160 and not re.search(r"[\x00-\x1f]", value)
                   for value in overrides.values())
        labels = {**LABELS, **overrides}
        symbols = list(dict.fromkeys(args.symbols.split(",")))
        assert 1 <= len(symbols) <= 2
        assert all(1 <= len(symbol) <= 80 and all(char == "_" or char.isalnum() for char in symbol)
                   and len(symbol.encode("utf-8")) <= 240 for symbol in symbols)
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
                    return
                if parsed.path == CURRENT:
                    values = parse_qs(parsed.query)
                    try:
                        if float(values.get("confidence_min", ["0"])[0]) > float(values.get("confidence_max", ["1"])[0]):
                            report["invalid_range_requests"] += 1
                    except ValueError:
                        report["invalid_range_requests"] += 1
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
                    if path == CURRENT:
                        report["current_response_count"] += 1
                    if response.status >= 400:
                        report["api_failures"].append({"kind": endpoint_kind(path), "status": response.status})

            page.on("response", response_finished)

            def request_finished(request):
                path = urlsplit(request.url).path
                if not authenticated or not path.startswith("/api/") or len(report["api_request_timings"]) >= 200:
                    return
                try:
                    duration = float(request.timing.get("responseEnd", -1))
                    if not math.isfinite(duration) or duration < 0:
                        raise ValueError("TIMING_UNAVAILABLE")
                    report["api_request_timings"].append({"kind": endpoint_kind(path), "duration_ms": round(duration, 1)})
                except Exception:
                    report["timing_unavailable_count"] += 1

            page.on("requestfinished", request_finished)
            try:
                step = "encrypted_login"
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
                authenticated = True
                if urlsplit(page.url).path != "/markets/chart":
                    page.goto(origin + "/markets/chart", wait_until="domcontentloaded")
                expect(page.locator(".watch-select").first).to_be_visible()
                available = [symbol for symbol in symbols if page.get_by_role(
                    "button", name=f"将 {symbol} 移出自选", exact=True).count()]
                assert available, "NO_REQUESTED_EXISTING_WATCHLIST_SYMBOL"
                report["existing_watch_count"] = len(available)
                for index, symbol in enumerate(available):
                    page.set_viewport_size({"width": 1440, "height": 960})
                    step = f"select_watch_{index}"
                    instrument = select_existing_watch(page, symbol)
                    minute = page.locator(".timeframes").get_by_role("button", name=TIMEFRAME, exact=True)
                    if "active" not in (minute.get_attribute("class") or "").split():
                        with page.expect_response(lambda response, instrument=instrument: current_response(response, instrument)):
                            minute.click()
                    expect(minute).to_have_class("active")
                    for width in (1440, 390):
                        step = f"screen_{index}_{width}"
                        page.set_viewport_size({"width": width, "height": 960})
                        report["checks"].append(verify_screen(page, instrument, index, width, labels,
                                                              evidence, args.preset_mode, report))
                        print(f"READONLY {width}: watched market {index + 1} screening checks completed", flush=True)
            except Exception:
                if authenticated and not page.locator('input[name="password"], .mfa-code-inline').count():
                    try:
                        page.screenshot(path=str(evidence / "failed-page.png"), full_page=True,
                                        mask=[page.locator(".profile-menu")])
                        viewport_screenshot(page, evidence, "failed-page")
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
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["request_latency_summary"] = {}
        for kind in sorted({item["kind"] for item in report["api_request_timings"]}):
            values = sorted(item["duration_ms"] for item in report["api_request_timings"] if item["kind"] == kind)
            report["request_latency_summary"][kind] = {"count": len(values), "median_ms": round(median(values), 1),
                "p95_ms": values[math.ceil(len(values) * .95) - 1], "max_ms": values[-1]}
        (evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Private read-only browser evidence:", evidence, flush=True)
    return int(bool(report["errors"] or report["api_failures"] or report["blocked_requests"] or report["invalid_range_requests"]))


if __name__ == "__main__":
    raise SystemExit(main())
