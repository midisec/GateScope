"""Regression coverage for UI lifecycle and paginated, user-scoped reads."""
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_alert_rules import (
    PostgresAlertRuleRepository, AlertRuleNotFound, AlertRulePreconditionFailed, AlertRuleConflict,
)
from gatescope.adapters.postgres_notifications import PostgresNotificationRepository
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, INSTRUMENT, seed_two_tenants


def test_alert_resume_and_notification_pages_are_user_scoped(migrated_database):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as conn:
        for tenant, user in ((TENANT_A, USER_A), (TENANT_A, USER_B), (TENANT_B, USER_B)):
            channel = uuid4()
            conn.execute("""INSERT INTO notification_channels
                (id,tenant_id,owner_user_id,channel_type,alias,status,created_at,updated_at)
                VALUES (%s,%s,%s,'WEB',%s,'ACTIVE',now(),now())""", (channel, tenant, user, str(channel)))
            for i in range(25):
                event = uuid4()
                conn.execute("""INSERT INTO outbox_events
                    (event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,payload_json,
                     occurred_at,created_at,next_attempt_at)
                    VALUES (%s,%s,'TEST',%s,'SYSTEM',1,'{}',now(),now(),now())""", (event, tenant, channel))
                conn.execute("""INSERT INTO notification_deliveries
                    (id,tenant_id,channel_id,event_id,template_version,status,next_attempt_at,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,1,%s,now(),now(),now())""",
                    (uuid4(), tenant, channel, event, "SENT" if i < 20 else "FAILED"))
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    pool.open()
    try:
        rules = PostgresAlertRuleRepository(pool)
        original = rules.create(TENANT_A, USER_A, name="Resume regression",
            condition={"instrument_id": str(INSTRUMENT)}, routing={})
        paused = rules.pause(TENANT_A, USER_A, original.id, expected_etag=original.etag)
        with pytest.raises(AlertRulePreconditionFailed):
            rules.resume(TENANT_A, USER_A, original.id, expected_etag=original.etag)
        with pytest.raises(AlertRuleNotFound):
            rules.resume(TENANT_A, USER_B, original.id, expected_etag=paused.etag)
        with pytest.raises(AlertRuleNotFound):
            rules.resume(TENANT_B, USER_B, original.id, expected_etag=paused.etag)
        resumed = rules.resume(TENANT_A, USER_A, original.id, expected_etag=paused.etag)
        assert resumed.status == "ACTIVE"
        assert resumed.version_id == original.version_id
        assert resumed.current_version == original.current_version
        edit = dict(name="Updated rule", condition={"instrument_id": str(INSTRUMENT), "timeframe": "4h"}, routing={}, expected_etag=resumed.etag)
        for tenant, user in ((TENANT_A, USER_B), (TENANT_B, USER_B)):
            with pytest.raises(AlertRuleNotFound):
                rules.update(tenant, user, original.id, **edit)
            with pytest.raises(AlertRuleNotFound):
                rules.delete(tenant, user, original.id, expected_etag=resumed.etag)
        with pytest.raises(AlertRuleConflict):
            rules.update(TENANT_A, USER_A, original.id, **{**edit, "routing": {"channel_ids": [str(uuid4())]}})
        assert rules.get(TENANT_A, USER_A, original.id).etag == resumed.etag
        changed = rules.update(TENANT_A, USER_A, original.id, **edit)
        assert changed.current_version == 2 and changed.name == "Updated rule"
        assert changed.symbol == "BTC_USDT" and changed.status == "ACTIVE"
        with pytest.raises(AlertRulePreconditionFailed):
            rules.update(TENANT_A, USER_A, original.id, **edit)
        rules.create(TENANT_A, USER_A, name="Taken name", condition=edit["condition"], routing={})
        with pytest.raises(AlertRuleConflict):
            rules.update(TENANT_A, USER_A, original.id, **{**edit, "name": "Taken name", "expected_etag": changed.etag})
        assert rules.get(TENANT_A, USER_A, original.id).current_version == 2
        paused = rules.pause(TENANT_A, USER_A, original.id, expected_etag=changed.etag)
        changed = rules.update(TENANT_A, USER_A, original.id, **{**edit, "expected_etag": paused.etag})
        assert changed.status == "PAUSED" and changed.current_version == 3
        with pytest.raises(AlertRulePreconditionFailed):
            rules.delete(TENANT_A, USER_A, original.id, expected_etag=paused.etag)
        rules.delete(TENANT_A, USER_A, original.id, expected_etag=changed.etag)
        with pytest.raises(AlertRuleNotFound):
            rules.get(TENANT_A, USER_A, original.id)
        with pool.transaction(TENANT_A, USER_A) as conn:
            versions = conn.execute("SELECT version,condition_json FROM alert_rule_versions WHERE alert_rule_id=%s ORDER BY version", (original.id,)).fetchall()
            assert len(versions) == 3
            assert versions[0][1] == dict(original.condition)
            assert conn.execute("SELECT status FROM alert_rules WHERE id=%s", (original.id,)).fetchone()[0] == "DELETED"
        notifications = PostgresNotificationRepository(pool)
        first = notifications.list_notification_events(TENANT_A, USER_A, limit=20)
        second = notifications.list_notification_events(TENANT_A, USER_A, limit=20, offset=20)
        assert len(first) == 20 and len(second) == 5
        assert not {r.id for r in first} & {r.id for r in second}
        assert notifications.notification_counts(TENANT_A, USER_A) == (25, 5)
        assert notifications.notification_counts(TENANT_A, USER_A, "FAILED") == (5, 5)
        assert all(r.status == "FAILED" for r in notifications.list_notification_events(TENANT_A, USER_A, status="FAILED"))
        assert notifications.list_notification_events(TENANT_B, USER_A) == []
        assert notifications.notification_counts(TENANT_B, USER_A) == (0, 0)
    finally:
        pool.close()
