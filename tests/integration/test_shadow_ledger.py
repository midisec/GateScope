"""No external sends/orders: real-role forward-only shadow ledger invariants."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_shadow import ShadowTradeRepository
from gatescope.application.pattern_trade_notice import build_pattern_trade_notice
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, USER_A, USER_B, seed_two_tenants
from tests.unit.test_pattern_trade_notice import event


@contextmanager
def fixture(url):
    seed_two_tenants(url)
    worker = PostgresPool(make_conninfo(url, options="-c role=gatescope_paper"), max_size=1)
    api = PostgresPool(make_conninfo(url, options="-c role=gatescope_api"), max_size=1)
    worker.open()
    api.open()
    try:
        yield ShadowTradeRepository(worker), ShadowTradeRepository(api), worker, api
    finally:
        worker.close()
        api.close()


def seed_alert(url, *, invalid=False, old=False):
    identifier, rule, version = uuid4(), uuid4(), uuid4()
    with psycopg.connect(url) as conn:
        enabled = conn.execute("SELECT enabled_at FROM shadow_runtime").fetchone()[0]
        t = int(enabled.timestamp()) // 60 * 60 + 180
        at = datetime.fromtimestamp(t, timezone.utc)
        created = at + timedelta(microseconds=500_000)
        if old:
            created = enabled - timedelta(hours=1)
        source = event()
        source["observed_time"] = t
        source["forecast"]["issued_at"] = t - 60
        notice = build_pattern_trade_notice(source, {"ts": t - 3600, "close": 100.0})
        if invalid:
            notice = {}
        payload = {"matched": [{"pattern_id": "double_bottom", "source_event": source, "trade_notice": notice}]}
        conn.execute("""INSERT INTO alert_rules(id,tenant_id,owner_user_id,name,current_version,status,etag,created_at,updated_at)
          VALUES(%s,%s,%s,%s,1,'ACTIVE',1,%s,%s)""", (rule, TENANT_A, USER_A, str(rule), created, created))
        conn.execute("""INSERT INTO alert_rule_versions(id,tenant_id,alert_rule_id,version,condition_json,routing_json,created_by,created_at)
          VALUES(%s,%s,%s,1,'{}','{}',%s,%s)""", (version, TENANT_A, rule, USER_A, created))
        conn.execute("""INSERT INTO alert_event_facts(id,tenant_id,alert_rule_id,alert_rule_version_id,event_key,
          instrument_id,timeframe,available_at,matched_fact_ids,payload_json,payload_hash,created_at)
          VALUES(%s,%s,%s,%s,%s,%s,'1h',%s,'{}',%s,%s,%s)""",
                     (identifier, TENANT_A, rule, version, str(identifier), INSTRUMENT, at, Jsonb(payload), "a" * 64, created))
    return identifier, at


def seed_delivery(url, alert, at, status="SENT"):
    channel, delivery = uuid4(), uuid4()
    with psycopg.connect(url) as conn:
        conn.execute("""INSERT INTO notification_channels(id,tenant_id,channel_type,alias,status,created_at,updated_at)
          VALUES(%s,%s,'WEB',%s,'ACTIVE',%s,%s)""", (channel, TENANT_A, str(channel), at, at))
        conn.execute("""INSERT INTO notification_deliveries(id,tenant_id,channel_id,event_id,template_version,status,
          next_attempt_at,created_at,updated_at) VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s)""",
                     (delivery, TENANT_A, channel, uuid5(NAMESPACE_URL, f"gatescope:outbox:notification:{alert}"), status, at, at, at))


def seed_bar(url, at, high, low, close, *, revision=1, available=None):
    available = available or at + timedelta(seconds=61)
    with psycopg.connect(url) as conn:
        conn.execute("""INSERT INTO bars(venue,market,instrument_id,timeframe,open_time,close_time,open,high,low,close,
          volume,is_final,revision,available_at,payload_hash,quality_flags,created_at)
          VALUES('GATE','FUTURES_USDT',%s,'1m',%s,%s,100,%s,%s,%s,10,true,%s,%s,%s,'{}',%s)""",
                     (INSTRUMENT, at, at + timedelta(seconds=60), high, low, close, revision, available, str(uuid4()), available))


def test_two_forward_cohorts_use_actual_delivery_clock_and_restart_idempotently(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        alert, at = seed_alert(migrated_database)
        seed_delivery(migrated_database, alert, at + timedelta(seconds=61))
        seed_bar(migrated_database, at + timedelta(seconds=60), 113, 99, 110)
        seed_bar(migrated_database, at + timedelta(seconds=120), 101, 93, 94)
        now = at + timedelta(seconds=182)
        assert worker.due_owners() == [(TENANT_A, USER_A)]
        assert worker.capture(TENANT_A, USER_A, now=now) == 1
        assert worker.capture(TENANT_A, USER_A, now=now) == 0
        assert worker.advance(TENANT_A, USER_A, now=now)["closed"] == 2
        items = reader.list(TENANT_A, USER_A)["items"]
        assert len(items) == 2
        signal = reader.get(TENANT_A, USER_A, next(i["id"] for i in items if i["cohort"] == "SIGNAL"))
        delivered = reader.get(TENANT_A, USER_A, next(i["id"] for i in items if i["cohort"] == "DELIVERED"))
        assert signal["entry_at"] == at + timedelta(seconds=60)
        assert delivered["entry_at"] == at + timedelta(seconds=120)
        assert signal["net_return"] > 0 and delivered["net_return"] < 0
        assert signal["result"]["state"] == "TARGET" and delivered["result"]["state"] == "STOP"
        assert delivered["eligible_at"] > signal["eligible_at"]
        assert signal["environment"] == "SHADOW" and signal["forward_only"]
        assert signal["policy"]["funding_basis"] == "CONSERVATIVE_RATE_MODEL_NOT_REALIZED_FUNDING"
        assert len(worker.due_owners()) == 0
        before = signal["events"]
        assert worker.advance(TENANT_A, USER_A, now=now)["attempted"] == 0
        assert reader.get(TENANT_A, USER_A, signal["id"])["events"] == before
        summary = reader.summary(TENANT_A, USER_A)
        assert {r["cohort"]: r["win_rate"] for r in summary["cohorts"]} == {"SIGNAL": 1.0, "DELIVERED": 0.0}
        page = reader.list(TENANT_A, USER_A, limit=1)
        assert page["has_more"] and page["next_offset"] == 1
        assert reader.list(TENANT_A, USER_A, limit=1, offset=1)["items"][0]["id"] != page["items"][0]["id"]


def test_old_signals_excluded_missing_plans_and_failed_delivery_retained(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        seed_alert(migrated_database, old=True)
        alert, at = seed_alert(migrated_database, invalid=True)
        seed_delivery(migrated_database, alert, at + timedelta(seconds=2), status="FAILED")
        assert worker.capture(TENANT_A, USER_A, now=at + timedelta(seconds=10)) == 1
        worker.advance(TENANT_A, USER_A, now=at + timedelta(seconds=10))
        items = reader.list(TENANT_A, USER_A)["items"]
        assert len(items) == 2 and all(i["state"] == "SKIPPED" for i in items)
        assert {i["cohort"]: i["reason_code"] for i in items}["DELIVERED"] == "DELIVERY_FAILED"
        summary = reader.summary(TENANT_A, USER_A)
        assert all(c["closed"] == 0 and c["win_rate"] is None for c in summary["cohorts"])


def test_owner_rls_fact_immutability_and_api_cannot_write(migrated_database):
    with fixture(migrated_database) as (worker, reader, worker_pool, api_pool):
        _, at = seed_alert(migrated_database)
        worker.capture(TENANT_A, USER_A, now=at + timedelta(seconds=10))
        identifier = reader.list(TENANT_A, USER_A)["items"][0]["id"]
        assert reader.list(TENANT_A, USER_B)["total"] == 0
        with pytest.raises(LookupError):
            reader.get(TENANT_A, USER_B, identifier)
        with api_pool.public_transaction() as conn:
            assert conn.execute("SELECT count(*) FROM shadow_trade_facts").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with api_pool.transaction(TENANT_A, USER_A) as conn:
                conn.execute("UPDATE shadow_trade_states SET state='CLOSED' WHERE trade_id=%s", (identifier,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with worker_pool.transaction(TENANT_A, USER_A) as conn:
                conn.execute("UPDATE shadow_trade_facts SET plan_json='{}' WHERE id=%s", (identifier,))
        with psycopg.connect(migrated_database) as conn:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                conn.execute("UPDATE shadow_trade_facts SET plan_json='{}' WHERE id=%s", (identifier,))


def test_expired_delivery_not_in_trade_win_denominator(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        alert, at = seed_alert(migrated_database)
        seed_delivery(migrated_database, alert, at + timedelta(hours=3))
        now = at + timedelta(hours=4)
        worker.capture(TENANT_A, USER_A, now=now)
        worker.advance(TENANT_A, USER_A, now=now)
        rows = reader.list(TENANT_A, USER_A, cohort="DELIVERED")["items"]
        assert rows[0]["state"] == "EXPIRED" and rows[0]["reason_code"] == "DELIVERY_AFTER_ENTRY_WINDOW"
        assert rows[0]["delivered_at"] == at + timedelta(hours=3)
        assert rows[0]["entry_at"] is None
        assert reader.summary(TENANT_A, USER_A, cohort="DELIVERED")["cohorts"][0]["closed"] == 0


def test_earliest_revision_frozen_and_future_bar_revision_not_visible(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        _, at = seed_alert(migrated_database)
        opened = at + timedelta(seconds=60)
        seed_bar(migrated_database, opened, 101, 99, 100)
        seed_bar(migrated_database, opened, 113, 99, 110, revision=2, available=at + timedelta(seconds=122))
        now = at + timedelta(seconds=123)
        worker.capture(TENANT_A, USER_A, now=now)
        worker.advance(TENANT_A, USER_A, now=now)
        signal = reader.list(TENANT_A, USER_A, cohort="SIGNAL")["items"][0]
        assert signal["state"] == "OPEN" and signal["net_return"] is None
        detail = reader.get(TENANT_A, USER_A, signal["id"])
        transition = next(e for e in detail["events"] if e["event_type"] == "STATE_CHANGED")
        assert transition["payload"]["bar"]["revision"] == 1


def test_context_groups_keep_unknown_and_separate_signal_from_delivered(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        alert, at = seed_alert(migrated_database)
        seed_delivery(migrated_database, alert, at + timedelta(seconds=61))
        seed_bar(migrated_database, at + timedelta(seconds=60), 113, 99, 110)
        seed_bar(migrated_database, at + timedelta(seconds=120), 101, 93, 94)
        now = at + timedelta(seconds=182)
        worker.capture(TENANT_A, USER_A, now=now)
        worker.advance(TENANT_A, USER_A, now=now)
        groups = reader.summary(TENANT_A, USER_A)["by_context"]
        trend = [g for g in groups if g["dimension"] == "trend"]
        assert len(trend) == 2
        assert all(g["bucket"] == "UNKNOWN" and g["total"] == 1 and g["closed"] == 1 for g in trend)
        assert {g["cohort"]: g["win_rate"] for g in trend} == {"SIGNAL": 1.0, "DELIVERED": 0.0}
        assert {g["dimension"] for g in groups} == {"trend", "volatility", "volume", "higher_alignment", "score_bucket"}
        filtered = reader.summary(TENANT_A, USER_A, cohort="DELIVERED")["by_context"]
        assert len(filtered) == 5 and all(g["cohort"] == "DELIVERED" and g["win_rate"] == 0 for g in filtered)


def test_worker_health_is_visible_without_hostnames_or_raw_error_details(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        assert reader.summary(TENANT_A, USER_A)["worker"]["state"] == "NOT_STARTED"
        worker.heartbeat("private-host-fixture", running=True)
        health = reader.summary(TENANT_A, USER_A)["worker"]
        assert health["state"] == "HEALTHY" and "private-host" not in str(health)
        worker.heartbeat("private-host-fixture", running=True, safe_error="SHADOW_PARTIAL_FAILURE")
        assert reader.summary(TENANT_A, USER_A)["worker"]["state"] == "DEGRADED"
        worker.heartbeat("private-host-fixture", running=True, safe_error="sensitive unapproved text")
        assert reader.summary(TENANT_A, USER_A)["worker"]["safe_error_code"] is None
        worker.heartbeat("private-host-fixture", running=False)
        assert reader.summary(TENANT_A, USER_A)["worker"]["state"] == "STOPPED"


def test_spot_shadow_plan_does_not_charge_derivative_funding(migrated_database):
    with fixture(migrated_database) as (worker, reader, _, _):
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE instruments SET market='SPOT' WHERE id=%s", (INSTRUMENT,))
        _, at = seed_alert(migrated_database)
        assert worker.capture(TENANT_A, USER_A, now=at + timedelta(seconds=10)) == 1
        item = reader.list(TENANT_A, USER_A, cohort="SIGNAL")["items"][0]
        detail = reader.get(TENANT_A, USER_A, item["id"])
        assert detail["policy"]["funding_bps_per_8h"] == 0
