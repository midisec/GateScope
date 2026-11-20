from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from gatescope.application.telegram_market import market_command, callback_update, build_market_reply
from gatescope.application.telegram_chart import chart_spec, render_chart
from gatescope.application.notifications import TelegramSender
from tests.unit.test_notification_worker import FakeResponse, FakeSession

TOKEN = "123456789:" + 35 * "A"


def test_commands_are_bounded_read_only_and_callbacks_require_private_owner():
    assert market_command("/chart@scope_bot BTC 4h") == {
        "action": "overview",
        "symbol": "BTC",
        "timeframe": "4h",
    }
    with pytest.raises(ValueError):
        market_command("/order BTC")
    assert market_command("/start binding-token") is None
    with pytest.raises(ValueError):
        market_command("/chart BTC 99h")
    q = {
        "update_id": 123,
        "callback_query": {
            "id": "cb",
            "from": {"id": 42},
            "message": {"chat": {"id": 42, "type": "private"}},
            "data": "gm:" + "a" * 32 + ":0",
        },
    }
    assert callback_update(q)[1:3] == (42, 42)
    q["callback_query"]["message"]["chat"]["id"] = 43
    with pytest.raises(ValueError):
        callback_update(q)


def test_signal_buttons_are_opaque_bounded_and_private():
    from gatescope.application.telegram_market import notification_market_buttons, signal_callback

    identifier = uuid4()
    buttons = notification_market_buttons(identifier)["inline_keyboard"][0]
    for button in buttons:
        assert len(button["callback_data"].encode()) <= 64
        command = signal_callback(button["callback_data"])
        assert command["event_id"] == str(identifier) and command["action"] == "signal"
        q = {
            "update_id": 123,
            "callback_query": {
                "id": "cb",
                "from": {"id": 42},
                "message": {"chat": {"id": 42, "type": "private"}},
                "data": button["callback_data"],
            },
        }
        assert callback_update(q)[1:3] == (42, 42)
        q["callback_query"]["message"]["chat"]["type"] = "group"
        with pytest.raises(ValueError):
            callback_update(q)
    assert signal_callback("gm:signal:../../private") is None


def test_signal_reply_checks_event_owner_before_any_chart_read():
    class Repository:
        def owned_signal(self, *args):
            return None

        def signal_chart(self, *args, **kwargs):
            pytest.fail("unowned event read")

    reply = build_market_reply(
        Repository(), uuid4(), uuid4(), uuid4(), {"action": "signal", "event_id": str(uuid4())}
    )
    assert "不属于你的账户" in reply["text"] and reply["chart"] is None


def test_historical_signal_does_not_require_current_watchlist():
    event_id = uuid4()

    class Repository:
        def owned_signal(self, *args):
            return {"id": str(event_id)}

        def owned_instrument(self, *args):
            pytest.fail("historical event is not current watchlist")

        def signal_chart(self, signal, *, latest):
            assert latest
            return None, "精确原图不可用；冻结计划仍保留。"

    reply = build_market_reply(
        Repository(),
        uuid4(),
        uuid4(),
        uuid4(),
        {"action": "signal", "event_id": str(event_id), "latest": True},
    )
    assert reply["chart"] is None and "冻结计划" in reply["text"]
    assert reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "gm:signal:" + event_id.hex


def test_watchlist_buttons_are_paginated_and_fit_callback_limit():
    class Repository:
        def watchlist(self, *args):
            return [{"id": str(uuid4()), "symbol": "BTC_USDT", "timeframe": "1h"}] * 8, 20

    reply = build_market_reply(Repository(), uuid4(), uuid4(), uuid4(), {"action": "watchlist", "offset": 8})
    assert len(reply["actions"]) == 12
    assert all(
        len(b["callback_data"].encode()) <= 64
        for row in reply["reply_markup"]["inline_keyboard"]
        for b in row
    )
    assert reply["actions"][-2]["offset"] == 0 and reply["actions"][-1]["offset"] == 16


def test_overview_does_not_query_unowned_instrument():
    class Repository:
        def owned_instrument(self, *args):
            return None

        def tracking(self, *args):
            raise AssertionError("cross-user data access")

    reply = build_market_reply(
        Repository(), uuid4(), uuid4(), uuid4(), {"action": "overview", "symbol": "BTC", "timeframe": "1h"}
    )
    assert "不在你的自选" in reply["text"] and not reply["chart"]


def test_chart_uses_exact_times_prices_and_shared_indicator_versions():
    rows = [
        {
            "ts": 1700000000 + i * 3600,
            "open": 100 + i * 0.1,
            "high": 103 + i * 0.1,
            "low": 98 + i * 0.1,
            "close": 101 + i * 0.1,
            "volume": 10 + i % 3,
        }
        for i in range(120)
    ]
    points = [{"time": rows[i]["ts"], "price": rows[i]["high"], "label": str(i)} for i in (20, 80)]
    event = {
        "name": "固定锚点",
        "points": points,
        "lines": [{"from": points[0], "to": points[1], "style": "dashed"}],
    }
    spec = chart_spec("BTC_USDT", "1h", rows, event, "指标对照", layout="INDICATORS")
    assert spec["event"]["points"] == points and spec["bars"] == rows
    assert spec["indicator_versions"]["boll"] == "1.0.1"
    png = render_chart(spec)
    im = Image.open(BytesIO(png))
    assert im.size == (1400, 1120) and len(png) < 10_000_000
    assert spec["values"][str(rows[-1]["ts"])]["rsi"]["rsi"] == 100
    with pytest.raises(ValueError):
        render_chart({**spec, "bars": list(reversed(rows))})


def test_pattern_chart_preserves_early_anchors_without_computing_indicators(monkeypatch):
    import gatescope.application.telegram_chart as charts

    monkeypatch.setattr(
        charts, "compute_batch", lambda *a: pytest.fail("pattern view must not compute indicators")
    )
    rows = [
        {
            "ts": 1700000000 + i * 3600,
            "open": 100.0,
            "high": 104.0,
            "low": 98.0,
            "close": 101.0,
            "volume": 10.0,
        }
        for i in range(1000)
    ]
    points = [{"time": rows[i]["ts"], "price": rows[i]["high"], "label": str(i)} for i in (20, 380)]
    event = {"name": "历史形态", "points": points, "lines": [{"from": points[0], "to": points[1]}]}
    spec = chart_spec("BTC_USDT", "1h", rows, event, "原始证据")
    assert spec["event"] == event and spec["bars"] == rows[8:]
    assert spec["values"] == {} and spec["indicator_versions"] == {}
    assert "RSI" not in spec["summary"] and "EMA" not in spec["summary"]
    assert Image.open(BytesIO(render_chart(spec))).size == (1400, 960)
    assert chart_spec("BTC_USDT", "1h", rows, None, "形态总览", layout="PATTERN")["bars"] == rows[-120:]


def test_photo_upload_keeps_buttons_and_checks_telegram_business_result():
    session = FakeSession(FakeResponse(200, {"ok": True, "result": {"message_id": 321}}))
    sender = TelegramSender(TOKEN, session=session)
    result = sender.send_market(42, "行情", {"inline_keyboard": []}, b"png")
    assert result.sent and result.telegram_message_id == 321
    assert session.request["files"]["photo"][0] == "gatescope-chart.png"
    assert session.request["data"]["protect_content"] == "true"
    fail = TelegramSender(TOKEN, session=FakeSession(FakeResponse(200, {"ok": False}))).send_market(
        42, "图", {}, b"png"
    )
    assert not fail.sent


def test_shrinking_pattern_list_returns_to_a_populated_page():
    class Repository:
        def owned_instrument(self, *args):
            return {"id": str(uuid4()), "symbol": "BTC_USDT"}

        def tracking(self, *args):
            return {
                "records": {
                    str(i): {
                        "id": f"{i:064x}",
                        "phase": "FORMED",
                        "observed_time": i,
                        "latest": {"name": f"形态{i}"},
                    }
                    for i in range(5)
                }
            }

    reply = build_market_reply(
        Repository(),
        uuid4(),
        uuid4(),
        uuid4(),
        {"action": "patterns", "instrument_id": "x", "timeframe": "1h", "phase": "FORMED", "offset": 12},
    )
    assert "共 5 个，当前第 1 页" in reply["text"]
    details = [a for a in reply["actions"] if a["action"] == "detail"]
    assert len(details) == 5 and all(a["return_to"]["offset"] == 0 for a in details)


def test_shrinking_watchlist_clamps_and_rereads_last_page():
    offsets = []
    items = [{"id": str(uuid4()), "symbol": f"COIN{i}_USDT", "timeframe": "1h"} for i in range(5)]

    class Repository:
        def watchlist(self, tenant, user, offset, limit):
            offsets.append(offset)
            return items[offset : offset + limit], len(items)

    reply = build_market_reply(Repository(), uuid4(), uuid4(), uuid4(), {"action": "watchlist", "offset": 16})
    assert offsets == [16, 0]
    assert sum(a["action"] == "pattern_overview" for a in reply["actions"]) == 5
    assert not any(a["action"] == "watchlist" and a.get("offset") for a in reply["actions"])


def test_shrinking_opportunity_group_and_detail_keep_return_context():
    offsets = []
    iid = str(uuid4())
    coins = [{"id": str(uuid4()), "symbol": f"COIN{i}_USDT"} for i in range(3)]
    item = {
        "instrument_id": iid,
        "candidate": "a" * 64,
        "symbol": "BTC_USDT",
        "timeframe": "8h",
        "name": "双底",
        "phase": "FORMED",
        "direction": "bullish",
        "geometry": 0.8,
        "qualified": False,
        "reasons": ["SAMPLES"],
        "statistics": {
            "sample_count": 20,
            "win_rate": 0.6,
            "interval": None,
            "mean_net_return": 0.01,
            "sample_requirement_reachable": False,
            "maximum_independent_samples": 54,
        },
    }

    class Repository:
        def opportunities(self, tenant, user, offset):
            offsets.append(offset)
            return {
                "items": [item] * 6 if offset < len(coins) else [],
                "coins": coins[offset : offset + 2],
                "total_coins": len(coins),
                "as_of": 1700000000,
                "pending_pairs": 0,
            }

        def owned_instrument(self, *args):
            return {"id": iid, "symbol": "BTC_USDT"}

        def evidence_chart(self, *args):
            return {"bars": []}, "原始证据"

    repo = Repository()
    reply = build_market_reply(
        repo,
        uuid4(),
        uuid4(),
        uuid4(),
        {"action": "opportunities", "coin_offset": 6, "mode": "observe", "offset": 5},
    )
    assert offsets == [6, 2]
    assert "最多约 54 个独立样本" in reply["text"] and "无法满足 100" in reply["text"]
    assert "本组达标 0" in reply["text"]
    detail = next(a for a in reply["actions"] if a["action"] == "detail")
    back = {"action": "opportunities", "coin_offset": 2, "mode": "observe", "offset": 5}
    assert detail["return_to"] == back
    chart = build_market_reply(repo, uuid4(), uuid4(), uuid4(), detail)
    assert back in chart["actions"]
    initial = next(a for a in chart["actions"] if a.get("initial"))
    assert initial["return_to"] == back
    assert back in build_market_reply(repo, uuid4(), uuid4(), uuid4(), initial)["actions"]
