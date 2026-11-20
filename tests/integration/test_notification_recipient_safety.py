"""Actual notification workload role; fake transport only, no external sends."""
from dataclasses import replace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.application.notifications import DeliveryOutcome, NotificationDispatcher
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A
from tests.integration.test_telegram_market import setup_data


def seed_channel(database):
    binding = setup_data(database)
    channel = uuid4()
    with psycopg.connect(database) as conn:
        conn.execute(
            """INSERT INTO notification_channels(id,tenant_id,channel_type,alias,status,connection_id,created_at,updated_at)
               VALUES(%s,%s,'TELEGRAM','Recipient safety','ACTIVE',%s,now(),now())""",
            (channel, TENANT_A, binding),
        )
    return channel, binding


def enqueue(database, channel, dispatcher, *, queue_state="PENDING"):
    event = uuid4()
    with psycopg.connect(database) as conn:
        conn.execute(
            """INSERT INTO outbox_events(event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,
                      payload_json,occurred_at,created_at,next_attempt_at)
               VALUES(%s,%s,'NOTIFICATION_CHANNEL',%s,'notification.test.requested.v1',1,%s,now(),now(),now())""",
            (event, TENANT_A, channel, Jsonb({"channel_id": str(channel)})),
        )
        conn.execute(
            """INSERT INTO operations_current(id,tenant_id,actor_user_id,operation_type,resource_id,state,created_at,updated_at)
               VALUES(%s,%s,%s,'NOTIFICATION_TEST',%s,'QUEUED',now(),now())""",
            (event, TENANT_A, USER_A, channel),
        )
    assert dispatcher._expand_outbox(TENANT_A, 20) == 1
    with psycopg.connect(database) as conn:
        conn.execute(
            """UPDATE notification_deliveries SET status=%s,
                      lease_owner=CASE WHEN %s='SENDING' THEN 'old-worker' ELSE NULL END,
                      lease_expires_at=CASE WHEN %s='SENDING' THEN now()-interval '1 minute' ELSE NULL END
               WHERE event_id=%s""",
            (queue_state, queue_state, queue_state, event),
        )
    return event


def assert_failed(database, event):
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT status,safe_error_code,lease_owner FROM notification_deliveries WHERE event_id=%s", (event,),
        ).fetchone() == ("FAILED", "TG_RECIPIENT_INACTIVE", None)
        assert conn.execute(
            "SELECT state,safe_error_code FROM operations_current WHERE id=%s", (event,),
        ).fetchone() == ("FAILED", "TG_RECIPIENT_INACTIVE")


def test_pending_retry_and_expired_claims_stop_for_inactive_members_users_or_tenants(migrated_database):
    channel, _ = seed_channel(migrated_database)
    sent = []

    class Sender:
        def send(self, chat, text, *, reply_markup=None):
            sent.append(chat)
            return DeliveryOutcome(True, None, None, len(sent))

    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    pool.open()
    try:
        dispatcher = NotificationDispatcher(pool, Sender(), worker_id="recipient-safety")
        # Existing queue items, not merely newly expanded notifications, are denied.
        for state, disable, restore in (
            ("PENDING", "UPDATE memberships SET status='REVOKED' WHERE tenant_id=%s AND user_id=%s",
             "UPDATE memberships SET status='ACTIVE' WHERE tenant_id=%s AND user_id=%s"),
            ("RETRY", "UPDATE users SET status='DISABLED' WHERE id=%s",
             "UPDATE users SET status='ACTIVE' WHERE id=%s"),
            ("SENDING", "UPDATE tenants SET status='SUSPENDED' WHERE id=%s",
             "UPDATE tenants SET status='ACTIVE' WHERE id=%s"),
        ):
            event = enqueue(migrated_database, channel, dispatcher, queue_state=state)
            args = (TENANT_A, USER_A) if state == "PENDING" else ((USER_A,) if state == "RETRY" else (TENANT_A,))
            with psycopg.connect(migrated_database) as conn:
                conn.execute(disable, args)
            assert dispatcher.run_once() == 1 and sent == []
            assert_failed(migrated_database, event)
            with psycopg.connect(migrated_database) as conn:
                conn.execute(restore, args)
            assert dispatcher.run_once() == 0  # Reactivation does not resurrect suppressed messages.

        # Read notifications remain available to every ACTIVE role, including VIEWER.
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE memberships SET role='VIEWER' WHERE tenant_id=%s AND user_id=%s", (TENANT_A, USER_A))
        enqueue(migrated_database, channel, dispatcher)
        assert dispatcher.run_once() == 1 and sent == [42]
    finally:
        pool.close()


def test_claimed_batch_is_rechecked_after_revocation_and_cannot_rebind_tenant_chat_or_event(migrated_database):
    channel, _ = seed_channel(migrated_database)
    sent = []

    class Sender:
        def send(self, chat, text, *, reply_markup=None):
            sent.append(chat)
            # Simulate a revocation committed while Telegram is processing the first message.
            with psycopg.connect(migrated_database) as conn:
                conn.execute("UPDATE memberships SET status='REVOKED' WHERE tenant_id=%s AND user_id=%s", (TENANT_A, USER_A))
            return DeliveryOutcome(True, None, None, 1)

    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    pool.open()
    try:
        dispatcher = NotificationDispatcher(pool, Sender(), worker_id="claimed-recipient")
        enqueue(migrated_database, channel, dispatcher)
        enqueue(migrated_database, channel, dispatcher)
        claimed = dispatcher._claim_deliveries(TENANT_A, 20)
        assert len(claimed) == 2 and dispatcher._recipient_active(claimed[0])
        for altered in (
            replace(claimed[0], tenant_id=TENANT_B),
            replace(claimed[0], telegram_chat_id=43),
            replace(claimed[0], event_id=uuid4()),
        ):
            assert not dispatcher._recipient_active(altered)
        # Prior slow network calls can age a batch lease. Keep the existing
        # dispatch semantics unless another worker has actually taken ownership.
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE notification_deliveries SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", (claimed[0].id,))
        assert dispatcher._recipient_active(claimed[0])
        # A worker must not send a delivery whose lease is owned by a different worker.
        assert not NotificationDispatcher(pool, Sender(), worker_id="other-worker")._recipient_active(claimed[0])
        dispatcher._due_tenants = lambda function, limit: ([] if function == "notification_outbox_due_tenants" else [TENANT_A])
        dispatcher._claim_deliveries = lambda tenant, size: claimed
        assert dispatcher.run_once() == 2 and sent == [42]
        assert_failed(migrated_database, claimed[1].event_id)
        with psycopg.connect(migrated_database) as conn:
            assert conn.execute("SELECT status FROM notification_deliveries WHERE event_id=%s", (claimed[0].event_id,)).fetchone() == ("SENT",)
    finally:
        pool.close()
