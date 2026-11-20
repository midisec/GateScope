"""Bounded read-only menus. Callback payloads point to owner-bound saved actions."""

import re
from uuid import UUID
from gatescope.application.telegram_paper import paper_callback, paper_command

PERIODS = ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d")
PHASES = {
    "FORMING": "形成中",
    "FORMED": "已形成",
    "CONFIRMED": "已确认",
    "INVALIDATED": "已失效",
    "EXPIRED": "已到期",
    "CENSORED": "不计成败",
}
STATIC_ACTIONS = {
    "gm:opportunities": {"action": "opportunities", "coin_offset": 0, "offset": 0, "mode": "strict"},
    "gm:watchlist": {"action": "watchlist", "offset": 0},
}


def notification_market_buttons(alert_id=None):
    # These contain NO tenant/user/instrument/order data. The webhook resolves
    # the currently bound private sender and searches only that user's watchlist.
    rows = []
    if alert_id:
        try:
            identifier = UUID(str(alert_id)).hex
        except (TypeError, ValueError, AttributeError):
            identifier = None
        if identifier:
            rows.append(
                [
                    {"text": "通知原图 / 做单计划", "callback_data": f"gm:signal:{identifier}"},
                    {"text": "最新形态状态", "callback_data": f"gm:signalstate:{identifier}"},
                ]
            )
    rows.append(
        [
            {"text": "筛选高置信形态", "callback_data": "gm:opportunities"},
            {"text": "我的自选形态", "callback_data": "gm:watchlist"},
        ]
    )
    return {"inline_keyboard": rows}


def signal_callback(data):
    match = re.fullmatch(r"gm:(signal|signalstate):([a-f0-9]{32})", data) if isinstance(data, str) else None
    return (
        {"action": "signal", "event_id": str(UUID(match[2])), "latest": match[1] == "signalstate"}
        if match
        else None
    )


def market_command(text):
    paper = paper_command(text)
    if paper is not None:
        return paper
    parts = text.split()
    name = parts[0].split("@", 1)[0].lower() if parts else ""
    if name not in {"/start", "/menu", "/watchlist", "/patterns", "/chart", "/indicators", "/opportunities"}:
        return None
    if name == "/start" and len(parts) > 1:
        return None
    if name == "/opportunities":
        if len(parts) > 1:
            raise ValueError("使用 /opportunities 打开筛选，再点击按钮切换。")
        return dict(STATIC_ACTIONS["gm:opportunities"])
    if len(parts) == 1:
        return {"action": "watchlist", "offset": 0}
    if name not in {"/patterns", "/chart", "/indicators"} or len(parts) > 3:
        raise ValueError("使用 /chart BTC 1h，或 /watchlist 打开按钮菜单。")
    symbol = parts[1].upper()
    tf = parts[2] if len(parts) == 3 else "1h"
    if not re.fullmatch(r"[A-Z0-9_/-]{1,32}", symbol) or tf not in PERIODS:
        raise ValueError("周期支持 1m、5m、15m、30m、1h、4h、8h、1d。")
    return {
        "action": "pattern_overview" if name == "/patterns" else "overview",
        "symbol": symbol,
        "timeframe": tf,
    }


def callback_update(payload):
    q = payload.get("callback_query") if isinstance(payload, dict) else None
    if not isinstance(q, dict):
        return None
    message = q.get("message")
    sender = q.get("from")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        raise ValueError("invalid callback")
    chat = message.get("chat", {})
    user = sender.get("id")
    update = payload.get("update_id")
    data = q.get("data")
    qid = q.get("id")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2**63 for v in (user, update)):
        raise ValueError("invalid callback identity")
    if (
        chat.get("type") != "private"
        or chat.get("id") != user
        or not isinstance(data, str)
        or not (
            data in STATIC_ACTIONS
            or signal_callback(data)
            or paper_callback(data)
            or re.fullmatch(r"gm:[a-f0-9]{32}:\d{1,2}", data)
        )
    ):
        raise ValueError("invalid private callback")
    if not isinstance(qid, str) or not 1 <= len(qid) <= 256:
        raise ValueError("invalid callback id")
    return update, user, user, sender.get("username"), data, qid


class Menu:
    def __init__(self, identifier):
        self.id = identifier
        self.actions = []
        self.rows = []

    def row(self, *buttons):
        row = []
        for label, command in buttons:
            index = len(self.actions)
            self.actions.append(command)
            row.append({"text": label[:64], "callback_data": f"gm:{self.id.hex}:{index}"})
        if row:
            self.rows.append(row)

    def result(self, text, chart=None):
        assert len(self.actions) <= 99
        return {
            "text": text[:1000] if chart else text[:4000],
            "reply_markup": {"inline_keyboard": self.rows},
            "actions": self.actions,
            "chart": chart,
        }


def page_offset(offset, total, size):
    return min(max(0, int(offset)), max(0, (total - 1) // size * size))


def build_market_reply(repository, tenant, user, identifier, command):
    """Only repository-owned watchlist entries can reach charts or evidence."""
    menu = Menu(identifier)
    action = command["action"]
    if action in {
        "paper_accounts",
        "paper_account",
        "paper_orders",
        "paper_order",
        "paper_positions",
        "paper_chart",
    }:
        from gatescope.application.telegram_paper import build_paper_reply

        return build_paper_reply(repository, tenant, user, menu, command)
    if action == "signal":
        signal = repository.owned_signal(tenant, user, command.get("event_id"))
        if signal is None:
            return menu.result("该通知事件不可用，或不属于你的账户。")
        chart, description = repository.signal_chart(signal, latest=bool(command.get("latest")))
        response = menu.result(description, chart)
        response["reply_markup"] = notification_market_buttons(signal["id"])
        return response
    if action == "opportunities":
        return opportunity_reply(repository, tenant, user, menu, command)
    if action == "watchlist":
        offset = max(0, int(command.get("offset", 0)))
        items, total = repository.watchlist(tenant, user, offset, 8)
        clamped = page_offset(offset, total, 8)
        if clamped != offset:
            offset = clamped
            items, total = repository.watchlist(tenant, user, offset, 8)
        for item in items:
            menu.row(
                (
                    item["symbol"],
                    {
                        "action": "pattern_overview",
                        "instrument_id": item["id"],
                        "timeframe": item["timeframe"],
                    },
                )
            )
        menu.row(("模拟账户 / 持仓", {"action": "paper_accounts", "offset": 0}))
        menu.row(("筛选高置信形态", dict(STATIC_ACTIONS["gm:opportunities"])))
        nav = []
        if offset:
            nav.append(("‹ 上一页", {"action": "watchlist", "offset": max(0, offset - 8)}))
        if offset + len(items) < total:
            nav.append(("下一页 ›", {"action": "watchlist", "offset": offset + 8}))
        menu.row(*nav)
        return menu.result(
            f"我的自选 · {total} 个\n选择币种，查看形态、波浪和指标图。\n后台持续更新，无需保持网页打开。"
            if total
            else "自选列表为空，请先在网页行情页添加币种。"
        )
    instrument = repository.owned_instrument(
        tenant, user, command.get("instrument_id"), command.get("symbol")
    )
    if instrument is None:
        menu.row(("返回我的自选", {"action": "watchlist", "offset": 0}))
        return menu.result("这个币种不在你的自选中，或名称有多个匹配。请从自选菜单选择。")
    iid = instrument["id"]
    tf = command.get("timeframe", "1h")
    if tf not in PERIODS:
        raise ValueError("unsupported period")
    base = {"instrument_id": iid, "timeframe": tf}
    home = {"action": "pattern_overview", **base}
    if action in ("overview", "pattern_overview"):
        state = repository.tracking(iid, tf)
        records = list(state.get("records", {}).values())
        counts = {p: sum(r["phase"] == p for r in records) for p in PHASES}
        for begin in (0, 4):
            menu.row(
                *[
                    (("✓ " if p == tf else "") + p, {"action": action, "instrument_id": iid, "timeframe": p})
                    for p in PERIODS[begin : begin + 4]
                ]
            )
        menu.row(
            *[
                (f"{PHASES[p]} {counts[p]}", {"action": "patterns", **base, "phase": p, "offset": 0})
                for p in ("FORMING", "FORMED", "CONFIRMED")
            ]
        )
        menu.row(
            (
                "查看指标图" if action == "pattern_overview" else "查看形态",
                {"action": "overview" if action == "pattern_overview" else "pattern_overview", **base},
            ),
            ("返回自选", {"action": "watchlist", "offset": 0}),
        )
        menu.row(("筛选高置信形态", dict(STATIC_ACTIONS["gm:opportunities"])))
        chart = repository.overview_chart(instrument, tf, patterns_only=action == "pattern_overview")
        status = repository.cache_status(iid, tf)
        note = "行情延迟，当前显示本地缓存。" if status.get("stale", True) else "后台行情同步正常。"
        text = f"{instrument['symbol']} · {tf}\n形成中 {counts['FORMING']} · 已形成 {counts['FORMED']} · 已确认 {counts['CONFIRMED']}\n{note}\n{chart['summary']}\n选择状态，再点形态查看原理线和首次预测。\n指标仅按已收盘 K 线计算；形态确认不代表盈利。"
        return menu.result(text, chart)
    if action == "patterns":
        phase = command.get("phase")
        offset = max(0, int(command.get("offset", 0)))
        if phase not in PHASES:
            raise ValueError("unsupported phase")
        state = repository.tracking(iid, tf)
        records = sorted(
            (r for r in state.get("records", {}).values() if r["phase"] == phase),
            key=lambda r: (r["observed_time"], r["id"]),
            reverse=True,
        )
        offset = page_offset(offset, len(records), 6)
        page = records[offset : offset + 6]
        return_to = {"action": "patterns", **base, "phase": phase, "offset": offset}
        for record in page:
            menu.row(
                (
                    f"{record['latest']['name']} · {PHASES[record['phase']]}",
                    {
                        "action": "detail",
                        **base,
                        "candidate": record["id"],
                        "initial": False,
                        "return_to": return_to,
                    },
                )
            )
        nav = []
        if offset:
            nav.append(
                ("‹ 上一页", {"action": "patterns", **base, "phase": phase, "offset": max(0, offset - 6)})
            )
        if offset + len(page) < len(records):
            nav.append(("下一页 ›", {"action": "patterns", **base, "phase": phase, "offset": offset + 6}))
        menu.row(*nav)
        menu.row(("返回币种 / 切周期", home), ("我的自选", {"action": "watchlist", "offset": 0}))
        menu.row(
            *[
                (PHASES[p], {"action": "patterns", **base, "phase": p, "offset": 0})
                for p in ("FORMING", "FORMED", "CONFIRMED")
                if p != phase
            ]
        )
        return menu.result(
            f"{instrument['symbol']} · {tf} · {PHASES[phase]}\n共 {len(records)} 个，当前第 {offset // 6 + 1} 页。\n点击查看带原理线的原始 K 线图。"
            if records
            else f"{instrument['symbol']} · {tf}：当前跟踪窗口没有{PHASES[phase]}的结构。\n不会为凑数量生成形态。"
        )
    if action == "detail":
        candidate = str(command.get("candidate", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", candidate):
            raise ValueError("invalid candidate")
        initial = bool(command.get("initial"))
        chart, description = repository.evidence_chart(instrument, tf, candidate, initial)
        return_to = command.get("return_to")
        context = (
            {"return_to": return_to}
            if isinstance(return_to, dict) and return_to.get("action") in ("patterns", "opportunities")
            else {}
        )
        menu.row(
            ("首次识别图", {"action": "detail", **base, "candidate": candidate, "initial": True, **context}),
            ("最新状态图", {"action": "detail", **base, "candidate": candidate, "initial": False, **context}),
        )
        if context:
            menu.row(
                ("返回筛选结果" if return_to["action"] == "opportunities" else "返回形态列表", return_to)
            )
        menu.row(("返回币种 / 切周期", home), ("我的自选", {"action": "watchlist", "offset": 0}))
        menu.row(("筛选高置信形态", dict(STATIC_ACTIONS["gm:opportunities"])))
        return menu.result(description, chart)
    raise ValueError("unsupported market action")


def opportunity_reply(repository, tenant, user, menu, command):
    from gatescope.application.telegram_chart import stamp
    from gatescope.quant.structure_screening import REASONS

    coin_offset = max(0, int(command.get("coin_offset", 0)))
    offset = max(0, int(command.get("offset", 0)))
    mode = command.get("mode", "strict")
    if mode not in ("strict", "observe"):
        raise ValueError("unsupported screening mode")
    result = repository.opportunities(tenant, user, coin_offset)
    clamped = page_offset(coin_offset, result["total_coins"], 2)
    if clamped != coin_offset:
        coin_offset = clamped
        result = repository.opportunities(tenant, user, coin_offset)
    selected = [r for r in result["items"] if r["qualified"] == (mode == "strict")]
    # Recompute live data on each click. Clamp a shrinking result to its last page.
    offset = page_offset(offset, len(selected), 5)
    base = {"action": "opportunities", "coin_offset": coin_offset, "mode": mode}
    strict = sum(r["qualified"] for r in result["items"])
    coins = "、".join(r["symbol"].split("_")[0] for r in result["coins"]) or "无自选"
    text = [
        f"{'高置信形态筛选' if mode == 'strict' else '观察候选 · 未达标'} · {stamp(result['as_of'])}",
        f"范围：{coins} · 8 个周期；自选第 {coin_offset + 1 if result['coins'] else 0}–{coin_offset + len(result['coins'])}/{result['total_coins']} 个",
        f"本组达标 {strict} · 观察 {len(result['items']) - strict} · 行情/识别未就绪 {result['pending_pairs']} 个周期",
    ]
    studies = result.get("study_states", {})
    text.append(
        f"历史回放：已完成 {studies.get('READY', 0)} · 进行中 {studies.get('RUNNING', 0)} · 等待/异常 {sum(v for k, v in studies.items() if k not in ('READY', 'RUNNING'))}"
    )
    capacity_limited = sum(
        item["statistics"].get("sample_requirement_reachable") is False for item in result["items"]
    )
    if capacity_limited:
        text.append(
            f"其中 {capacity_limited} 个候选受历史窗口容量限制，无法积累所需样本；可在「观察候选」查看。"
        )
    if not selected:
        text.append(
            "当前本组没有达标形态。可切换「观察候选」查看未达标原因。"
            if mode == "strict"
            else "当前本组没有可用的未达标候选。"
        )
    for index, item in enumerate(selected[offset : offset + 5], offset + 1):
        stats = item["statistics"]
        interval = stats["interval"]
        rate = stats["win_rate"]
        heading = f"{index}. {item['symbol']} · {item['timeframe']} · {item['name']}"
        text.append("\n" + heading)
        text.append(
            f"{PHASES[item['phase']]} · {'看涨' if item['direction'] == 'bullish' else '看跌' if item['direction'] == 'bearish' else '待定'} · 几何 {item['geometry'] * 100:.0f} 分（非概率）"
            if item["geometry"] is not None
            else f"{PHASES[item['phase']]} · 几何待完成"
        )
        if item["phase"] == "FORMING":
            text.append("形成中：不套用形成后的入场胜率；点图查看首次形成概率。")
        else:
            text.append(
                f"历史净胜率 {rate:.1%} · n={stats['sample_count']}"
                if rate is not None
                else "历史净胜率：暂无有效样本"
            )
            if interval:
                text.append(
                    f"95%校正区间 {interval['lower']:.1%}–{interval['upper']:.1%} · 平均净收益 {stats['mean_net_return']:.2%}"
                )
        if not item["qualified"]:
            reasons = item["reasons"][:1] if item["phase"] == "FORMING" else item["reasons"]
            text.append("未达标：" + "；".join(REASONS[r] for r in reasons))
        if stats.get("sample_requirement_reachable") is False:
            text.append(
                f"当前历史窗口最多约 {stats['maximum_independent_samples']} 个独立样本，无法满足 100；继续作为观察候选。"
            )
        menu.row(
            (
                f"{index} · {item['symbol'].split('_')[0]} {item['timeframe']} · {item['name']} · 看图",
                {
                    "action": "detail",
                    "instrument_id": item["instrument_id"],
                    "timeframe": item["timeframe"],
                    "candidate": item["candidate"],
                    "initial": False,
                    "return_to": {**base, "offset": offset},
                },
            )
        )
    nav = []
    if offset:
        nav.append(("‹ 上一页", {**base, "offset": offset - 5}))
    if offset + 5 < len(selected):
        nav.append(("下一页 ›", {**base, "offset": offset + 5}))
    menu.row(*nav)
    menu.row(
        (f"达标 {strict}", {**base, "mode": "strict", "offset": 0}),
        (f"观察候选 {len(result['items']) - strict}", {**base, "mode": "observe", "offset": 0}),
    )
    groups = []
    if coin_offset:
        groups.append(("‹ 上一组币种", {**base, "coin_offset": max(0, coin_offset - 2), "offset": 0}))
    if coin_offset + len(result["coins"]) < result["total_coins"]:
        groups.append(("下一组币种 ›", {**base, "coin_offset": coin_offset + 2, "offset": 0}))
    menu.row(*groups)
    menu.row(("刷新筛选", {**base, "offset": offset}), ("我的自选", {"action": "watchlist", "offset": 0}))
    text.extend(
        [
            "\n门槛：净胜率 >55%，n≥100，校正区间下限 >50%、宽度≤30%，平均净收益 >0，几何≥70分。",
            "口径：同币种/周期/形态/阶段/方向；过去一年、结构起点前的非重叠回放，次根开盘入场、持有20根，双边费用及滑点合计0.10%。",
            "仅筛选研究线索，不是当前交易胜率或策略部署许可；未检验资金费、夏普与回撤。新确认只保留2根内，未确认的不视为入场信号。",
        ]
    )
    response = menu.result("\n".join(text))
    response["scope_instruments"] = [r["id"] for r in result["coins"]]
    return response
