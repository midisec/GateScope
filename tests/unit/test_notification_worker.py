from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
import requests

from gatescope.application.notifications import (
    ClaimedDelivery,
    TelegramBotAdmin,
    TelegramSender,
    notification_dedupe_key,
    quiet_until,
    render_notification,
    route_matches,
    signal_delivery_error,
    NotificationDispatcher,
)


class FakeResponse:
    def __init__(
        self, status_code: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request: dict[str, Any] | None = None

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request = {"url": url, **kwargs}
        return self.response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request = {"url": url, **kwargs}
        return self.response


def sender(response: FakeResponse) -> tuple[TelegramSender, FakeSession]:
    session = FakeSession(response)
    return TelegramSender("123456789:" + "A" * 35, session=session), session  # type: ignore[arg-type]


def test_telegram_sender_checks_business_ok_and_message_id() -> None:
    client, session = sender(FakeResponse(200, {"ok": True, "result": {"message_id": 42}}))
    result = client.send(123, "hello")
    assert result.sent and result.telegram_message_id == 42
    assert session.request is not None
    assert session.request["json"] == {"chat_id": 123, "text": "hello", "protect_content": True}

    denied, _ = sender(FakeResponse(200, {"ok": False, "description": "not echoed"}))
    assert denied.send(123, "hello").safe_error_code == "TG_REJECTED"


def test_telegram_sender_honors_bounded_retry_after() -> None:
    client, _ = sender(FakeResponse(429, {"ok": False, "parameters": {"retry_after": 17}}))
    outcome = client.send(123, "hello")
    assert not outcome.sent
    assert outcome.retry_after_seconds == 17
    assert outcome.safe_error_code == "TG_RATE_LIMIT"


def test_telegram_admin_validates_business_response_and_sets_webhook() -> None:
    token = "123456789:" + "B" * 35
    get_session = FakeSession(
        FakeResponse(200, {"ok": True, "result": {"username": "gatescope_test_bot"}}),
    )
    assert TelegramBotAdmin(session=get_session).validate_token(token) == "gatescope_test_bot"  # type: ignore[arg-type]
    assert get_session.request is not None
    assert get_session.request["url"].endswith("/getMe")
    assert get_session.request["timeout"] == (3.05, 10)

    webhook_secret = "C" * 32
    post_session = FakeSession(FakeResponse(200, {"ok": True, "result": True}))
    TelegramBotAdmin(session=post_session).set_webhook(  # type: ignore[arg-type]
        token,
        "https://trade.example.test/api/v2/integrations/telegram/webhook/" + "D" * 32,
        webhook_secret,
    )
    assert post_session.request is not None
    assert post_session.request["url"].endswith("/setWebhook")
    assert post_session.request["json"] == {
        "url": "https://trade.example.test/api/v2/integrations/telegram/webhook/" + "D" * 32,
        "secret_token": webhook_secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }


@pytest.mark.parametrize(
    ("status_code", "payload", "method", "expected_type", "safe_code"),
    [
        (200, {"ok": False, "description": "sensitive upstream body"}, "get", RuntimeError, "TG_UPSTREAM"),
        (200, {"ok": True, "result": {}}, "get", RuntimeError, "TG_INVALID_RESPONSE"),
        (401, {"ok": False, "description": "token rejected"}, "get", ValueError, "TG_AUTH"),
        (429, {"ok": False, "description": "quota details"}, "get", RuntimeError, "TG_RATE_LIMIT"),
        (
            200,
            {"ok": False, "description": "private webhook detail"},
            "post",
            RuntimeError,
            "TG_WEBHOOK_REJECTED",
        ),
        (404, {"ok": False, "description": "token rejected"}, "post", ValueError, "TG_AUTH"),
        (429, {"ok": False, "description": "quota details"}, "post", RuntimeError, "TG_RATE_LIMIT"),
    ],
)
def test_telegram_admin_uses_safe_error_codes_without_upstream_body(
    status_code: int,
    payload: dict[str, Any],
    method: str,
    expected_type: type[Exception],
    safe_code: str,
) -> None:
    token = "123456789:" + "E" * 35
    session = FakeSession(FakeResponse(status_code, payload))
    admin = TelegramBotAdmin(session=session)  # type: ignore[arg-type]
    with pytest.raises(expected_type) as captured:
        if method == "get":
            admin.validate_token(token)
        else:
            admin.set_webhook(token, "https://trade.example.test/hook", "F" * 32)
    assert str(captured.value) == safe_code
    assert token not in str(captured.value)
    description = payload.get("description")
    if isinstance(description, str):
        assert description not in str(captured.value)


class NetworkFailureSession(FakeSession):
    def __init__(self) -> None:
        super().__init__(FakeResponse(500, {}))

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("private network detail")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("private network detail")


@pytest.mark.parametrize("method", ["get", "post"])
def test_telegram_admin_redacts_network_exception(method: str) -> None:
    token = "123456789:" + "G" * 35
    admin = TelegramBotAdmin(session=NetworkFailureSession())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as captured:
        if method == "get":
            admin.validate_token(token)
        else:
            admin.set_webhook(token, "https://trade.example.test/hook", "H" * 32)
    assert str(captured.value) == "TG_NETWORK"
    assert token not in str(captured.value)
    assert "private network detail" not in str(captured.value)


def test_alert_message_is_human_readable_and_never_claims_profit() -> None:
    delivery = ClaimedDelivery(
        id=UUID("10000000-0000-0000-0000-000000000001"),
        tenant_id=UUID("10000000-0000-0000-0000-000000000002"),
        event_id=UUID("10000000-0000-0000-0000-000000000003"),
        event_type="notification.requested.v1",
        attempt_count=1,
        telegram_chat_id=123,
        payload={
            "instrument_id": "BTC",
            "timeframe": "1h",
            "matched": [{"pattern_id": "double_bottom"}],
        },
    )
    message = render_notification(delivery)
    assert "双底" in message
    assert "尚未下单" in message
    assert "不是获利承诺" in message


def test_notification_route_filters_are_fail_closed() -> None:
    filters = {
        "symbols": ["BTC_USDT"],
        "strategy_ids": [],
        "exchange_account_ids": [],
        "environments": ["PAPER"],
        "minimum_severity": "WARNING",
    }
    assert route_matches(
        filters,
        {"environment": "PAPER", "severity": "CRITICAL"},
        "BTC_USDT",
    )
    assert not route_matches(
        filters,
        {"environment": "PAPER", "severity": "INFO"},
        "BTC_USDT",
    )
    assert not route_matches(
        filters,
        {"environment": "PAPER", "severity": "CRITICAL"},
        "ETH_USDT",
    )
    assert not route_matches({**filters, "symbols": "BTC_USDT"}, {}, "BTC_USDT")


def test_quiet_hours_cross_midnight_and_critical_bypass() -> None:
    policy = {
        "quiet_hours": {"timezone": "Asia/Shanghai", "start": "23:00", "end": "07:00"},
        "bypass_quiet_rules": [{"event_type": "RISK_CRITICAL", "environments": []}],
    }
    during_quiet = datetime(2026, 9, 7, 16, 30, tzinfo=timezone.utc)  # 00:30 Shanghai
    scheduled = quiet_until(policy, "RESEARCH_SIGNAL", "PAPER", during_quiet)
    assert scheduled == datetime(2026, 9, 7, 23, 0, tzinfo=timezone.utc)
    assert quiet_until(policy, "RISK_CRITICAL", "LIVE", during_quiet) is None
    daytime = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)  # 12:00 Shanghai
    assert quiet_until(policy, "RESEARCH_SIGNAL", "PAPER", daytime) is None


def test_notification_dedupe_prefers_immutable_event_key() -> None:
    key = "a" * 64
    assert notification_dedupe_key({"event_key": key}) == key
    first = notification_dedupe_key({"instrument_id": "BTC", "timeframe": "1h"})
    second = notification_dedupe_key({"timeframe": "1h", "instrument_id": "BTC"})
    assert first == second


def test_signal_deadline_survives_retry_quiet_hours_and_legacy_queue():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {
        "timeframe": "1m",
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
    }
    delivery = ClaimedDelivery(
        UUID(int=1), UUID(int=2), UUID(int=3), "notification.requested.v1", 1, 123, payload, occurred_at=now
    )
    assert signal_delivery_error(delivery, now + timedelta(seconds=119)) is None
    assert (
        signal_delivery_error(replace(delivery, attempt_count=4), now + timedelta(minutes=2))
        == "SIGNAL_EXPIRED"
    )
    legacy = replace(delivery, payload={"timeframe": "1m"})
    assert signal_delivery_error(legacy, now + timedelta(hours=8)) == "SIGNAL_EXPIRED"
    assert signal_delivery_error(replace(legacy, occurred_at=None), now) == "SIGNAL_EXPIRY_UNKNOWN"
    assert (
        signal_delivery_error(replace(delivery, payload={**payload, "expires_at": "bad"}), now)
        == "SIGNAL_EXPIRY_UNKNOWN"
    )
    assert (
        signal_delivery_error(
            replace(delivery, event_type="notification.test.requested.v1"), now + timedelta(days=1)
        )
        is None
    )
    for kind in ("RISK_CRITICAL", "TP_SL", "ORDER_FILLED", "SYSTEM"):
        assert (
            signal_delivery_error(
                replace(delivery, payload={"notification_type": kind}), now + timedelta(days=1)
            )
            is None
        )


def test_dispatcher_completes_expired_signal_without_contacting_telegram():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    delivery = ClaimedDelivery(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        "notification.requested.v1",
        3,
        123,
        {"timeframe": "1h"},
        occurred_at=old,
    )
    dispatcher = object.__new__(NotificationDispatcher)
    dispatcher._due_tenants = lambda function, limit: (
        [] if function == "notification_outbox_due_tenants" else [UUID(int=2)]
    )
    dispatcher._claim_deliveries = lambda tenant, size: [delivery]
    dispatcher._sender_for_tenant = lambda tenant: pytest.fail("expired signal must not contact Telegram")
    completed = []
    dispatcher._complete = lambda message, outcome: completed.append(outcome)
    assert dispatcher.run_once() == 1
    assert completed[0].safe_error_code == "SIGNAL_EXPIRED"
    assert not completed[0].sent and completed[0].retry_after_seconds is None


def test_notification_uses_permanent_alert_fact_id_and_sends_exactly_one_message():
    now=datetime.now(timezone.utc)
    delivery=ClaimedDelivery(UUID(int=1),UUID(int=2),UUID(int=3),'notification.requested.v1',1,123,
        {'timeframe':'1h','matched':[{'pattern_id':'double_bottom'}]},occurred_at=now,alert_id=UUID(int=9))
    client,session=sender(FakeResponse(200,{'ok':True,'result':{'message_id':101}}))
    dispatcher=object.__new__(NotificationDispatcher)
    dispatcher._due_tenants=lambda function,limit: [] if function=='notification_outbox_due_tenants' else [UUID(int=2)]
    dispatcher._claim_deliveries=lambda tenant,size:[delivery]
    dispatcher._sender_for_tenant=lambda tenant:client
    dispatcher._recipient_active=lambda message:True
    completed=[]
    dispatcher._complete=lambda message,outcome:completed.append(outcome)
    assert dispatcher.run_once()==1 and len(completed)==1 and completed[0].sent
    assert session.request['url'].endswith('/sendMessage')
    buttons=session.request['json']['reply_markup']['inline_keyboard'][0]
    assert buttons[0]['callback_data']=='gm:signal:'+UUID(int=9).hex
    assert buttons[1]['callback_data']=='gm:signalstate:'+UUID(int=9).hex
    assert UUID(int=3).hex not in str(buttons)


def test_notification_is_bounded_in_utf16_and_always_preserves_safety_footer(monkeypatch):
    import gatescope.application.pattern_trade_notice as notices
    monkeypatch.setattr(notices,'render_pattern_trade_notice',lambda notice:'形成计划 🚀'*1000)
    delivery=ClaimedDelivery(UUID(int=1),UUID(int=2),UUID(int=3),'notification.requested.v1',1,123,
        {'timeframe':'1h','matched':[{'pattern_id':'double_bottom','trade_notice':{'status':'READY'}}]})
    message=render_notification(delivery)
    assert len(message.encode('utf-16-le'))//2<=4096
    assert message.endswith('未提交订单；不是获利承诺。')
