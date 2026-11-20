import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient

from apps.api.formal_app import create_formal_app
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository, MarketSubscription
from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
from gatescope.application.notifications import DeliveryOutcome
from tests.integration.test_postgres_rls import (
    seed_two_tenants,
    TENANT_A,
    TENANT_B,
    USER_A,
    USER_B,
    INSTRUMENT,
)


def setup_data(url):
    seed_two_tenants(url)
    connection = uuid4()
    with psycopg.connect(url) as c:
        c.execute(
            "INSERT INTO telegram_connections(id,tenant_id,user_id,telegram_user_id,telegram_chat_id,status,verified_at,created_at,updated_at) VALUES(%s,%s,%s,42,42,'ACTIVE',now(),now(),now())",
            (connection, TENANT_A, USER_A),
        )
        w = c.execute(
            "INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,'mine',now(),now()) RETURNING id",
            (TENANT_A, USER_A),
        ).fetchone()[0]
        c.execute(
            "INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,%s,0,'1h',now(),now())",
            (TENANT_A, w, INSTRUMENT),
        )
    return connection


def test_queue_private_callbacks_restart_chart_and_revocation(migrated_database):
    connection = setup_data(migrated_database)
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    notification = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    market = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"))
    for p in (api, notification, market):
        p.open()
    try:
        q = TelegramMarketRepository(api)
        worker_repo = TelegramMarketRepository(notification)
        a = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A)
        b = SimpleNamespace(tenant_id=TENANT_B, user_id=USER_B)
        assert q.enqueue(a, 1, 42, {"action": "watchlist"}) == "QUEUED"
        assert q.enqueue(a, 1, 42, {"action": "watchlist"}) == "DUPLICATE"
        sent = []

        class Sender:
            def send_market(self, chat, text, markup, photo):
                sent.append((chat, text, markup, photo))
                return DeliveryOutcome(True, None, None, 100 + len(sent))

        runtime = TelegramMarketRuntime(worker_repo, lambda tenant: Sender(), "test", uuid4())
        assert runtime.run_once() == 1 and len(sent) == 1
        callback = sent[0][2]["inline_keyboard"][0][0]["callback_data"]
        assert q.enqueue(b, 2, 42, callback=callback) == "UNBOUND"
        # Another account in the SAME tenant cannot replay the owner's buttons.
        with psycopg.connect(migrated_database) as c:
            c.execute(
                "INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,'VIEWER','ACTIVE',now(),now())",
                (TENANT_A, USER_B),
            )
            c.execute(
                "INSERT INTO telegram_connections(id,tenant_id,user_id,telegram_user_id,telegram_chat_id,status,verified_at,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,43,43,'ACTIVE',now(),now(),now())",
                (TENANT_A, USER_B),
            )
        same_tenant = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_B)
        assert q.enqueue(same_tenant, 2, 43, callback=callback) == "EXPIRED"
        assert q.owned_instrument(TENANT_A, USER_B, str(INSTRUMENT)) is None
        now = datetime.now(timezone.utc)
        last = int(now.timestamp()) // 3600 * 3600 - 3600
        rows = [(last - (179 - i) * 3600, 100, 104, 98, 101 + i * 0.01, 10) for i in range(180)]
        PostgresMarketIngestRepository(market).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            rows,
            observed_at=now,
        )
        assert q.enqueue(a, 3, 42, callback=callback) == "QUEUED"
        restarted = TelegramMarketRuntime(
            TelegramMarketRepository(notification), lambda tenant: Sender(), "restart", uuid4()
        )
        assert restarted.run_once() == 1 and sent[-1][3].startswith(b"\x89PNG")
        assert "EMA20" not in sent[-1][1]
        with api.transaction(TENANT_A, USER_A) as c:
            row = c.execute(
                "SELECT response_json FROM telegram_market_requests WHERE update_id=3"
            ).fetchone()[0]
            assert "chart" not in row and row["actions"]
        assert q.enqueue(a, 4, 42, callback=callback) == "QUEUED"
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE telegram_connections SET status='REVOKED' WHERE id=%s", (connection,))
        assert restarted.run_once() == 1 and len(sent) == 2
        with api.transaction(TENANT_A, USER_A) as c:
            assert (
                c.execute("SELECT safe_error FROM telegram_market_requests WHERE update_id=4").fetchone()[0]
                == "TG_BINDING_REVOKED"
            )
        with api.transaction(TENANT_B, USER_B) as c:
            assert c.execute("SELECT count(*) FROM telegram_market_requests").fetchone()[0] == 0
        with notification.public_transaction() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE bars SET revision=revision")
    finally:
        for p in (api, notification, market):
            p.close()


def test_real_webhook_acknowledges_callbacks_and_queues_once(migrated_database, monkeypatch):
    setup_data(migrated_database)
    for key in ("GATESCOPE_IDENTITY_KEK", "GATESCOPE_RECOVERY_PEPPER", "GATESCOPE_CURSOR_SIGNING_KEY"):
        monkeypatch.setenv(key, base64.urlsafe_b64encode(b"k" * 32).decode())
    for k, v in {
        "GATESCOPE_DATABASE_URL": migrated_database,
        "GATESCOPE_IDENTITY_KEY_VERSION": "fixture",
        "GATESCOPE_PUBLIC_ORIGIN": "https://testserver",
        "GATESCOPE_TELEGRAM_BOT_USERNAME": "fixture_bot",
        "GATESCOPE_TELEGRAM_WEBHOOK_ID": "w" * 32,
        "GATESCOPE_TELEGRAM_WEBHOOK_SECRET": "s" * 32,
        "GATESCOPE_TELEGRAM_ALLOWED_USER_IDS": "",
    }.items():
        monkeypatch.setenv(k, v)
    endpoint = "/api/v2/telegram/webhook/" + "w" * 32
    message = {
        "update_id": 5,
        "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/watchlist"},
    }
    with TestClient(create_formal_app(), base_url="https://testserver") as client:
        assert client.post(endpoint, json=message).status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": "s" * 32}
        response = client.post(endpoint, json=message, headers=headers)
        assert response.status_code == 200 and "正在读取" in response.json()["text"]
        assert client.post(endpoint, json=message, headers=headers).json() == {"ok": True}
        callback = {
            "update_id": 6,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 42},
                "message": {"chat": {"id": 42, "type": "private"}},
                "data": "gm:" + "a" * 32 + ":0",
            },
        }
        response = client.post(endpoint, json=callback, headers=headers).json()
        assert response["method"] == "answerCallbackQuery" and "过期" in response["text"]
        callback["callback_query"]["message"]["chat"]["id"] = 43
        assert client.post(endpoint, json=callback, headers=headers).json() == {"ok": True}
        callback["update_id"] = 7
        callback["callback_query"]["message"]["chat"]["id"] = 42
        callback["callback_query"]["data"] = "gm:opportunities"
        response = client.post(endpoint, json=callback, headers=headers).json()
        assert response["method"] == "answerCallbackQuery" and "正在读取" in response["text"]
        with psycopg.connect(migrated_database) as c:
            assert (
                c.execute("SELECT command_json FROM telegram_market_requests WHERE update_id=7").fetchone()[
                    0
                ]["action"]
                == "opportunities"
            )
