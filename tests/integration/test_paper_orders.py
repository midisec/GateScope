from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege

from gatescope.adapters.gate_paper_market import MarketSnapshot
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PostgresPaperRepository, PaperConflict, PaperNotFound, PaperPreconditionFailed
from gatescope.adapters.postgres_paper_orders import PostgresPaperOrderRepository
from gatescope.domain.paper_matching import BookLevel, ContractSpec, OrderBook
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, INSTRUMENT, seed_two_tenants


@pytest.fixture
def paperdb(migrated_database):
    seed_two_tenants(migrated_database)
    admin = PostgresPool(migrated_database, min_size=1, max_size=3)
    mcp = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_mcp"), min_size=1, max_size=3)
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_paper"), min_size=1, max_size=2)
    for pool in (admin, mcp, worker):
        pool.open()
    accounts = PostgresPaperRepository(admin)
    account = accounts.create_account(TENANT_A, USER_A, name="MCP Paper", base_currency="USDT", initial_equity=Decimal(10000))
    other = accounts.create_account(TENANT_A, USER_B, name="Other Owner", base_currency="USDT", initial_equity=Decimal(10000))
    try:
        yield migrated_database, PostgresPaperOrderRepository(mcp), PostgresPaperOrderRepository(worker), account.id, other.id
    finally:
        for pool in (admin, mcp, worker):
            pool.close()
        # Only this explicitly guarded throwaway database may discard test facts.
        # Production migration rollback refuses if even one order fact exists.
        assert psycopg.conninfo.conninfo_to_dict(migrated_database)["dbname"] == "gatescope_test"
        with psycopg.connect(migrated_database) as connection:
            connection.execute("TRUNCATE paper_order_commands,paper_order_events,paper_order_fills,paper_positions,paper_orders,paper_quote_consumption,paper_decision_snapshots CASCADE")


def quote(at, *, bid="99", ask="100", depth=100, sequence="1"):
    spec = ContractSpec("BTC_USDT", Decimal(1), Decimal(".01"), Decimal(1), Decimal(10000), Decimal(".0005"))
    book = OrderBook("BTC_USDT", (BookLevel(Decimal(bid), Decimal(depth)),), (BookLevel(Decimal(ask), Decimal(depth)),), at, at, sequence_id=sequence)
    return MarketSnapshot(spec, book, Decimal(ask), None, at)


def opening(account_id, **overrides):
    request = {"account_id": str(account_id), "instrument_id": str(INSTRUMENT), "symbol": "BTC_USDT", "timeframe": "15m", "side": "LONG",
               "contracts": 3, "stop_price": "95", "take_profit_price": "120", "idempotency_key": "test-"+str(uuid4()), "source": "MCP"}
    request.update(overrides)
    return request


def test_atomic_shared_cash_owner_rls_fills_partial_close_and_tg_outbox(paperdb):
    database, repo, worker, account, other = paperdb
    at = datetime.now(timezone.utc)
    request = opening(account)
    result = repo.submit(TENANT_A, USER_A, request, quote(at), now=at)
    order, position_id = result["order"], UUID(result["order"]["position_id"])
    assert order["status"] == "FILLED" and order["filled_contracts"] == 3
    assert repo.replay_request(TENANT_A, USER_A, request)["replayed"]
    assert repo.submit(TENANT_A, USER_A, request, None, now=at)["replayed"]
    with pytest.raises(PaperConflict, match="IDEMPOTENCY"):
        repo.submit(TENANT_A, USER_A, {**request, "contracts": 2}, quote(at), now=at)
    with pytest.raises(PaperNotFound):
        repo.account_snapshot(TENANT_A, USER_A, other)
    with pytest.raises(PaperNotFound):
        repo.get_position(TENANT_B, USER_B, position_id)
    with repo.pool.transaction(TENANT_A, USER_A) as connection:
        assert connection.execute("SELECT count(*) FROM paper_accounts").fetchone() == (1,)
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_accounts SET initial_equity=1000000")
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_order_fills SET price=1")
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_orders SET request_json='{}'")
        with pytest.raises(InsufficientPrivilege), connection.transaction():
            connection.execute("UPDATE paper_positions SET stop_price=1")
    closing = {"contracts": 2, "idempotency_key": "close-two-contracts"}
    close = repo.close(TENANT_A, USER_A, position_id, closing, quote(at, bid="110", ask="111", sequence="2"), now=at)
    assert close["order"]["status"] == "FILLED"
    assert close["fills"][0]["realized_pnl"] == "20.000000000000000000"
    position = repo.get_position(TENANT_A, USER_A, position_id)
    assert position["remaining_contracts"] == 1 and position["closed_contracts"] == 2
    snapshot = repo.account_snapshot(TENANT_A, USER_A, account, now=at)
    assert Decimal(snapshot["balance"]["cash_balance"]) == Decimal("10019.74")
    assert snapshot["balance"]["unrealized_pnl"] is None
    targets = worker.pending_targets()
    assert any(row["account_id"] == account for row in targets)
    protected = worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="94", ask="95", sequence="3"), now=at)
    assert protected["items"][0]["order"]["reason"] == "STOP_LOSS"
    assert repo.get_position(TENANT_A, USER_A, position_id)["status"] == "CLOSED"
    assert Decimal(repo.account_snapshot(TENANT_A, USER_A, account, now=at)["balance"]["cash_balance"]) == Decimal("10013.693")
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM outbox_events WHERE event_type='paper.order.fill.v1'").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM paper_order_fills").fetchone() == (3,)
        assert connection.execute("SELECT bool_and(payload_json->>'environment'='PAPER') FROM outbox_events WHERE event_type='paper.order.fill.v1'").fetchone() == (True,)


def test_depth_global_reservation_concurrent_retries_and_no_stale_or_fok_fills(paperdb):
    database, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    thin = quote(at, depth=20)
    request = opening(account)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: repo.submit(TENANT_A, USER_A, request, thin, now=at), range(2)))
    assert sorted(row["replayed"] for row in results) == [False, True]
    result = results[0]
    assert result["order"]["status"] == "CANCELED" and result["order"]["filled_contracts"] == 2
    second = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), thin, now=at)
    assert second["order"]["filled_contracts"] == 0
    assert second["order"]["reserved_notional"] == "0"
    with pytest.raises(PaperPreconditionFailed, match="QUOTE_STALE"):
        repo.submit(TENANT_A, USER_A, opening(account, contracts=1), thin, now=at+timedelta(seconds=6))
    fok = repo.submit(TENANT_A, USER_A, opening(account, contracts=3, stop_price="97", order_type="LIMIT", time_in_force="FOK", limit_price="100"), quote(at, depth=20, sequence="2"), now=at)
    assert fok["order"]["filled_contracts"] == 0
    assert fok["order"]["status"] == "CANCELED"
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT sum(contracts) FROM paper_order_fills").fetchone() == (Decimal(2),)
        assert connection.execute("SELECT count(*) FROM outbox_events WHERE event_type='paper.order.fill.v1'").fetchone() == (1,)


def test_limit_reservations_expiry_partial_lot_close_prevents_overfill_and_role_revocation(paperdb):
    database, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    pending = repo.submit(TENANT_A, USER_A, opening(account, contracts=2, order_type="LIMIT", limit_price="99", ttl_seconds=60), quote(at), now=at)
    assert pending["order"]["status"] == "OPEN"
    assert Decimal(repo.account_snapshot(TENANT_A, USER_A, account)["balance"]["order_reserved_margin"]) == 198
    expired = worker.process_pending(TENANT_A, USER_A, UUID(pending["order"]["id"]), None, now=at+timedelta(seconds=61))
    assert expired["order"]["status"] == "EXPIRED"
    partial = repo.submit(TENANT_A, USER_A, opening(account, contracts=3, order_type="LIMIT", limit_price="100"), quote(at, depth=10, sequence="2"), now=at)
    assert partial["order"]["status"] == "PARTIALLY_FILLED"
    position_id = UUID(partial["order"]["position_id"])
    close = repo.close(TENANT_A, USER_A, position_id, {"contracts": 1, "idempotency_key": "limit-close-1", "order_type": "LIMIT", "limit_price": "110"}, quote(at), now=at)
    assert close["order"]["status"] == "OPEN"
    assert repo.get_order(TENANT_A, USER_A, UUID(partial["order"]["id"]))["status"] == "CANCELED"
    with pytest.raises(PaperConflict, match="REDUCE_ONLY"):
        repo.close(TENANT_A, USER_A, position_id, {"contracts": 1, "idempotency_key": "over-close-1"}, quote(at), now=at)
    repo.cancel(TENANT_A, USER_A, UUID(close["order"]["id"]), "cancel-close-1", now=at)
    assert repo.get_position(TENANT_A, USER_A, position_id)["reserved_close_contracts"] == 0
    with pytest.raises(PaperConflict, match="IDEMPOTENCY"):
        repo.cancel(TENANT_A, USER_A, UUID(partial["order"]["id"]), "cancel-close-1", now=at)
    with psycopg.connect(database) as connection:
        connection.execute("UPDATE memberships SET role='VIEWER' WHERE tenant_id=%s AND user_id=%s", (TENANT_A, USER_A))
    with pytest.raises(PermissionError):
        repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    assert worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="94", ask="95", sequence="4"), now=at)["items"][0]["order"]["status"] == "FILLED"


def test_protection_latch_survives_missing_liquidity_network_and_rebound(paperdb):
    _, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    opened = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    position = UUID(opened["order"]["position_id"])
    stopped = worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="94", ask="95", depth=1, sequence="2"), now=at)
    assert stopped["items"][0]["order"]["filled_contracts"] == 0
    assert repo.get_position(TENANT_A, USER_A, position)["trigger_reason"] == "STOP_LOSS"
    worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, None, now=at+timedelta(seconds=1))
    assert repo.get_position(TENANT_A, USER_A, position)["protection_status"] == "WAITING_MARKET"
    rebound = worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="105", ask="106", sequence="3"), now=at)
    assert rebound["items"][0]["order"]["reason"] == "STOP_LOSS"
    assert repo.get_position(TENANT_A, USER_A, position)["status"] == "CLOSED"


def test_omitted_close_quantity_is_locked_all_available_and_retry_does_not_recompute(paperdb):
    _, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc)
    opened = repo.submit(TENANT_A, USER_A, opening(account, contracts=2), quote(at), now=at)
    position = UUID(opened["order"]["position_id"])
    request = {"idempotency_key": "close-all-with-stable-key"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: repo.close(TENANT_A, USER_A, position, request, quote(at, sequence="2"), now=at), range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert all(result["order"]["contracts"] == 2 for result in results)
    assert repo.get_position(TENANT_A, USER_A, position)["status"] == "CLOSED"
    assert repo.replay_request(TENANT_A, USER_A, request, effect="CLOSE", position_id=position)["replayed"]
    with pytest.raises(PaperConflict, match="IDEMPOTENCY"):
        repo.replay_request(TENANT_A, USER_A, {**request, "contracts": 2}, effect="CLOSE", position_id=position)


def test_risk_rejection_is_durable_and_account_lock_prevents_concurrent_budget_double_spend(paperdb):
    database, repo, _, account, _ = paperdb
    at = datetime.now(timezone.utc)
    requests = [opening(account), opening(account)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda request: repo.submit(TENANT_A, USER_A, request, quote(at), now=at), requests))
    assert sorted(result["order"]["status"] for result in results) == ["FILLED", "REJECTED"]
    rejected = next(result for result in results if result["order"]["status"] == "REJECTED")
    assert not rejected["accepted"] and rejected["order"]["reason"] == "ACCOUNT_RISK_LIMIT"
    assert rejected["fills"] == []
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM paper_orders").fetchone() == (2,)
        assert connection.execute("SELECT sum(contracts) FROM paper_order_fills").fetchone() == (Decimal(3),)
    detail = repo.get_order(TENANT_A, USER_A, UUID(rejected["order"]["id"]))
    assert detail["events"][0]["event_type"] == "REJECTED"
    assert not detail["events_has_more"] and not detail["fills_has_more"]


def test_protective_attempt_rollback_does_not_erase_observed_stop_or_reserve(paperdb, monkeypatch):
    _, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    opened = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    position_id = UUID(opened["order"]["position_id"])
    original = worker._match

    def stale_after_latch(*args, **kwargs):
        raise PaperPreconditionFailed("QUOTE_STALE")

    monkeypatch.setattr(worker, "_match", stale_after_latch)
    attempted = worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="94", ask="95", sequence="2"), now=at)
    assert attempted["items"][0]["status"] == "WAITING_MARKET"
    position = repo.get_position(TENANT_A, USER_A, position_id)
    assert position["trigger_reason"] == "STOP_LOSS"
    assert position["reserved_close_contracts"] == 0 and position["remaining_contracts"] == 1
    assert len(repo.list_orders(TENANT_A, USER_A)["items"]) == 1
    monkeypatch.setattr(worker, "_match", original)
    worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="105", ask="106", sequence="3"), now=at)
    assert repo.get_position(TENANT_A, USER_A, position_id)["status"] == "CLOSED"


def test_fresh_stop_observed_before_account_lock_is_latched_but_never_filled_when_stale(paperdb, monkeypatch):
    import gatescope.adapters.postgres_paper_orders as module

    _, repo, worker, account, _ = paperdb
    at = datetime.now(timezone.utc)
    opened = repo.submit(TENANT_A, USER_A, opening(account, contracts=1), quote(at), now=at)
    position_id = UUID(opened["order"]["position_id"])
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock["elapsed"]))
    original_account = worker._account

    def delayed_account(*args, **kwargs):
        account_row = original_account(*args, **kwargs)
        clock["elapsed"] = 6.0
        return account_row

    monkeypatch.setattr(worker, "_account", delayed_account)
    attempted = worker.protect_positions(TENANT_A, USER_A, INSTRUMENT, quote(at, bid="94", ask="95", sequence="2"), now=at)
    assert attempted["items"][0]["status"] == "WAITING_MARKET"
    position = repo.get_position(TENANT_A, USER_A, position_id)
    assert position["trigger_reason"] == "STOP_LOSS" and position["status"] == "OPEN"
    assert position["remaining_contracts"] == 1 and position["reserved_close_contracts"] == 0
    # The simulated lock wait advances event time, not the actual wall clock.
    # Read at that same virtual instant so the insertion snapshot includes it.
    detail = repo.get_order(TENANT_A, USER_A, UUID(opened["order"]["id"]), now=at+timedelta(seconds=clock["elapsed"]))
    events = [event for event in detail["events"] if event["event_type"] == "PROTECTION_TRIGGERED"]
    assert len(events) == 1 and events[0]["payload_json"]["fill_guaranteed"] is False
    assert len(repo.list_fills(TENANT_A, USER_A)["items"]) == 1  # Only the original opening fill.
