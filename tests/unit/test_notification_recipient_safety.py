"""Pre-send authorization is independent of order execution/protection."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from gatescope.application.notifications import ClaimedDelivery, DeliveryOutcome, NotificationDispatcher


def delivery(number=1):
    return ClaimedDelivery(
        UUID(int=number), UUID(int=10), UUID(int=100+number),
        "notification.test.requested.v1", 1, 42, {},
    )


def dispatcher_for(deliveries, eligibility, sent, completed):
    dispatcher = object.__new__(NotificationDispatcher)
    dispatcher._due_tenants = lambda function, limit: (
        [] if function == "notification_outbox_due_tenants" else [UUID(int=10)]
    )
    dispatcher._claim_deliveries = lambda tenant, size: deliveries
    dispatcher._recipient_active = eligibility
    dispatcher._complete = lambda message, outcome: completed.append((message, outcome))

    def send(chat, message, **kwargs):
        sent.append(chat)
        return DeliveryOutcome(True, None, None, 1)

    dispatcher._sender_for_tenant = lambda tenant: SimpleNamespace(send=send)
    return dispatcher


@pytest.mark.parametrize("event_type", ["notification.test.requested.v1", "paper.order.fill.v1"])
def test_inactive_recipient_is_terminal_and_never_sent(event_type):
    sent, completed = [], []
    item = replace(delivery(), event_type=event_type)
    dispatcher = dispatcher_for([item], lambda _: False, sent, completed)
    assert dispatcher.run_once() == 1
    assert not sent
    assert completed == [(item, DeliveryOutcome(False, None, "TG_RECIPIENT_INACTIVE"))]


def test_already_claimed_batch_rechecks_each_recipient_after_prior_send():
    sent, completed = [], []
    checks = []

    def eligible(message):
        checks.append(message.id)
        # Models a revocation committed while the first network call was pending.
        return not sent

    dispatcher = dispatcher_for([delivery(1), delivery(2)], eligible, sent, completed)
    assert dispatcher.run_once() == 2
    assert checks == [UUID(int=1), UUID(int=2)]
    assert sent == [42]
    assert completed[0][1].sent
    assert completed[1][1].safe_error_code == "TG_RECIPIENT_INACTIVE"


def test_database_failure_is_fail_closed_not_missing_bot_configuration():
    sent, completed = [], []

    def unavailable(_):
        raise RuntimeError("database unavailable")

    dispatcher = dispatcher_for([delivery()], unavailable, sent, completed)
    with pytest.raises(RuntimeError, match="database unavailable"):
        dispatcher.run_once()
    assert not sent and not completed  # Durable lease can be recovered; no external request occurred.


@pytest.mark.parametrize("exists", [False, True])
def test_recipient_lookup_is_tenant_event_chat_and_lease_bound_read_only(exists):
    statements = []
    transactions = []
    item = delivery()

    class Connection:
        def execute(self, statement, parameters=()):
            statements.append((" ".join(statement.split()), parameters))
            return SimpleNamespace(fetchone=lambda: (1,) if exists else None)

    class Pool:
        @contextmanager
        def transaction(self, tenant, subject):
            transactions.append((tenant, subject))
            yield Connection()

    dispatcher = NotificationDispatcher(Pool(), object(), worker_id="recipient-test")
    assert dispatcher._recipient_active(item) is exists
    assert transactions == [(item.tenant_id, dispatcher.subject_id)]
    assert statements[0] == ("SET LOCAL statement_timeout='3s'", ())
    query, params = statements[1]
    for constraint in (
        "u.status='ACTIVE'", "m.status='ACTIVE'", "t.status='ACTIVE'",
        "m.tenant_id=nd.tenant_id", "nd.tenant_id=%s", "nd.event_id=%s",
        "nd.status='SENDING'", "nd.lease_owner=%s",
        "tc.telegram_chat_id=%s", "nc.status='ACTIVE'", "tc.status='ACTIVE'",
        "ar.owner_user_id=tc.user_id", "(oe.payload_json->>'owner_user_id')::uuid=tc.user_id",
    ):
        assert constraint in query
    assert params == (item.id, item.tenant_id, item.event_id, dispatcher.worker_id, item.telegram_chat_id)
    assert query.startswith("SELECT ") and "m.role" not in query
    # Slow prior sends may age this worker's lease; only a changed owner/status
    # invalidates this snapshot. Aging is not evidence of revoked membership.
    assert "lease_expires_at" not in query
    assert "paper_orders" not in query and "paper_positions" not in query
