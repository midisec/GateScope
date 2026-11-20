"""Real PAPER/notification DB roles; fake Telegram transport, no external messages."""
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.application.notifications import DeliveryOutcome, NotificationDispatcher
from tests.integration.test_paper_orders import paperdb, quote, opening  # noqa: F401
from tests.integration.test_postgres_rls import TENANT_A, USER_A, USER_B


def seed_channels(database):
    own_channel = None
    with psycopg.connect(database) as conn:
        for owner, chat in ((USER_A, 41), (USER_B, 42)):
            connection, channel = uuid4(), uuid4()
            conn.execute("""INSERT INTO telegram_connections(id,tenant_id,user_id,telegram_user_id,telegram_chat_id,status,verified_at,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,'ACTIVE',now(),now(),now())""", (connection, TENANT_A, owner, chat, chat))
            conn.execute("""INSERT INTO notification_channels(id,tenant_id,channel_type,alias,status,connection_id,created_at,updated_at)
                VALUES(%s,%s,'TELEGRAM',%s,'ACTIVE',%s,now(),now())""", (channel, TENANT_A, str(channel), connection))
            if owner == USER_A:
                own_channel = channel
    return own_channel


def test_fill_notice_owner_isolation_batch_dedupe_and_persisted_delivery(paperdb):  # noqa: F811
    database, repo, _, account, _ = paperdb
    seed_channels(database)
    at = datetime.now(timezone.utc)
    request = opening(account, contracts=1)
    repo.submit(TENANT_A, USER_A, request, quote(at), now=at)
    repo.submit(TENANT_A, USER_A, request, None, now=at)
    pool = PostgresPool(make_conninfo(database, options="-c role=gatescope_notification"))
    pool.open()
    sent = []
    class Sender:
        def send(self, chat, text, *, reply_markup=None):
            sent.append((chat, text))
            return DeliveryOutcome(True, None, None, 100)
    try:
        dispatcher = NotificationDispatcher(pool, Sender(), worker_id="paper-notice-test")
        assert dispatcher.run_once() == 2
        assert dispatcher.run_once() == 0
        assert len(sent) == 1 and sent[0][0] == 41
        assert "PAPER 模拟开仓成交" in sent[0][1] and "尚未下单" not in sent[0][1]
        with psycopg.connect(database) as conn:
            assert conn.execute("SELECT status,attempt_count FROM notification_deliveries").fetchall() == [("SENT", 1)]
    finally:
        pool.close()


def test_forged_owner_event_is_dead_lettered_not_broadcast(paperdb):  # noqa: F811
    database, repo, _, account, _ = paperdb
    seed_channels(database)
    at = datetime.now(timezone.utc)
    repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    with psycopg.connect(database) as conn:
        original = conn.execute("SELECT aggregate_id,payload_json FROM outbox_events WHERE event_type='paper.order.fill.v1'").fetchone()
        payload = {**original[1], "owner_user_id": str(USER_B)}
        forged = uuid4()
        conn.execute("""INSERT INTO outbox_events(event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,payload_json,occurred_at,created_at,next_attempt_at)
            VALUES(%s,%s,'paper_order',%s,'paper.order.fill.v1',1,%s,now(),now(),now())""", (forged, TENANT_A, original[0], Jsonb(payload)))
    pool = PostgresPool(make_conninfo(database, options="-c role=gatescope_notification"))
    pool.open()
    sent = []
    class Sender:
        def send(self, chat, text, *, reply_markup=None):
            sent.append(chat)
            return DeliveryOutcome(True, None, None, 101)
    try:
        NotificationDispatcher(pool, Sender(), worker_id="paper-forged-test").run_once()
        assert sent == [41]
        with psycopg.connect(database) as conn:
            assert conn.execute("SELECT dead_lettered_at IS NOT NULL FROM outbox_events WHERE event_id=%s", (forged,)).fetchone() == (True,)
    finally:
        pool.close()
