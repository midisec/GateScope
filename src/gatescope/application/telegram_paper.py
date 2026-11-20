"""Read-only PAPER account menus; opaque IDs are never authorization."""
import re
from uuid import UUID

STATIC_PAPER_ACTIONS = {
    "gp:accounts": {"action": "paper_accounts", "offset": 0},
    "gp:orders": {"action": "paper_orders", "offset": 0},
    "gp:positions": {"action": "paper_positions", "offset": 0, "state": "OPEN"},
}
PAPER_READ_ACTIONS = frozenset({"paper_accounts", "paper_account", "paper_orders", "paper_order", "paper_positions", "paper_chart"})


def paper_callback(data):
    if not isinstance(data, str):
        return None
    if data in STATIC_PAPER_ACTIONS:
        return dict(STATIC_PAPER_ACTIONS[data])
    match = re.fullmatch(r"gp:(order|account|positions):([a-f0-9]{32})", data)
    if not match:
        return None
    return {"action": "paper_" + match[1], "order_id" if match[1] == "order" else "account_id": str(UUID(match[2])), "offset": 0}


def paper_command(text):
    parts = text.split()
    name = parts[0].split("@", 1)[0].lower() if parts else ""
    action = {"/trades": "paper_orders", "/orders": "paper_orders", "/positions": "paper_positions",
              "/accounts": "paper_accounts", "/balance": "paper_accounts", "/order": "paper_order"}.get(name)
    if action is None:
        return None
    if name == "/order":
        if len(parts) != 2:
            raise ValueError("使用 /order 订单编号，或 /trades 点击订单详情。")
        try:
            identifier = str(UUID(parts[1]))
        except ValueError:
            raise ValueError("订单编号格式不正确，请从 /trades 菜单选择。") from None
        return {"action": action, "order_id": identifier}
    if len(parts) != 1:
        raise ValueError("请直接发送 /accounts、/positions 或 /trades，再点击按钮选择账户。")
    return {"action": action, "offset": 0}


def paper_notification_buttons(payload):
    rows = []
    try:
        order = UUID(str(payload.get("order_id"))).hex
        account = UUID(str(payload.get("account_id"))).hex
    except (ValueError, TypeError, AttributeError):
        pass
    else:
        rows.append([{"text": "查看订单", "callback_data": "gp:order:" + order},
                     {"text": "当前持仓", "callback_data": "gp:positions:" + account}])
        rows.append([{"text": "账户余额", "callback_data": "gp:account:" + account}])
    rows.append([{"text": "我的模拟账户", "callback_data": "gp:accounts"},
                 {"text": "最近委托", "callback_data": "gp:orders"}])
    return {"inline_keyboard": rows}


def _value(value):
    return "未知" if value is None else str(value)


def _status(value):
    return {"OPEN": "持仓/挂单中", "CLOSED": "已平仓", "FILLED": "已成交", "PARTIALLY_FILLED": "部分成交",
            "CANCELED": "已撤销余量", "EXPIRED": "已到期", "REJECTED": "已拒绝"}.get(value, value)


def build_paper_reply(repository, tenant, user, menu, command):
    from gatescope.adapters.postgres_paper import PaperNotFound
    try:
        result = repository.paper_read(tenant, user, command)
    except PaperNotFound:
        menu.row(("我的模拟账户", dict(STATIC_PAPER_ACTIONS["gp:accounts"])))
        return menu.result("没有找到该模拟账户或订单，或它不属于当前绑定用户。")
    action = command["action"]
    account_id = command.get("account_id")
    offset = int(command.get("offset", 0))
    lines = ["PAPER 模拟账本 · 与网页 / MCP 共用"]
    if action == "paper_chart":
        from gatescope.application.paper_decision_chart import paper_decision_chart_spec
        from gatescope.application.paper_execution import PaperExecutionError
        menu.row(("返回订单", {"action": "paper_order", "order_id": result["id"]}))
        try:
            chart = paper_decision_chart_spec(result, pattern_index=command.get("pattern_index", 0))
        except PaperExecutionError as exc:
            return menu.result(exc.message)
        if chart["pattern_count"] > 1:
            menu.row(("下一个原始形态", {**command, "pattern_index": (chart["pattern_index"]+1) % chart["pattern_count"]}))
        text = f"{chart['symbol']} · {chart['timeframe']} · 开单分析原图\n快照 {chart['decision_snapshot_id']}\n{chart['summary']}\n只读取已冻结行情和归档形态，不补画后来才知道的信息。"
        return menu.result(text, chart)
    if action == "paper_accounts":
        lines.append("选择账户查看可用余额、持仓和委托。")
        for account in result["items"]:
            menu.row((account["name"], {"action": "paper_account", "account_id": account["id"]}))
        if not result["items"]:
            lines.append("尚无模拟账户，请在网页创建；这里不会自动创建账户。")
    elif action == "paper_account":
        balance = result["balance"]
        lines += [result["account"]["name"],
            f"现金 {_value(balance['cash_balance'])} USDT · 可用 {_value(balance['available_balance'])}",
            f"持仓抵押 {_value(balance['position_margin'])} · 挂单冻结 {_value(balance['order_reserved_margin'])}",
            f"浮盈亏 {_value(balance['unrealized_pnl'])} · 净值 {_value(balance['equity'])}",
            "浮盈亏若显示未知，表示没有足够新鲜的退出盘口，不等于零。"]
        menu.row(("当前持仓", {"action": "paper_positions", "account_id": account_id, "offset": 0}),
                 ("已平仓记录", {"action": "paper_positions", "account_id": account_id, "state": "CLOSED", "offset": 0}))
        menu.row(("账户委托", {"action": "paper_orders", "account_id": account_id, "offset": 0}),
                 ("刷新余额", command))
    elif action == "paper_order":
        order = result
        lines += [f"{order['symbol']} · {order['side']} · {'开仓' if order['effect']=='OPEN' else '平仓'}",
            f"状态 {_status(order['status'])} · 已成交 {order['filled_contracts']}/{order['contracts']} 张",
            f"成交均价 {_value(order['average_fill_price'])} · 原因 {_value(order.get('reason'))}",
            f"止损 {_value(order['stop_price'])} · 止盈 {_value(order['take_profit_price'])}",
            f"订单 {order['id']}", f"仓位 {_value(order.get('position_id'))}",
            f"创建 {order['created_at']}"]
        if order.get("decision_snapshot_id"):
            lines.append("已关联不可变决策快照：" + str(order["decision_snapshot_id"]))
            menu.row(("查看开单分析原图", {"action": "paper_chart", "order_id": order["id"], "pattern_index": 0}))
        else:
            lines.append("未关联分析证据，不将这笔订单归因于某个历史形态。")
        menu.row(("刷新订单", command), ("账户余额", {"action": "paper_account", "account_id": order["account_id"]}))
        menu.row(("当前持仓", {"action": "paper_positions", "account_id": order["account_id"], "offset": 0}))
    elif action == "paper_positions":
        lines.append("已平仓记录" if command.get("state") == "CLOSED" else "当前持仓（盘口撮合模型）")
        for position in result["items"]:
            lines += [f"\n{position['symbol']} · {position['side']} · {_status(position['status'])}",
                f"剩余 {position['remaining_contracts']} 张 · 入场 {_value(position['average_entry_price'])}",
                f"止损 {_value(position['stop_price'])} · 止盈 {_value(position['take_profit_price'])}",
                f"已实现净额 {_value(position.get('net_realized_pnl'))} · 保护 {_value(position.get('protection_status'))}"]
            menu.row((f"{position['symbol']} 开仓订单", {"action": "paper_order", "order_id": position["opening_order_id"]}))
        if not result["items"]:
            lines.append("当前筛选下没有仓位。")
        menu.row(("切换当前持仓" if command.get("state") == "CLOSED" else "查看已平仓", {
            **command, "state": "OPEN" if command.get("state") == "CLOSED" else "CLOSED", "offset": 0}))
    elif action == "paper_orders":
        lines.append("盘口模拟委托（含网页与 MCP）")
        for order in result["items"]:
            lines += [f"\n{order['symbol']} · {order['side']} · {'开仓' if order['effect']=='OPEN' else '平仓'} · {_status(order['status'])}",
                      f"已成交 {order['filled_contracts']}/{order['contracts']} 张 · 来源 {order['source']}"]
            menu.row((f"{order['symbol']} {'开仓' if order['effect']=='OPEN' else '平仓'}详情", {"action": "paper_order", "order_id": order["id"]}))
        if not result["items"]:
            lines.append("尚无盘口模拟委托。")
        if result.get("legacy"):
            lines.append("\n旧 K 线模型交易（独立记录，非盘口成交）")
            for trade in result["legacy"]:
                lines.append(f"{trade['symbol']} {trade['side']} · {trade['state']}\n/trade {trade['public_trade_id']}")
    if action in {"paper_orders", "paper_positions", "paper_accounts"}:
        nav = []
        if offset:
            nav.append(("‹ 上一页", {**command, "offset": max(0, offset-5)}))
        if result.get("has_more") and offset < 10000:
            nav.append(("下一页 ›", {**command, "offset": offset+5}))
        menu.row(*nav)
        lines.append(f"第 {offset//5+1} 页 · 每页最多 5 条")
    if action != "paper_accounts":
        menu.row(("我的模拟账户", dict(STATIC_PAPER_ACTIONS["gp:accounts"])),
                 ("最近委托", dict(STATIC_PAPER_ACTIONS["gp:orders"])))
    lines.append("仅查询，不开仓、平仓或修改止盈止损。")
    return menu.result("\n".join(lines))
