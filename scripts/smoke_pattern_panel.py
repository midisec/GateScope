"""Built desktop/mobile structure panel acceptance; intercepted public-data fixtures only."""
import json
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

DIST = Path(os.environ.get("SMOKE_DIST", "web/dist")).resolve()
INSTRUMENT = "30000000-0000-0000-0000-000000000033"
NAMES = [("double_top", "双顶"), ("double_bottom", "双底"), ("head_shoulders", "头肩顶"), ("rectangle", "矩形整理"),
    ("rising_wedge", "上升楔形"), ("falling_wedge", "下降楔形"), ("bull_flag", "看涨旗形"),
    ("elliott_impulse_bull", "艾略特上升五浪"), ("elliott_cycle_bear", "下降五浪+ABC完整周期")]


def fixtures(period):
    step = {"15m": 900, "1h": 3600, "4h": 14400}[period]
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    bars = [{"open_time": (origin + timedelta(seconds=i * step)).isoformat(), "open": "100", "high": "110", "low": "90",
        "close": str(99 + i % 3), "volume": "100", "is_final": True, "revision": 1, "quality_flags": []} for i in range(180)]
    catalog, events = [], []
    for index, (identifier, name) in enumerate(NAMES):
        wave = identifier.startswith("elliott")
        catalog.append({"id": identifier, "name": name, "family": "wave" if wave else "chart", "group": "波浪理论" if wave else "图表形态"})
        points = [{"time": int(datetime.fromisoformat(bars[i]["open_time"]).timestamp()), "price": price, "label": label}
            for i, price, label in ((130, 110, "0" if wave else "1"), (145, 90, "1" if wave else "N"), (160, 109, "2"))]
        events.append({"id": identifier, "evidence_id": identifier, "pattern": identifier, "name": name, "group": catalog[-1]["group"],
            "experimental": wave, "phase": "FORMING" if wave else "CONFIRMED" if index % 2 == 0 else "FORMED", "confidence": None if wave else .75,
            "direction": "bullish", "signal_direction": "neutral" if wave else "bearish", "start_time": points[0]["time"], "end_time": points[-1]["time"],
            "points": points, "lines": [{"from": a, "to": b, "style": "dashed" if wave else "solid"} for a, b in zip(points, points[1:])],
            "historical_reference":{"rate":.6,"sample_count":25,"interval":{"lower":.25,"upper":.85},
                "formation":{"rate":.8,"probability":.8,"sample_count":25,"interval":{"lower":.4,"upper":.98}},
                "recorded_count":5,"replay_count":20,"invalidated":6,"expired":4,
                "failure_examples":[{"context_id":"a"*64,"id":"b"*64,"source":"RECORDED","phase":"INVALIDATED","observed_time":points[0]["time"]-3600}]},
            "stage": "已识别至 2 · 后续浪未形成" if wave else "结构已形成 · 等待确认", "note": "测试结构"})
    current = {"items": events, "catalog": catalog, "as_of": events[0]["end_time"] + step, "bars_scanned": 180, "version": "fixture"}
    for e in events:
        f={"id":"initial-"+e["id"],"kind":"NEXT_PIVOT","issued_at":current["as_of"]-5*step,"deadline_time":current["as_of"]+40*step,
            "source":"THEORY_ONLY","history_count":0,"status":"PENDING","condition":"第二拐点进入候选区后，等待右侧 4 根枢轴确认",
            "confirmation_closes":2,"confirmation":[{"side":-1,"price":89.8}],
            "target":{"price":108.7,"lower":108,"upper":109.5,"theory_price":108.7,"label":"第二拐点候选区"}}
        e.update(initial_forecast=f,forecast={**f,"id":"revised-"+e["id"],"issued_at":current["as_of"],"source":"PRIOR_COMPLETED_LEGS_SHRUNK",
            "history_count":25,"target":{**f["target"],"price":109.2}},forecast_outcome={"status":"PENDING"})
    rows = [{"pattern": ident, "phase": phase, "horizon": h, "detected": 48, "sample_count": 23,
        "pending": 3, "overlap_excluded": 22, "neutral_excluded": 0, "win_rate": .65, "interval": {"lower": .3, "upper": .9},
        "mean_net_return": .02, "mean_mfe": .05, "mean_mae": -.03, "recent_samples": [{"observed_time": current["as_of"],
        "entry_time": current["as_of"], "exit_time": current["as_of"] + step * h, "side": -1, "net_return": .02, "evidence_id": "fixture"}]}
        for ident, _ in NAMES for phase in ("FORMED", "CONFIRMED") for h in (5, 10, 20, 50)]
    report = {"version": "fixture", "bar_count": 8000, "from_time": int(origin.timestamp()), "to_time": current["as_of"], "rows": rows,
        "round_trip_cost": .001, "metadata": {"snapshot_at": origin.isoformat(), "quality_flags": [], "input_digest": "fixture"}}
    report["completion_rows"]=[{"pattern":ident,"phase":phase,"score_bucket":bucket,"total":50,"FORMING":5,"FORMED":5,"CONFIRMED":20,
        "INVALIDATED":10,"EXPIRED":8,"CENSORED":2,"pending":10,"formed_count":30,"overlap_excluded":10,"sample_count":25,"successes":15,
        "rate":.6,"interval":{"lower":.25,"upper":.85},"calibration_count":0,"brier":None}
        for ident,_ in NAMES for phase in ("FORMING","FORMED") for bucket in ("ALL","UNSCORED","LOW","MID","HIGH")]
    report["metadata"].update(period_days=365,target_bars=35040,missing_bars=27040,coverage_ratio=8000/35040,mode="FULL")
    report["forecast_outcomes"]={"TOUCHED":20,"NOT_REACHED":10,"PENDING":5,"AMBIGUOUS":2}
    # More than the old 96 cap; unique candidate identities, not inflated counts.
    base=list(events)
    for i in range(len(events),125):
        e=base[i%len(base)]
        events.append({**e,"id":f"fixture-{i}","evidence_id":f"fixture-{i}","name":f"{e['name']} · {i}"})
    current.update(total=len(events),tracked_total=len(events),offset=0,limit=12,
        phase_counts={phase:sum(e["phase"]==phase for e in events) for phase in ("FORMING","FORMED","CONFIRMED","INVALIDATED","EXPIRED","CENSORED")})
    return bars, current, report


def main():
    expect.set_options(timeout=12000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("BROWSER_EXECUTABLE"),
            args=["--renderer-process-limit=1", "--disable-dev-shm-usage", "--js-flags=--max-old-space-size=64"])
        try:
            for width in (1440, 390):
                page = browser.new_page(viewport={"width": width, "height": 950})
                errors, held = [], []
                state = {"submitted": False, "polls": 0, "hold_current": "", "failed": False, "posts": 0, "requested_at": datetime.now(timezone.utc).isoformat()}
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))

                def handle(route, request):
                    url = urlsplit(request.url)
                    path, query = url.path, parse_qs(url.query)
                    period = query.get("timeframe", ["15m"])[0]
                    bars, current, report = fixtures(period)
                    if path == "/auth/me":
                        payload = {"user": {"id": "fixture", "tenant_id": "fixture", "username": "test", "role": "VIEWER"}, "mfa_required": False}
                    elif path == "/api/v2/instruments":
                        payload = {"items": [{"id": INSTRUMENT, "symbol": "BTC_USDT", "market": "futures_usdt", "settle": "usdt", "status": "ACTIVE"}]}
                    elif path == "/api/v2/watchlists":
                        payload = {"items": [{"id": "watch", "etag": 1, "name": "我的自选", "items": [{"instrument_id": INSTRUMENT, "symbol": "BTC_USDT", "default_timeframe": "15m"}]}]}
                    elif path == "/api/v2/bars":
                        payload = {"items": bars, "quality_flags": [], "data_watermark": bars[-1]["open_time"]}
                    elif path == "/api/v2/patterns/current":
                        if state["hold_current"] == period:
                            held.append((route, current))
                            return
                        if state["failed"]:
                            route.fulfill(status=503, json={"detail": "结构读取暂时失败"})
                            return
                        offset=int(query.get("offset",["0"])[0]);size=int(query.get("limit",["12"])[0])
                        family=query.get("family",[""])[0];phase=query.get("phase",[""])[0];needle=query.get("q",[""])[0]
                        matches=[e for e in current["items"] if (not family or (family=="wave")==bool(e.get("experimental")))
                            and (not phase or e["phase"]==phase) and needle in e["name"]]
                        payload={**current,"items":matches[offset:offset+size],"total":len(matches),"offset":offset,"limit":size}
                    elif path == "/api/v2/patterns/study":
                        if request.method == "POST":
                            state["posts"] += 1
                            held.append((route, {"state": "QUEUED", "cursor": 0, "total": 0, "report": None}))
                            return
                        if not state["submitted"]:
                            payload = {"state": "NOT_REQUESTED", "cursor": 0, "total": 0, "report": None}
                        else:
                            state["polls"] += 1
                            ready = state["polls"] >= 2
                            payload = {"state": "READY" if ready else "RUNNING", "cursor": 8000 if ready else 1234, "total": 8000, "report": report if ready else None, "requested_at": state["requested_at"]}
                    elif path == "/api/v2/patterns/events":
                        offset=int(query.get("offset",["0"])[0])
                        items=[{"context_id":"a"*64,"id":f"{i:064x}","phase":"INVALIDATED","name":"双顶","pattern":"double_top",
                            "first_observed_time":current["as_of"]-3600,"observed_time":current["as_of"],"deadline_time":current["as_of"]+1000,
                            "reason":"触及预设结构失效条件","sequence":2} for i in range(14)]
                        payload={"items":items[offset:offset+12],"total":14,"offset":offset,"limit":12,"contexts":[]}
                    elif path == "/api/v2/patterns/watchlist-progress":
                        offset=int(query.get("offset",["0"])[0]);limit=int(query.get("limit",["24"])[0])
                        items=[{"instrument_id":str(i),"symbol":symbol,"timeframe":tf,"cached_bars":min(target,8000),"target_bars":target,
                            "missing_bars":max(0,target-8000),"coverage_ratio":min(1,8000/target),"history_status":"BACKFILLING" if target>8000 else "READY",
                            "state":"RUNNING" if target>8000 else "READY","cursor":min(target,1234),"total":min(target,8000)}
                            for i,symbol in enumerate(("BTC_USDT","ETH_USDT","SOL_USDT","DOGE_USDT"))
                            for tf,target in (("1m",525600),("5m",105120),("15m",35040),("30m",17520),("1h",8760),("4h",2190),("8h",1095),("1d",365))]
                        payload={"items":items[offset:offset+limit],"total":32,"offset":offset,"limit":limit}
                    elif path.startswith("/api/v2/patterns/events/"):
                        first={**current["items"][0],"phase":"FORMING","stage":"首次识别，固定初始条件"}
                        last={**first,"phase":"INVALIDATED","stage":"触及预设结构失效条件"}
                        payload={"source_mode":"REPLAY","original_through":current["as_of"]-3600,
                            "bars":[{"ts":int(datetime.fromisoformat(b["open_time"]).timestamp()),**{k:float(b[k]) for k in ("open","high","low","close","volume")}} for b in bars],
                            "transitions":[{"recorded_at":"2026-09-10T00:00:00Z","record":{"phase":e["phase"],"first":first,"latest":e,
                                "observed_time":current["as_of"]-3600+3600*i,"sequence":i+1,"first_observed_time":current["as_of"]-3600,
                                "deadline_time":current["as_of"]+3600,"stages":{"FORMING":{"prediction":{"probability":None,"sample_count":0}}}}} for i,e in enumerate((first,last))]}
                    elif path.startswith(("/api/", "/auth/")):
                        payload = {"items": [], "attention_count": 0}
                    else:
                        file = DIST / path.lstrip("/") if path.startswith("/assets/") else DIST / "index.html"
                        route.fulfill(status=200, content_type=mimetypes.guess_type(file.name)[0] or "text/plain", body=file.read_bytes())
                        return
                    route.fulfill(status=200, json=payload)

                page.route("**/*", handle)
                page.goto("http://pattern.test/markets/chart")
                panel = page.get_by_role("region", name="形态与波浪", exact=True)
                expect(panel).to_be_visible()
                expect(panel.locator('.structure-confidence').first).to_be_visible()
                assert panel.locator('.structure-confidence').first.evaluate("el=>getComputedStyle(el).display")=='grid'
                expect(panel.get_by_role("button", name="叠加 双顶 已确认", exact=True)).to_be_enabled()
                panel.get_by_role("button",name="自选历史同步进度").click()
                annual=page.get_by_role("dialog")
                expect(annual).to_contain_text("525,600")
                annual.get_by_role("button",name="自选历史同步下一页").click()
                expect(annual).to_contain_text("DOGE_USDT")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.screenshot(path=f"/tmp/gatescope-annual-history-{width}.png")
                annual.get_by_role("button",name="关闭弹窗").click()
                assert panel.bounding_box()["y"] >= page.locator(".chart-panel").bounding_box()["y"] + page.locator(".chart-panel").bounding_box()["height"]
                assert panel.bounding_box()["y"] - page.locator(".chart-panel").bounding_box()["y"] - page.locator(".chart-panel").bounding_box()["height"] < 110, "empty cache status must not leave an empty grid row"
                panel.get_by_role("button", name="叠加 双顶 已确认", exact=True).click()
                expect(page.locator(".chart-pattern-legend [data-line-count='2']")).to_have_count(1)
                expect(panel).to_contain_text("跟踪 125 个结构")
                panel.get_by_role("button", name="当前结构下一页").click()
                expect(panel.locator(".pattern-card")).to_have_count(12)
                expect(panel.get_by_role("button",name="叠加 双顶 已确认",exact=True)).to_have_count(0)
                panel.get_by_role("button",name="当前结构上一页").click()
                panel.get_by_role("button", name="叠加 艾略特上升五浪 形成中", exact=True).click()
                expect(page.locator(".chart-pattern-legend > span")).to_have_count(2)
                expect(panel).to_contain_text("实盘观测 5 · 历史回放 20 · 失效 6 · 到期 4")
                panel.get_by_text("艾略特上升五浪 · 历史失败案例",exact=True).click()
                panel.get_by_role("button",name=re.compile("实测 · .* · 查看原图")).first.click()
                failure=page.get_by_role("dialog")
                expect(failure).to_contain_text("未替换原始 K 线版本")
                failure.get_by_label("候选事件时间线").select_option("1")
                expect(failure).to_contain_text("触及预设结构失效条件")
                expect(failure.locator("canvas").first).to_be_visible()
                page.screenshot(path=f"/tmp/gatescope-failure-memory-{width}.png")
                failure.get_by_role("button",name="关闭弹窗").click()
                panel.get_by_label("形态类别").select_option("wave")
                expect(panel.locator(".pattern-card")).to_have_count(12)
                expect(panel.get_by_role("button",name="叠加 双顶 已确认",exact=True)).to_have_count(0)
                panel.get_by_label("搜索形态或波浪").fill("不存在的结构")
                expect(panel).to_contain_text("筛选 0 / 跟踪 125")
                panel.get_by_label("搜索形态或波浪").fill("")
                panel.get_by_label("形态类别").select_option("all")
                expect(panel).to_contain_text("筛选 125 / 跟踪 125")
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=f"/tmp/gatescope-pattern-overlay-{width}.png", full_page=True)
                panel.get_by_role("tab", name="历史表现", exact=True).click()
                panel.get_by_role("button", name="计算历史表现", exact=True).click()
                expect(panel.get_by_role("button", name="提交中…", exact=True)).to_be_disabled()
                assert state["posts"] == 1
                for route, data in held:
                    route.fulfill(status=202, json=data)
                held.clear()
                state["submitted"] = True
                expect(panel.get_by_role("status")).to_contain_text("已排队")
                expect(panel.get_by_role("region", name="形态历史表现表")).to_be_visible(timeout=15000)
                expect(panel.get_by_role("region", name="候选完成统计表")).to_contain_text("10 / 8")
                panel.get_by_label("候选统计阶段").select_option("FORMED")
                panel.get_by_label("初始匹配分组").select_option("MID")
                expect(panel.get_by_role("region", name="候选完成统计表").get_by_text("60.0%",exact=True)).to_have_count(8)
                expect(panel.get_by_role("button", name=re.compile("秒后可更新"))).to_be_disabled()
                expect(panel.get_by_text("65.0%", exact=True)).to_have_count(8)
                panel.get_by_role("button", name="历史表现下一页").click()
                expect(panel.get_by_text("65.0%", exact=True)).to_have_count(1)
                panel.get_by_label("统计样本组").select_option("FORMED")
                expect(panel.get_by_text("65.0%", exact=True)).to_have_count(8)
                panel.get_by_role("button", name="查看 双顶 统计明细", exact=True).click()
                expect(page.get_by_role("dialog")).to_contain_text("研究入场")
                expect(page.get_by_role("dialog")).to_contain_text("首次识别")
                page.get_by_role("button", name="关闭弹窗").click()
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=f"/tmp/gatescope-pattern-stats-{width}.png", full_page=True)
                panel.get_by_role("tab",name="候选档案",exact=True).click()
                panel.get_by_label("档案来源").select_option("REPLAY")
                panel.get_by_label("档案状态").select_option("INVALIDATED")
                expect(panel.get_by_role("button",name="查看 双顶 候选档案",exact=True)).to_have_count(12)
                panel.get_by_role("button",name="候选档案下一页",exact=True).click()
                expect(panel.get_by_role("button",name="查看 双顶 候选档案",exact=True)).to_have_count(2)
                panel.get_by_role("button",name="查看 双顶 候选档案",exact=True).first.click()
                dialog=page.get_by_role("dialog")
                expect(dialog).to_contain_text("首次预测 108.7")
                expect(dialog).to_contain_text("109.2")
                expect(dialog).to_contain_text("连续 2 根")
                expect(dialog.get_by_label("候选事件时间线")).to_have_value("0")
                expect(dialog).to_contain_text("未替换原始 K 线版本")
                dialog.get_by_label("候选事件时间线").select_option("1")
                expect(dialog).to_contain_text("触及预设结构失效条件")
                expect(dialog.locator("canvas").first).to_be_visible()
                page.screenshot(path=f"/tmp/gatescope-pattern-evidence-{width}.png")
                page.get_by_role("button",name="关闭弹窗").click()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                state["hold_current"] = "4h"
                page.get_by_role("button", name="4h", exact=True).click()
                expect(page.locator(".chart-pattern-legend")).to_have_count(0)
                page.wait_for_timeout(300)
                page.get_by_role("button", name="1h", exact=True).click()
                for route, data in held:
                    route.fulfill(json=data)
                held.clear()
                state["hold_current"] = ""
                expect(panel.locator("header").first).to_contain_text("1h")
                expect(panel.get_by_role("button", name="叠加 双顶 已确认", exact=True)).to_be_enabled()
                state["failed"] = True
                page.evaluate("window.dispatchEvent(new Event('online'))")
                expect(panel.get_by_role("alert")).to_contain_text("结构读取暂时失败")
                state["failed"] = False
                panel.get_by_role("button", name="重新加载", exact=True).click()
                expect(panel.get_by_role("alert")).to_have_count(0)
                assert not errors, errors
                print(json.dumps({"width": width, "overlays": "ok", "pagination": "ok", "async_study": "ok", "cohorts": "ok", "cycle_race": "ok", "recovery": "ok"}), flush=True)
                page.close()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
