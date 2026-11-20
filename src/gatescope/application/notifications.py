"""Durable outbox expansion and Telegram delivery worker."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import requests
from gatescope.adapters.postgres import PostgresPool
from gatescope.application.signal_freshness import signal_lifetime
from gatescope.domain.market import utc

TELEGRAM_API_ORIGIN = "https://api.telegram.org"
SUPPORTED_OUTBOX_TYPES = ("notification.requested.v1", "notification.test.requested.v1", "paper.order.fill.v1")


@dataclass(frozen=True)
class DeliveryOutcome:
    sent: bool
    retry_after_seconds: int | None
    safe_error_code: str | None
    telegram_message_id: int | None = None


@dataclass(frozen=True)
class ClaimedDelivery:
    id: UUID
    tenant_id: UUID
    event_id: UUID
    event_type: str
    attempt_count: int
    telegram_chat_id: int
    payload: Mapping[str, Any]
    symbol: str | None = None
    occurred_at: datetime | None = None
    alert_id: UUID | None = None


@dataclass(frozen=True)
class DeliveryTarget:
    channel_id: UUID
    route_id: UUID | None
    next_attempt_at: datetime
    dedupe_key: str | None


class TelegramSender:
    def __init__(self, bot_token: str, *, session: requests.Session | None = None) -> None:
        token = bot_token.strip()
        if re.fullmatch(r"[0-9]{6,20}:[A-Za-z0-9_-]{30,128}", token) is None:
            raise ValueError("Telegram Bot token has an invalid format")
        self._url = f"{TELEGRAM_API_ORIGIN}/bot{token}/sendMessage"
        self._session = session or requests.Session()

    def send(self, chat_id: int, text: str, *, reply_markup=None) -> DeliveryOutcome:
        if not 0 < chat_id < 2**63 or not 1 <= len(text) <= 4096:
            return DeliveryOutcome(False, None, "TG_INVALID_PAYLOAD")
        try:
            response = self._session.post(
                self._url,
                json={"chat_id": chat_id, "text": text, "protect_content": True,
                      **({"reply_markup":reply_markup} if reply_markup else {})},
                timeout=(3.05, 10),
            )
        except requests.RequestException:
            return DeliveryOutcome(False, 5, "TG_NETWORK")
        return self._outcome(response)

    def send_market(self,chat_id,text,reply_markup,photo=None):
        if photo is None:
            return self.send(chat_id,text,reply_markup=reply_markup)
        if not 0<chat_id<2**63 or len(text)>1024 or not 0<len(photo)<=10_000_000:
            return DeliveryOutcome(False,None,'TG_INVALID_PAYLOAD')
        try:
            response=self._session.post(self._url.rsplit('/',1)[0]+'/sendPhoto',
                data={'chat_id':str(chat_id),'caption':text,'protect_content':'true','reply_markup':json.dumps(reply_markup)},
                files={'photo':('gatescope-chart.png',photo,'image/png')},timeout=(3.05,15))
        except requests.RequestException:
            return DeliveryOutcome(False,5,'TG_NETWORK')
        return self._outcome(response)

    @staticmethod
    def _outcome(response):
        payload = _response_object(response)
        if response.status_code == 429:
            retry_after = _retry_after(payload, response.headers.get("Retry-After"))
            return DeliveryOutcome(False, retry_after, "TG_RATE_LIMIT")
        if response.status_code in {401, 404}:
            return DeliveryOutcome(False, None, "TG_AUTH")
        if response.status_code == 403:
            return DeliveryOutcome(False, None, "TG_FORBIDDEN")
        if response.status_code >= 500:
            return DeliveryOutcome(False, 5, "TG_UPSTREAM")
        if response.status_code != 200 or payload.get("ok") is not True:
            return DeliveryOutcome(False, None, "TG_REJECTED")
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            return DeliveryOutcome(False, None, "TG_INVALID_RESPONSE")
        return DeliveryOutcome(True, None, None, message_id)


class TelegramBotAdmin:
    """Bounded Telegram control-plane calls that never expose upstream bodies."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def validate_token(self, token: str) -> str:
        _valid_bot_token(token)
        try:
            response = self._session.get(
                f"{TELEGRAM_API_ORIGIN}/bot{token}/getMe", timeout=(3.05, 10),
            )
        except requests.RequestException as exc:
            raise RuntimeError("TG_NETWORK") from exc
        payload = _response_object(response)
        if response.status_code == 429:
            raise RuntimeError("TG_RATE_LIMIT")
        if response.status_code in {401, 404}:
            raise ValueError("TG_AUTH")
        if response.status_code != 200 or payload.get("ok") is not True:
            raise RuntimeError("TG_UPSTREAM")
        result = payload.get("result")
        username = result.get("username") if isinstance(result, dict) else None
        if not isinstance(username, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", username) is None:
            raise RuntimeError("TG_INVALID_RESPONSE")
        return username

    def set_market_commands(self,token):
        _valid_bot_token(token)
        commands=[{'command':c,'description':d} for c,d in (
            ('opportunities','筛选自选中的高置信形态 · 净胜率 / 样本'),
            ('watchlist','自选币种 · 形态 / 波浪 / 指标图'),('chart','查询指标 K 线，例如 /chart BTC 1h'),
            ('patterns','形成中、已形成与已确认形态'),('help','帮助与安全查询命令'),('trades','最近 PAPER 模拟交易'))]
        try:
            response=self._session.post(f'{TELEGRAM_API_ORIGIN}/bot{token}/setMyCommands',json={'commands':commands},timeout=(3.05,10))
        except requests.RequestException:
            raise RuntimeError('TG_NETWORK') from None
        if response.status_code!=200 or _response_object(response).get('ok') is not True:
            raise RuntimeError('TG_COMMAND_MENU_FAILED')

    def set_webhook(self, token: str, webhook_url: str, webhook_secret: str) -> None:
        _valid_bot_token(token)
        if not webhook_url.startswith("https://"):
            raise ValueError("TG_HTTPS_REQUIRED")
        if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", webhook_secret) is None:
            raise ValueError("TG_INVALID_SECRET")
        try:
            response = self._session.post(
                f"{TELEGRAM_API_ORIGIN}/bot{token}/setWebhook",
                json={
                    "url": webhook_url,
                    "secret_token": webhook_secret,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": False,
                },
                timeout=(3.05, 10),
            )
        except requests.RequestException as exc:
            raise RuntimeError("TG_NETWORK") from exc
        payload = _response_object(response)
        if response.status_code == 429:
            raise RuntimeError("TG_RATE_LIMIT")
        if response.status_code in {401, 404}:
            raise ValueError("TG_AUTH")
        if response.status_code != 200 or payload.get("ok") is not True:
            raise RuntimeError("TG_WEBHOOK_REJECTED")


class NotificationDispatcher:
    MAX_ATTEMPTS = 8

    def __init__(
        self,
        pool: PostgresPool,
        sender: TelegramSender | Callable[[UUID], TelegramSender],
        *,
        worker_id: str,
    ) -> None:
        worker = worker_id.strip()
        if not 1 <= len(worker) <= 80:
            raise ValueError("worker_id must contain 1-80 characters")
        self.pool = pool
        self.sender = sender
        self._sender_for_tenant = sender if callable(sender) else lambda _tenant_id: sender
        self.worker_id = worker
        self.subject_id = uuid5(NAMESPACE_URL, f"gatescope:notification-worker:{worker}")

    def run_once(self, *, tenant_limit: int = 20, batch_size: int = 20) -> int:
        processed = 0
        for tenant_id in self._due_tenants("notification_outbox_due_tenants", tenant_limit):
            processed += self._expand_outbox(tenant_id, batch_size)
        for tenant_id in self._due_tenants("notification_delivery_due_tenants", tenant_limit):
            deliveries = self._claim_deliveries(tenant_id, batch_size)
            for delivery in deliveries:
                expiry_error = signal_delivery_error(delivery, datetime.now(timezone.utc))
                if expiry_error:
                    self._complete(delivery, DeliveryOutcome(False, None, expiry_error))
                    processed += 1
                    continue
                try:
                    tenant_sender = self._sender_for_tenant(delivery.tenant_id)
                    from gatescope.application.telegram_market import notification_market_buttons
                    from gatescope.application.telegram_paper import paper_notification_buttons
                    message = render_notification(delivery)
                    markup = (paper_notification_buttons(delivery.payload)
                              if delivery.event_type == "paper.order.fill.v1"
                              else notification_market_buttons(delivery.alert_id))
                except (RuntimeError, ValueError):
                    outcome = DeliveryOutcome(False, None, "TG_NOT_CONFIGURED")
                else:
                    # Claims can sit in memory while earlier sends wait on Telegram.
                    # Recheck the recipient immediately before each external send,
                    # including existing queued/retried deliveries. A database error
                    # must fail closed, not be confused with missing bot configuration.
                    if not self._recipient_active(delivery):
                        outcome = DeliveryOutcome(False, None, "TG_RECIPIENT_INACTIVE")
                    else:
                        try:
                            outcome = tenant_sender.send(
                                delivery.telegram_chat_id, message, reply_markup=markup,
                            )
                        except (RuntimeError, ValueError):
                            outcome = DeliveryOutcome(False, None, "TG_NOT_CONFIGURED")
                self._complete(delivery, outcome)
                processed += 1
        return processed

    def _recipient_active(self, delivery: ClaimedDelivery) -> bool:
        """Validate the current read recipient, without changing trading state.

        All active membership roles may receive their own notifications. This is
        deliberately separate from permission to open orders: protective exits
        continue independently even if the account loses notification access.
        No database transaction is held across the external Telegram request.
        """
        with self.pool.transaction(delivery.tenant_id, self.subject_id) as connection:
            connection.execute("SET LOCAL statement_timeout='3s'")
            return connection.execute(
                """SELECT 1 FROM notification_deliveries nd
                   JOIN outbox_events oe ON oe.event_id=nd.event_id AND oe.tenant_id=nd.tenant_id
                   JOIN notification_channels nc ON nc.id=nd.channel_id AND nc.tenant_id=nd.tenant_id
                   JOIN telegram_connections tc ON tc.id=nc.connection_id AND tc.tenant_id=nd.tenant_id
                   JOIN users u ON u.id=tc.user_id AND u.status='ACTIVE'
                   JOIN memberships m ON m.user_id=tc.user_id AND m.tenant_id=nd.tenant_id
                     AND m.status='ACTIVE'
                   JOIN tenants t ON t.id=nd.tenant_id AND t.status='ACTIVE'
                   WHERE nd.id=%s AND nd.tenant_id=%s AND nd.event_id=%s
                     AND nd.status='SENDING' AND nd.lease_owner=%s
                     AND tc.telegram_chat_id=%s AND nc.status='ACTIVE' AND tc.status='ACTIVE'
                     AND (oe.event_type<>'paper.order.fill.v1' OR
                       ((oe.payload_json->>'owner_user_id')::uuid=tc.user_id
                        AND oe.payload_json->>'environment'='PAPER'
                        AND (oe.payload_json->>'order_id')::uuid=oe.aggregate_id))
                     AND (oe.aggregate_type<>'ALERT' OR EXISTS (
                       SELECT 1 FROM alert_event_facts af JOIN alert_rules ar ON ar.id=af.alert_rule_id
                         AND ar.tenant_id=af.tenant_id
                       WHERE af.id=oe.aggregate_id AND af.tenant_id=nd.tenant_id
                         AND ar.owner_user_id=tc.user_id
                     ))""",
                (delivery.id, delivery.tenant_id, delivery.event_id,
                 self.worker_id, delivery.telegram_chat_id),
            ).fetchone() is not None

    def _due_tenants(self, function_name: str, limit: int) -> list[UUID]:
        if function_name not in {
            "notification_outbox_due_tenants", "notification_delivery_due_tenants",
        }:
            raise ValueError("unsupported due-tenant function")
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                f"SELECT tenant_id FROM gatescope_security.{function_name}(%s)",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [row[0] for row in rows]

    def _expand_outbox(self, tenant_id: UUID, batch_size: int) -> int:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, self.subject_id) as connection:
            rows = connection.execute(
                """WITH picked AS (
                     SELECT event_id FROM outbox_events
                     WHERE published_at IS NULL AND dead_lettered_at IS NULL
                       AND next_attempt_at<=%s
                       AND (lease_expires_at IS NULL OR lease_expires_at<%s)
                       AND event_type=ANY(%s)
                     ORDER BY occurred_at, event_id FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE outbox_events oe
                   SET lease_owner=%s, lease_expires_at=%s, attempt_count=attempt_count+1
                   FROM picked WHERE oe.event_id=picked.event_id
                   RETURNING oe.event_id, oe.aggregate_id, oe.event_type, oe.payload_json""",
                (
                    now, now, list(SUPPORTED_OUTBOX_TYPES), max(1, min(batch_size, 100)),
                    self.worker_id, now + timedelta(seconds=60),
                ),
            ).fetchall()
            for row in rows:
                event_id, aggregate_id, event_type = row[0], row[1], str(row[2])
                payload = dict(row[3])
                try:
                    targets = self._target_deliveries(connection, event_type, payload, now)
                    for target in targets:
                        connection.execute(
                            """INSERT INTO notification_deliveries
                                 (id, tenant_id, channel_id, route_id, event_id, template_version,
                                  dedupe_key, status, attempt_count, next_attempt_at, created_at, updated_at)
                               SELECT %s, %s, nc.id, %s, %s, 1, %s, 'PENDING', 0, %s, %s, %s
                               FROM notification_channels nc
                               JOIN telegram_connections tc ON tc.id=nc.connection_id
                               WHERE nc.id=%s AND nc.status='ACTIVE' AND tc.status='ACTIVE'
                               ON CONFLICT (tenant_id, channel_id, event_id, template_version) DO NOTHING""",
                            (
                                uuid4(), tenant_id, target.route_id, event_id, target.dedupe_key,
                                target.next_attempt_at, now, now, target.channel_id,
                            ),
                        )
                    connection.execute(
                        """UPDATE outbox_events SET published_at=%s, lease_owner=NULL, lease_expires_at=NULL
                           WHERE event_id=%s AND lease_owner=%s""",
                        (now, event_id, self.worker_id),
                    )
                    if event_type == "notification.test.requested.v1":
                        connection.execute(
                            """UPDATE operations_current SET state='RUNNING', progress=25,
                                      version=version+1, updated_at=%s
                               WHERE id=%s AND resource_id=%s""",
                            (now, event_id, aggregate_id),
                        )
                except (KeyError, TypeError, ValueError):
                    connection.execute(
                        """UPDATE outbox_events SET dead_lettered_at=%s, lease_owner=NULL,
                                  lease_expires_at=NULL
                           WHERE event_id=%s AND lease_owner=%s""",
                        (now, event_id, self.worker_id),
                    )
                    connection.execute(
                        """UPDATE operations_current SET state='FAILED', progress=100,
                                  safe_error_code='NOTIFICATION_INVALID_EVENT',
                                  version=version+1, updated_at=%s WHERE id=%s""",
                        (now, event_id),
                    )
        return len(rows)

    def _target_deliveries(
        self,
        connection: Any,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> list[DeliveryTarget]:
        if event_type == "paper.order.fill.v1":
            if payload.get("environment") != "PAPER":
                raise ValueError("invalid paper notification environment")
            owner = UUID(str(payload["owner_user_id"]))
            order_id = UUID(str(payload["order_id"]))
            account_id = UUID(str(payload["account_id"]))
            # The notification worker starts with a zero subject. Set only the
            # subject, retaining transaction tenant RLS, and verify durable ownership.
            previous = connection.execute("SELECT current_setting('app.subject_id',true)").fetchone()[0]
            connection.execute("SELECT set_config('app.subject_id',%s,true)", (str(owner),))
            try:
                order = connection.execute(
                    "SELECT id FROM paper_orders WHERE id=%s AND owner_user_id=%s AND account_id=%s",
                    (order_id, owner, account_id),
                ).fetchone()
                if order is None:
                    raise ValueError("paper notification owner mismatch")
                channels = connection.execute(
                    """SELECT nc.id FROM notification_channels nc JOIN telegram_connections tc
                       ON tc.id=nc.connection_id AND tc.tenant_id=nc.tenant_id
                       WHERE tc.user_id=%s AND nc.status='ACTIVE' AND tc.status='ACTIVE'""",
                    (owner,),
                ).fetchall()
                return [DeliveryTarget(row[0], None, now, None) for row in channels]
            finally:
                connection.execute("SELECT set_config('app.subject_id',%s,true)", (previous or str(UUID(int=0)),))
        if event_type == "notification.test.requested.v1":
            return [DeliveryTarget(UUID(str(payload["channel_id"])), None, now, None)]
        version_id = UUID(str(payload["alert_rule_version_id"]))
        version = connection.execute(
            """SELECT v.routing_json,r.owner_user_id FROM alert_rule_versions v
               JOIN alert_rules r ON r.id=v.alert_rule_id AND r.tenant_id=v.tenant_id WHERE v.id=%s""",
            (version_id,),
        ).fetchone()
        if version is None:
            raise ValueError("alert rule version is unavailable")
        configured = [UUID(str(value)) for value in dict(version[0]).get('channel_ids', [])]
        owned_channels = connection.execute(
            """SELECT nc.id FROM notification_channels nc JOIN telegram_connections tc ON tc.id=nc.connection_id
               AND tc.tenant_id=nc.tenant_id WHERE nc.id=ANY(%s) AND tc.user_id=%s
               AND nc.status='ACTIVE' AND tc.status='ACTIVE'""", (configured, version[1]),
        ).fetchall() if configured else []
        targets = {
            UUID(str(value)): DeliveryTarget(UUID(str(value)), None, now, None)
            for (value,) in owned_channels
        }
        instrument_id = UUID(str(payload["instrument_id"]))
        instrument = connection.execute(
            "SELECT symbol FROM instruments WHERE id=%s", (instrument_id,),
        ).fetchone()
        symbol = str(instrument[0]) if instrument else ""
        domain_event_type = str(payload.get("notification_type", "RESEARCH_SIGNAL"))
        routes = connection.execute(
            """SELECT nr.id, nr.channel_id, nr.filters_json, nr.quiet_hours_json, nr.rate_policy_json
               FROM notification_routes nr JOIN notification_channels nc ON nc.id=nr.channel_id AND nc.tenant_id=nr.tenant_id
               JOIN telegram_connections tc ON tc.id=nc.connection_id AND tc.tenant_id=nc.tenant_id
               WHERE nr.status='ACTIVE' AND %s=ANY(nr.event_types) AND nr.owner_user_id=%s
                 AND tc.user_id=nr.owner_user_id AND nc.status='ACTIVE' AND tc.status='ACTIVE'
               ORDER BY nr.id""",
            (domain_event_type,version[1]),
        ).fetchall()
        for route in routes:
            route_id, channel_id = route[0], route[1]
            filters = dict(route[2])
            quiet_policy = dict(route[3])
            rate_policy = dict(route[4])
            if not route_matches(filters, payload, symbol):
                continue
            dedupe_key = notification_dedupe_key(payload)
            dedupe_seconds = int(rate_policy.get("dedupe_seconds", 0))
            if dedupe_seconds > 0:
                duplicate = connection.execute(
                    """SELECT 1 FROM notification_deliveries
                       WHERE route_id=%s AND dedupe_key=%s
                         AND status IN ('PENDING','SENDING','SENT','RETRY')
                         AND created_at>=%s LIMIT 1""",
                    (route_id, dedupe_key, now - timedelta(seconds=dedupe_seconds)),
                ).fetchone()
                if duplicate is not None:
                    continue
            environment = str(payload.get("environment", "PAPER"))
            scheduled = quiet_until(quiet_policy, domain_event_type, environment, now) or now
            maximum = max(1, min(int(rate_policy.get("max_per_minute", 10)), 60))
            recent = connection.execute(
                """SELECT COUNT(*), MIN(created_at) FROM notification_deliveries
                   WHERE route_id=%s AND created_at>=%s
                     AND status IN ('PENDING','SENDING','SENT','RETRY')""",
                (route_id, now - timedelta(minutes=1)),
            ).fetchone()
            if recent is not None and int(recent[0]) >= maximum and recent[1] is not None:
                scheduled = max(scheduled, recent[1] + timedelta(minutes=1))
            candidate = DeliveryTarget(channel_id, route_id, scheduled, dedupe_key)
            previous = targets.get(channel_id)
            if previous is None or candidate.next_attempt_at < previous.next_attempt_at:
                targets[channel_id] = candidate
        return list(targets.values())

    def _claim_deliveries(self, tenant_id: UUID, batch_size: int) -> list[ClaimedDelivery]:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, self.subject_id) as connection:
            claimed = connection.execute(
                """WITH picked AS (
                     SELECT id FROM notification_deliveries
                     WHERE (
                         status IN ('PENDING','RETRY') AND next_attempt_at<=%s
                       ) OR (
                         status='SENDING' AND lease_expires_at<%s
                       )
                     ORDER BY next_attempt_at, created_at, id
                     FOR UPDATE SKIP LOCKED LIMIT %s
                   )
                   UPDATE notification_deliveries nd
                   SET status='SENDING', lease_owner=%s, lease_expires_at=%s,
                       attempt_count=attempt_count+1, updated_at=%s
                   FROM picked WHERE nd.id=picked.id
                   RETURNING nd.id, nd.event_id, nd.attempt_count""",
                (
                    now, now, max(1, min(batch_size, 100)), self.worker_id,
                    now + timedelta(seconds=60), now,
                ),
            ).fetchall()
            deliveries: list[ClaimedDelivery] = []
            for row in claimed:
                detail = connection.execute(
                    """SELECT oe.event_type, oe.payload_json, tc.telegram_chat_id, i.symbol, oe.occurred_at,
                              CASE WHEN oe.aggregate_type='ALERT' THEN oe.aggregate_id END
                       FROM notification_deliveries nd
                       JOIN outbox_events oe ON oe.event_id=nd.event_id
                       JOIN notification_channels nc ON nc.id=nd.channel_id
                       JOIN telegram_connections tc ON tc.id=nc.connection_id
                       LEFT JOIN instruments i
                         ON i.id=(oe.payload_json->>'instrument_id')::uuid
                       WHERE nd.id=%s AND nc.status='ACTIVE' AND tc.status='ACTIVE'
                         AND (oe.event_type<>'paper.order.fill.v1' OR
                           ((oe.payload_json->>'owner_user_id')::uuid=tc.user_id
                            AND oe.payload_json->>'environment'='PAPER'
                            AND (oe.payload_json->>'order_id')::uuid=oe.aggregate_id))
                         AND (oe.aggregate_type<>'ALERT' OR EXISTS (
                           SELECT 1 FROM alert_event_facts af JOIN alert_rules ar ON ar.id=af.alert_rule_id
                             AND ar.tenant_id=af.tenant_id
                           WHERE af.id=oe.aggregate_id AND af.tenant_id=nd.tenant_id AND ar.owner_user_id=tc.user_id
                         ))""",
                    (row[0],),
                ).fetchone()
                if detail is None:
                    connection.execute(
                        """UPDATE notification_deliveries SET status='FAILED',
                                  safe_error_code='NOTIFICATION_CHANNEL_UNAVAILABLE',
                                  lease_owner=NULL, lease_expires_at=NULL, updated_at=%s
                           WHERE id=%s AND lease_owner=%s""",
                        (now, row[0], self.worker_id),
                    )
                    continue
                deliveries.append(ClaimedDelivery(
                    id=row[0], tenant_id=tenant_id, event_id=row[1], attempt_count=int(row[2]),
                    event_type=str(detail[0]), payload=dict(detail[1]),
                    telegram_chat_id=int(detail[2]),
                    symbol=str(detail[3]) if detail[3] else None,
                    occurred_at=detail[4],
                    alert_id=detail[5],
                ))
        return deliveries

    def _complete(self, delivery: ClaimedDelivery, outcome: DeliveryOutcome) -> None:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(delivery.tenant_id, self.subject_id) as connection:
            if outcome.sent:
                connection.execute(
                    """UPDATE notification_deliveries SET status='SENT', telegram_message_id=%s,
                              safe_error_code=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=%s
                       WHERE id=%s AND status='SENDING' AND lease_owner=%s""",
                    (outcome.telegram_message_id, now, delivery.id, self.worker_id),
                )
                connection.execute(
                    """UPDATE operations_current SET state='SUCCEEDED', progress=100,
                              result_link=%s, safe_error_code=NULL, version=version+1, updated_at=%s
                       WHERE id=%s AND operation_type='NOTIFICATION_TEST'""",
                    (f"/api/v2/notification-deliveries/{delivery.id}", now, delivery.event_id),
                )
                return
            retryable = outcome.retry_after_seconds is not None and delivery.attempt_count < self.MAX_ATTEMPTS
            if retryable:
                delay = max(1, min(int(outcome.retry_after_seconds or 1), 3_600))
                status = "RETRY"
                next_attempt_at = now + timedelta(seconds=delay)
            else:
                status = "DEAD_LETTER" if delivery.attempt_count >= self.MAX_ATTEMPTS else "FAILED"
                next_attempt_at = now
            connection.execute(
                """UPDATE notification_deliveries SET status=%s, next_attempt_at=%s,
                          safe_error_code=%s, lease_owner=NULL, lease_expires_at=NULL, updated_at=%s
                   WHERE id=%s AND status='SENDING' AND lease_owner=%s""",
                (status, next_attempt_at, outcome.safe_error_code, now, delivery.id, self.worker_id),
            )
            if not retryable:
                connection.execute(
                    """UPDATE operations_current SET state='FAILED', progress=100,
                              safe_error_code=%s, version=version+1, updated_at=%s
                       WHERE id=%s AND operation_type='NOTIFICATION_TEST'""",
                    (outcome.safe_error_code, now, delivery.event_id),
                )


def route_matches(filters: Mapping[str, Any], payload: Mapping[str, Any], symbol: str) -> bool:
    if not _filter_contains(filters, "symbols", symbol):
        return False
    if not _filter_contains(filters, "strategy_ids", str(payload.get("strategy_id", ""))):
        return False
    if not _filter_contains(
        filters, "exchange_account_ids", str(payload.get("exchange_account_id", "")),
    ):
        return False
    if not _filter_contains(filters, "environments", str(payload.get("environment", "PAPER"))):
        return False
    minimum = str(filters.get("minimum_severity", "INFO"))
    severity = str(payload.get("severity", "INFO"))
    levels = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
    return minimum in levels and severity in levels and levels[severity] >= levels[minimum]


def quiet_until(
    policy: Mapping[str, Any],
    event_type: str,
    environment: str,
    now: datetime,
) -> datetime | None:
    quiet = policy.get("quiet_hours")
    if not isinstance(quiet, dict):
        return None
    bypasses = policy.get("bypass_quiet_rules", [])
    if isinstance(bypasses, list):
        for item in bypasses:
            if not isinstance(item, dict) or str(item.get("event_type")) != event_type:
                continue
            environments = item.get("environments", [])
            if isinstance(environments, list) and (not environments or environment in environments):
                return None
    zone = ZoneInfo(str(quiet["timezone"]))
    start_hour, start_minute = _clock(str(quiet["start"]))
    end_hour, end_minute = _clock(str(quiet["end"]))
    if (start_hour, start_minute) == (end_hour, end_minute):
        return None
    local = now.astimezone(zone)
    minute = local.hour * 60 + local.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start < end:
        in_quiet = start <= minute < end
        end_day_offset = 0
    else:
        in_quiet = minute >= start or minute < end
        end_day_offset = 1 if minute >= start else 0
    if not in_quiet:
        return None
    local_end = (local + timedelta(days=end_day_offset)).replace(
        hour=end_hour, minute=end_minute, second=0, microsecond=0,
    )
    return local_end.astimezone(timezone.utc)


def notification_dedupe_key(payload: Mapping[str, Any]) -> str:
    explicit = payload.get("event_key")
    if isinstance(explicit, str) and re.fullmatch(r"[a-f0-9]{64}", explicit):
        return explicit
    material = {
        "instrument_id": payload.get("instrument_id"),
        "timeframe": payload.get("timeframe"),
        "notification_type": payload.get("notification_type", "RESEARCH_SIGNAL"),
        "matched": payload.get("matched", []),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _filter_contains(filters: Mapping[str, Any], name: str, actual: str) -> bool:
    configured = filters.get(name, [])
    if not isinstance(configured, list):
        return False
    return not configured or actual in {str(value) for value in configured}


def _clock(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
    if match is None:
        raise ValueError("quiet-hours clock is invalid")
    hour, minute = value.split(":", maxsplit=1)
    return int(hour), int(minute)


def signal_delivery_error(delivery: ClaimedDelivery, now: datetime) -> str | None:
    """Recheck after quiet hours, rate limits, retries and worker restarts.

    Legacy queued alerts use their immutable outbox event time, never the queue
    insertion/retry time. Test notifications have no trading-signal deadline.
    """
    if delivery.event_type != "notification.requested.v1":
        return None
    if delivery.payload.get('notification_type','RESEARCH_SIGNAL') != 'RESEARCH_SIGNAL':
        return None
    try:
        payload = delivery.payload
        lifetime = signal_lifetime(str(payload['timeframe']))
        if payload.get('observed_at'):
            observed = utc(datetime.fromisoformat(payload['observed_at']))
        else:
            if delivery.occurred_at is None:
                return 'SIGNAL_EXPIRY_UNKNOWN'
            observed = utc(delivery.occurred_at)
        expires = observed + lifetime
        if payload.get('expires_at'):
            expires = min(expires, utc(datetime.fromisoformat(payload['expires_at'])))
        if observed > utc(now) or expires <= observed:
            return 'SIGNAL_INVALID_TIME'
        return 'SIGNAL_EXPIRED' if utc(now) >= expires else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return 'SIGNAL_EXPIRY_UNKNOWN'


def render_notification(delivery: ClaimedDelivery) -> str:
    if delivery.event_type == "notification.test.requested.v1":
        return "✅ GateScope 测试通知\n\nTelegram 通道工作正常。此消息不包含任何 API Key 或交易凭据。"
    payload = delivery.payload
    if delivery.event_type == "paper.order.fill.v1":
        def number(value):
            try:
                numeric = Decimal(str(value))
                if numeric.is_finite():
                    rendered = format(numeric, "f")
                    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
            except InvalidOperation:
                pass
            return "未知"
        action = "开仓" if payload.get("effect") == "OPEN" else "平仓"
        side = {"LONG": "多仓", "SHORT": "空仓"}.get(payload.get("side"), "未知方向")
        reason = {"STOP_LOSS": "止损触发", "TAKE_PROFIT": "止盈触发", "MANUAL": "用户委托", "USER_ORDER": "用户委托"}.get(
            payload.get("reason"), str(payload.get("reason", "委托成交")))
        return telegram_text(
            f"🧪 GateScope · PAPER 模拟{action}成交\n\n"
            f"{payload.get('symbol', '未知')} · {side} · {payload.get('timeframe', '')}\n"
            f"本次成交：{payload.get('contracts')} 张，均价 {number(payload.get('fill_price'))} USDT\n"
            f"累计成交：{payload.get('filled_contracts')} 张；委托剩余：{payload.get('remaining_contracts')} 张\n"
            f"手续费：{number(payload.get('fee'))} USDT；本次毛盈亏：{number(payload.get('realized_pnl'))} USDT\n"
            f"状态：{payload.get('state')} · {reason}\n"
            f"成交时间：{payload.get('filled_at', delivery.occurred_at)}\n"
            f"账户：{payload.get('account_id')}\n委托：{payload.get('order_id')}\n仓位：{payload.get('position_id')}\n\n"
            "仅为本地模拟成交，未向交易所发单。网页「交易 / 模拟交易」可查询完整成交和平仓记录。", 4096)
    from gatescope.application.pattern_trade_notice import render_pattern_trade_notice
    from gatescope.quant.pattern_study import catalog
    names = {item['id']: item['name'] for item in catalog()}
    matched = [item for item in payload.get('matched', []) if isinstance(item, dict)]
    pattern_names = '、'.join(dict.fromkeys(
        str((item.get('source_event') or {}).get('name') or names.get(item.get('pattern_id'), item.get('pattern_id', '未标注')))
        for item in matched
    )) or '未标注'
    plans = [render_pattern_trade_notice(item['trade_notice']) for item in matched
             if isinstance(item.get('trade_notice'), dict)]
    direction = {'bullish':'看涨','bearish':'看跌'}.get(str(payload.get('direction')), '方向待定')
    timing = (f"观测：{payload['observed_at']}\n有效至：{payload['expires_at']}\n"
              if payload.get('observed_at') and payload.get('expires_at') else '')
    heading = (
        "🔔 GateScope 策略信号 · 尚未下单\n\n"
        f"标的：{delivery.symbol or payload.get('instrument_id', '未知')} · {payload.get('timeframe', '未知')}\n"
        f"形态：{pattern_names}\n"
        f"方向：{direction}\n"
        f"{timing}"
        f"事件：{delivery.alert_id or delivery.event_id}\n"
    )
    footer = '\n仅为形态研究通知，未提交订单；不是获利承诺。'
    body = '\n\n'.join(plans) if plans else '旧通知未保存形态做单计划，不以当前行情补写历史。'
    # One sendMessage per durable delivery, not a second photo send. Telegram's
    # 4096-unit limit also counts astral emoji as two UTF-16 code units.
    return telegram_text(heading + body, 4096 - len(footer.encode('utf-16-le')) // 2) + footer


def telegram_text(text: str, limit: int) -> str:
    encoded = text.encode('utf-16-le')
    if len(encoded) <= limit * 2:
        return text
    return encoded[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def _response_object(response: requests.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _retry_after(payload: Mapping[str, Any], header: str | None) -> int:
    parameters = payload.get("parameters")
    candidate = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        try:
            candidate = int(header or "5")
        except ValueError:
            candidate = 5
    return max(1, min(candidate, 3_600))


def _valid_bot_token(token: str) -> str:
    cleaned = token.strip()
    if re.fullmatch(r"[0-9]{6,20}:[A-Za-z0-9_-]{30,128}", cleaned) is None:
        raise ValueError("Telegram Bot token has an invalid format")
    return cleaned
