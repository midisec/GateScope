"""Real notification DB role, owner isolation, and fake-only Telegram delivery."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
from gatescope.application.notifications import DeliveryOutcome, NotificationDispatcher
from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
from tests.integration.test_telegram_market import setup_data
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, INSTRUMENT


def seed_signal(url, channels=()):
    rule, version, event = uuid4(), uuid4(), uuid4()
    payload = {
        "instrument_id": str(INSTRUMENT),
        "timeframe": "1h",
        "alert_rule_version_id": str(version),
        "notification_type": "RESEARCH_SIGNAL",
        "matched": [{"pattern_id": "double_bottom"}],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    with psycopg.connect(url) as c:
        c.execute(
            """INSERT INTO alert_rules(id,tenant_id,owner_user_id,name,current_version,status,created_at,updated_at)
            VALUES(%s,%s,%s,'Frozen signal',1,'ACTIVE',now(),now())""",
            (rule, TENANT_A, USER_A),
        )
        c.execute(
            """INSERT INTO alert_rule_versions(id,tenant_id,alert_rule_id,version,condition_json,routing_json,created_by,created_at)
            VALUES(%s,%s,%s,1,'{}',%s,%s,now())""",
            (version, TENANT_A, rule, Jsonb({"channel_ids": [str(ch) for ch in channels]}), USER_A),
        )
        c.execute(
            """INSERT INTO alert_event_facts(id,tenant_id,alert_rule_id,alert_rule_version_id,event_key,instrument_id,
            timeframe,available_at,matched_fact_ids,payload_json,payload_hash,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,'1h',now(),'{}',%s,%s,now()-interval '2 days')""",
            (event, TENANT_A, rule, version, "a" * 64, INSTRUMENT, Jsonb(payload), "b" * 64),
        )
    return event, payload


def add_other_bindings(url):
    same, other = uuid4(), uuid4()
    with psycopg.connect(url) as c:
        c.execute(
            """INSERT INTO memberships(id,tenant_id,user_id,role,status,created_at,updated_at)
            VALUES(gen_random_uuid(),%s,%s,'VIEWER','ACTIVE',now(),now())""",
            (TENANT_A, USER_B),
        )
        for connection, tenant, chat in ((same, TENANT_A, 43), (other, TENANT_B, 44)):
            c.execute(
                """INSERT INTO telegram_connections(id,tenant_id,user_id,telegram_user_id,telegram_chat_id,status,verified_at,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,'ACTIVE',now(),now(),now())""",
                (connection, tenant, USER_B, chat, chat),
            )
    return same, other


def test_permanent_signal_callback_is_owner_bound_and_survives_removed_watchlist(migrated_database):
    setup_data(migrated_database)
    add_other_bindings(migrated_database)
    event, _ = seed_signal(migrated_database)
    with psycopg.connect(migrated_database) as c:
        c.execute("DELETE FROM watchlist_items")
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    api.open()
    worker.open()
    try:
        query = TelegramMarketRepository(api)
        repo = TelegramMarketRepository(worker)
        a = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A)
        same = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_B)
        other = SimpleNamespace(tenant_id=TENANT_B, user_id=USER_B)
        callback = "gm:signal:" + event.hex
        assert query.enqueue(a, 100, 42, callback=callback) == "QUEUED"
        assert query.enqueue(same, 101, 43, callback=callback) == "EXPIRED"
        assert query.enqueue(other, 102, 44, callback=callback) == "EXPIRED"
        assert repo.owned_signal(TENANT_A, USER_A, str(event))["id"] == str(event)
        assert repo.owned_signal(TENANT_A, USER_B, str(event)) is None
        assert repo.owned_signal(TENANT_B, USER_B, str(event)) is None
        sent = []

        class Sender:
            def send_market(self, chat, text, markup, photo):
                sent.append((chat, text, markup, photo))
                return DeliveryOutcome(True, None, None, 101)

        assert TelegramMarketRuntime(repo, lambda tenant: Sender(), "signal-test", uuid4()).run_once() == 1
        assert len(sent) == 1 and sent[0][0] == 42 and sent[0][3] is None
        assert "精确原始图暂不可用" in sent[0][1] and "已移出" not in sent[0][1]
        assert sent[0][2]["inline_keyboard"][0][0]["callback_data"] == callback
    finally:
        api.close()
        worker.close()


def test_dispatcher_routes_and_explicit_channels_cannot_cross_same_tenant_owner(migrated_database):
    own = setup_data(migrated_database)
    same, _ = add_other_bindings(migrated_database)
    channel_a, channel_b = uuid4(), uuid4()
    with psycopg.connect(migrated_database) as c:
        for channel, connection, owner in ((channel_a, own, USER_A), (channel_b, same, USER_B)):
            c.execute(
                """INSERT INTO notification_channels(id,tenant_id,channel_type,alias,status,connection_id,created_at,updated_at)
                VALUES(%s,%s,'TELEGRAM',%s,'ACTIVE',%s,now(),now())""",
                (channel, TENANT_A, str(channel), connection),
            )
            c.execute(
                """INSERT INTO notification_routes(id,tenant_id,owner_user_id,channel_id,name,event_types,filters_json,quiet_hours_json,
                rate_policy_json,status,created_at,updated_at) VALUES(gen_random_uuid(),%s,%s,%s,%s,ARRAY['RESEARCH_SIGNAL'],'{}','{}','{}','ACTIVE',now(),now())""",
                (TENANT_A, owner, channel, str(channel)),
            )
    event, payload = seed_signal(migrated_database, (channel_a, channel_b))
    with psycopg.connect(migrated_database) as c:
        c.execute(
            """INSERT INTO outbox_events(event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,payload_json,
            occurred_at,created_at,next_attempt_at) VALUES(%s,%s,'ALERT',%s,'notification.requested.v1',1,%s,now(),now(),now())""",
            (uuid4(), TENANT_A, event, Jsonb(payload)),
        )
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    pool.open()
    try:
        sent = []

        class Sender:
            def send(self, chat, text, *, reply_markup=None):
                sent.append((chat, text, reply_markup))
                return DeliveryOutcome(True, None, None, 102)

        assert NotificationDispatcher(pool, Sender(), worker_id="owner-signal").run_once() == 2
        assert len(sent) == 1 and sent[0][0] == 42 and "双底" in sent[0][1]
        assert sent[0][2]["inline_keyboard"][0][0]["callback_data"] == "gm:signal:" + event.hex
        with pool.transaction(TENANT_A, USER_A) as c:
            assert c.execute("SELECT channel_id,status FROM notification_deliveries").fetchall() == [
                (channel_a, "SENT")
            ]
    finally:
        pool.close()
