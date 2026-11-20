from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest

from gatescope.adapters.postgres_paper import PaperNotFound
from tests.integration.test_paper_orders import paperdb, quote, opening  # noqa: F401
from tests.integration.test_postgres_rls import TENANT_A, USER_A, USER_B


def test_cursor_excludes_newer_insertions_and_rejects_changed_owner_or_filter(paperdb):  # noqa: F811
    _, repo, _, account, _ = paperdb
    start = datetime.now(timezone.utc) - timedelta(seconds=30)
    ids = []
    for i in range(3):
        at = start + timedelta(seconds=i)
        result = repo.submit(
            TENANT_A, USER_A, opening(account, contracts=1), quote(at, sequence=str(i + 1)), now=at
        )
        ids.append(result["order"]["id"])
    page = repo.list_orders(TENANT_A, USER_A, account_id=account, limit=2, pagination="CURSOR")
    assert [r["id"] for r in page["items"]] == ids[::-1][:2]
    assert page["next_cursor"] and page["next_offset"] is None
    at = datetime.now(timezone.utc)
    repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at, sequence="4"), now=at)
    tail = repo.list_orders(
        TENANT_A, USER_A, account_id=account, limit=2, pagination="CURSOR", cursor=page["next_cursor"]
    )
    assert [r["id"] for r in tail["items"]] == ids[:1]
    assert tail["snapshot_at"] == page["snapshot_at"] and not tail["has_more"]
    for kwargs in ({"status": "FILLED"}, {"account_id": uuid4()}):
        with pytest.raises(ValueError):
            repo.list_orders(
                TENANT_A, USER_A, **{"account_id": account, **kwargs}, cursor=page["next_cursor"]
            )
    with pytest.raises(ValueError):
        repo.list_orders(TENANT_A, USER_B, account_id=account, cursor=page["next_cursor"])


def test_order_history_continues_beyond_detail_first100_and_is_owner_scoped(paperdb):  # noqa: F811
    database, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc) - timedelta(seconds=30)
    result = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    order = UUID(result["order"]["id"])
    with psycopg.connect(database) as conn:
        for i in range(110):
            conn.execute(
                """INSERT INTO paper_order_events(id,tenant_id,owner_user_id,order_id,event_type,payload_json,created_at)
              VALUES(%s,%s,%s,%s,'TEST_HISTORY','{}',%s)""",
                (uuid4(), TENANT_A, USER_A, order, at + timedelta(microseconds=i + 1)),
            )
    detail = repo.get_order(TENANT_A, USER_A, order)
    assert len(detail["events"]) == 100 and detail["events_next_cursor"]
    tail = repo.order_history(TENANT_A, USER_A, order, kind="events", cursor=detail["events_next_cursor"])
    identities = [r["id"] for r in detail["events"] + tail["items"]]
    assert len(identities) > 110 and len(identities) == len(set(identities))
    assert not tail["has_more"]
    with pytest.raises(PaperNotFound):
        repo.order_history(TENANT_A, USER_B, order, kind="events")


def test_detail_and_next_history_cursor_share_one_insertion_cutoff(paperdb):  # noqa: F811
    database, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc) - timedelta(seconds=30)
    result = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    order = UUID(result["order"]["id"])
    # submit advances the supplied clock by actual lock/execution latency. Put
    # the synthetic history after its returned completion, not 100us after the
    # request began (which can exclude ACCEPTED/MATCHED on a busy test runner).
    history_start = datetime.fromisoformat(result["order"]["updated_at"]) + timedelta(seconds=1)
    cutoff = history_start + timedelta(microseconds=100)
    later = []
    with psycopg.connect(database) as conn:
        for i in range(110):
            identifier = uuid4()
            timestamp = history_start + timedelta(microseconds=i+1)
            if timestamp > cutoff:
                later.append(str(identifier))
            conn.execute("""INSERT INTO paper_order_events(id,tenant_id,owner_user_id,order_id,event_type,payload_json,created_at)
              VALUES(%s,%s,%s,%s,'TEST_CUTOFF','{}',%s)""", (identifier, TENANT_A, USER_A, order, timestamp))
    detail = repo.get_order(TENANT_A, USER_A, order, now=cutoff)
    assert len(detail["events"]) == 100 and detail["events_next_cursor"]
    tail = repo.order_history(TENANT_A, USER_A, order, kind="events", cursor=detail["events_next_cursor"])
    events = detail["events"] + tail["items"]
    assert len(events) == 102  # original ACCEPTED/MATCHED plus 100 old events
    assert not set(later).intersection(event["id"] for event in events)
    assert all(datetime.fromisoformat(event["created_at"]) <= cutoff for event in events)
