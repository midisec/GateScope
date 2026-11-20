"""Real PostgreSQL owner permissions, frozen facts and atomic PAPER linkage."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres_paper import PaperConflict, PaperNotFound, PaperPreconditionFailed
from gatescope.adapters.postgres_worker_health import PostgresWorkerHealth
from tests.integration.test_paper_orders import opening, paperdb, quote
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, TENANT_B, USER_A, USER_B


def test_decision_frozen_owner_isolated_and_carried_to_close_without_duplication(paperdb):
    database, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    reference = {"context_id": f"paper-test-{uuid4()}", "candidate_id": "double-bottom-1", "sequence": 1}
    original = {"id": reference["candidate_id"], "phase": "FORMING", "first": {"forecast": {"target": {"price": 120}}}}
    with psycopg.connect(database) as connection:
        connection.execute("""INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
          version,source_mode,phase,observed_time,recorded_at,event_json)
          VALUES(%s,%s,%s,%s,'15m','test-1','RECORDED','FORMING',%s,%s,%s)""",
          (*reference.values(), INSTRUMENT, at-timedelta(seconds=2), at-timedelta(seconds=1), Jsonb(original)))
    request = opening(account, decision_context={"observed_at": at.isoformat(), "rationale": "形态初始预测，突破前仍可能失败",
                                                "analysis_model": "test-client", "pattern_events": [reference]})
    result = repo.submit(TENANT_A, USER_A, request, quote(at), now=at)
    order = result["order"]
    assert order["decision_status"] == "LINKED" and order["decision_snapshot_id"]
    detail = repo.get_order(TENANT_A, USER_A, UUID(order["id"]))
    saved = detail["decision_snapshot"]["evidence_json"]
    assert saved["server_evidence"]["pattern_events"][0]["record"] == original
    assert saved["server_evidence"]["data_status"] == "NO_CACHED_CANDLES"
    assert saved["client_analysis"]["analysis_model"] == "test-client"
    assert repo.submit(TENANT_A, USER_A, request, None, now=at)["replayed"]
    with pytest.raises(PaperConflict, match="IDEMPOTENCY"):
        repo.submit(TENANT_A, USER_A, {**request, "decision_context": {**request["decision_context"], "rationale": "different"}}, quote(at), now=at)
    with pytest.raises(PaperNotFound):
        repo.get_order(TENANT_B, USER_B, UUID(order["id"]))
    with repo.pool.transaction(TENANT_A, USER_B) as connection:
        assert connection.execute("SELECT count(*) FROM paper_decision_snapshots").fetchone()[0] == 0
    with repo.pool.transaction(TENANT_A, USER_A) as connection:
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_decision_snapshots SET evidence_json='{}'")
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_orders SET decision_snapshot_id=NULL")
    closed = repo.close(TENANT_A, USER_A, UUID(order["position_id"]), {"idempotency_key": "close-original-decision"},
                        quote(at, bid="110", ask="111", sequence="2"), now=at)
    assert closed["order"]["decision_snapshot_id"] == order["decision_snapshot_id"]
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM paper_decision_snapshots").fetchone()[0] == 1
        # A later failed transition never changes the exact sequence already linked.
        connection.execute("""INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
          version,source_mode,phase,observed_time,recorded_at,event_json)
          VALUES(%s,%s,2,%s,'15m','test-1','RECORDED','INVALIDATED',%s,%s,%s)""",
          (reference["context_id"], reference["candidate_id"], INSTRUMENT, at, at, Jsonb({"phase": "INVALIDATED"})))
    assert repo.get_order(TENANT_A, USER_A, UUID(order["id"]))["decision_snapshot"] == detail["decision_snapshot"]


def test_unknown_evidence_rolls_back_without_order_or_balance_change(paperdb):
    database, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc)
    request = opening(account, decision_context={"observed_at": at.isoformat(), "rationale": "missing source",
      "pattern_events": [{"context_id": "missing", "candidate_id": "missing", "sequence": 1}]})
    before = repo.account_snapshot(TENANT_A, USER_A, account)["balance"]["cash_balance"]
    with pytest.raises(PaperPreconditionFailed, match="PATTERN_NOT_AVAILABLE"):
        repo.submit(TENANT_A, USER_A, request, quote(at), now=at)
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM paper_decision_snapshots").fetchone()[0] == 0
    assert repo.account_snapshot(TENANT_A, USER_A, account)["balance"]["cash_balance"] == before


def test_business_heartbeat_does_not_treat_failure_as_new_success(paperdb):
    database, _, worker, _, _ = paperdb
    health = PostgresWorkerHealth(worker.pool)
    health.record("paper-order-worker", "fixture", running=True, healthy=True, counters={"protected": 1})
    with psycopg.connect(database) as connection:
        first = connection.execute("SELECT status_json->>'last_success_at' FROM worker_heartbeats WHERE workload='paper-order-worker'").fetchone()[0]
    health.record("paper-order-worker", "fixture", running=True, healthy=False, safe_error="PAPER_ORDER_MARKET_UNAVAILABLE")
    with psycopg.connect(database) as connection:
        current = connection.execute("SELECT status_json FROM worker_heartbeats WHERE workload='paper-order-worker'").fetchone()[0]
    assert current["last_success_at"] == first and current["cycle_status"] == "DEGRADED"
    health.record("paper-order-worker", "fixture", running=False)


def test_snapshot_excludes_later_backfills_and_later_revisions(paperdb):
    database, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc)
    opened = at.replace(minute=at.minute//15*15, second=0, microsecond=0)-timedelta(minutes=30)
    with psycopg.connect(database) as connection:
        for start, revision, price, created in (
            (opened, 1, 100, at-timedelta(seconds=2)),
            (opened, 2, 101, at+timedelta(seconds=10)),
            (opened+timedelta(minutes=15), 1, 102, at+timedelta(seconds=10)),
        ):
            connection.execute("""INSERT INTO bars(venue,market,instrument_id,timeframe,open_time,close_time,
              open,high,low,close,volume,is_final,revision,available_at,payload_hash,created_at)
              VALUES('GATE','FUTURES_USDT',%s,'15m',%s,%s,%s,%s,%s,%s,10,true,%s,%s,%s,%s)""",
              (INSTRUMENT, start, start+timedelta(minutes=15), price, price, price, price, revision,
               start+timedelta(minutes=15), f"fixture-{start}-{revision}", created))
    result = repo.submit(TENANT_A, USER_A, opening(account, decision_context={"observed_at": at.isoformat(),
        "rationale": "Only information actually recorded by observation time"}), quote(at), now=at)
    detail = repo.get_order(TENANT_A, USER_A, UUID(result["order"]["id"]))
    candles = detail["decision_snapshot"]["evidence_json"]["server_evidence"]["candles"]
    assert len(candles) == 1 and candles[0]["revision"] == 1
    assert candles[0]["close"].startswith("100")
