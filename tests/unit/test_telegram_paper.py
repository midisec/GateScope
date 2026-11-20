from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_paper import PaperNotFound
from gatescope.application.notifications import DeliveryOutcome
from gatescope.application.telegram_market import build_market_reply, callback_update, market_command
from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
from gatescope.application.telegram_paper import paper_callback, paper_notification_buttons


def test_paper_commands_and_notification_buttons_are_bounded_read_only():
    order, account = uuid4(), uuid4()
    assert market_command("/trades") == {"action": "paper_orders", "offset": 0}
    assert market_command("/positions@fixture_bot")["action"] == "paper_positions"
    assert market_command("/accounts")["action"] == "paper_accounts"
    assert market_command(f"/order {order}")["order_id"] == str(order)
    for bad in ("/order BTC", "/order", "/order " + "x"*300, "/positions another-user"):
        with pytest.raises(ValueError):
            market_command(bad)
    for bad in ("gp:close:"+order.hex, "gp:cancel:"+order.hex, "gp:order:../../private", "gp:accounts:admin"):
        assert paper_callback(bad) is None
    buttons = paper_notification_buttons({"order_id": str(order), "account_id": str(account)})["inline_keyboard"]
    for row in buttons:
        for button in row:
            data = button["callback_data"]
            assert len(data.encode()) <= 64 and paper_callback(data)
            update = {"update_id": 12, "callback_query": {"id": "bounded", "from": {"id": 42},
                "message": {"chat": {"id": 42, "type": "private"}}, "data": data}}
            assert callback_update(update)[1:3] == (42, 42)
            update["callback_query"]["message"]["chat"]["type"] = "group"
            with pytest.raises(ValueError):
                callback_update(update)


def test_mcp_only_orders_are_visible_and_old_model_is_not_misrepresented():
    tenant, user = uuid4(), uuid4()
    order = {"id": str(uuid4()), "symbol": "SOL_USDT", "side": "LONG", "effect": "OPEN",
             "status": "PARTIALLY_FILLED", "filled_contracts": 2, "contracts": 5, "source": "MCP"}
    class Repository:
        def paper_read(self, actual_tenant, actual_user, command):
            assert (actual_tenant, actual_user) == (tenant, user)
            return {"items": [order], "has_more": True, "legacy": [
                {"symbol": "BTC_USDT", "side": "SHORT", "state": "CLOSED", "public_trade_id": "paper_fixture"}]}
    reply = build_market_reply(Repository(), tenant, user, uuid4(), market_command("/trades"))
    assert "SOL_USDT" in reply["text"] and "2/5 张" in reply["text"] and "来源 MCP" in reply["text"]
    assert "旧 K 线模型交易" in reply["text"] and "非盘口成交" in reply["text"]
    assert "目前没有 PAPER" not in reply["text"]
    assert {"action": "paper_order", "order_id": order["id"]} in reply["actions"]
    assert any(action.get("offset") == 5 for action in reply["actions"])
    assert all(action["action"].startswith("paper_") for action in reply["actions"])
    assert reply["chart"] is None


def test_foreign_order_lookup_never_exposes_any_record():
    class Repository:
        def paper_read(self, *args):
            raise PaperNotFound("private-details")
    reply = build_market_reply(Repository(), uuid4(), uuid4(), uuid4(), {"action": "paper_order", "order_id": str(uuid4())})
    assert "不属于当前绑定用户" in reply["text"] and "private-details" not in reply["text"]


def test_account_unknown_valuation_stays_unknown_and_does_not_make_mutation_buttons():
    class Repository:
        def paper_read(self, *args):
            return {"account": {"name": "我的账户"}, "balance": {"cash_balance": "1000", "available_balance": "900",
                "position_margin": "100", "order_reserved_margin": "0", "unrealized_pnl": None, "equity": None}}
    reply = build_market_reply(Repository(), uuid4(), uuid4(), uuid4(), {"action": "paper_account", "account_id": str(uuid4())})
    assert "浮盈亏 未知 · 净值 未知" in reply["text"]
    assert all(action["action"] in {"paper_accounts", "paper_account", "paper_orders", "paper_positions"} for action in reply["actions"])


def test_runtime_rechecks_paper_read_on_retry_without_watchlist_requirement():
    tenant, user, identifier, connection = uuid4(), uuid4(), uuid4(), uuid4()
    reads, sent = [], []
    now = datetime.now(timezone.utc)
    class Repository:
        def due_tenants(self): return [tenant]
        def claim(self, *args): return identifier, user, connection, {"action": "paper_accounts"}, 2, now, {"text": "stale private balance"}
        def recipient(self, *args): return 42
        def paper_read(self, actual_tenant, actual_user, command):
            reads.append((actual_tenant, actual_user))
            return {"items": [], "has_more": False}
        def save_response(self, *args): pass
        def owned_instrument(self, *args): pytest.fail("PAPER holdings need not be on watchlist")
        def finish(self, *args): assert args[-2].sent
    sender = SimpleNamespace(send_market=lambda chat, text, markup, photo: sent.append(text) or DeliveryOutcome(True, None, None, 1))
    assert TelegramMarketRuntime(Repository(), lambda _: sender, "test", uuid4()).run_once() == 1
    assert reads == [(tenant, user)] and len(sent) == 1 and "stale private balance" not in sent[0]
