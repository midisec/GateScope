from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import UUID

import pytest

from gatescope.adapters.gate_paper_market import MarketSnapshot, PaperMarketUnavailable
from gatescope.application.paper_order_monitor import PaperOrderMonitor
from gatescope.domain.paper_matching import BookLevel, ContractSpec, OrderBook

NOW = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)


def identity(number):
    return UUID(int=number)


def target(number=1, symbol="SOL_USDT"):
    return {"tenant_id": identity(number), "owner_user_id": identity(number + 100),
            "account_id": identity(number + 200), "instrument_id": identity(999), "symbol": symbol}


def snapshot(at=NOW, symbol="SOL_USDT", *, age=0):
    book = OrderBook(symbol, (BookLevel(D("99.99"), D("100")),), (BookLevel(D("100.01"), D("100")),),
                     at - timedelta(seconds=age), at - timedelta(seconds=age),
                     updated_at=at - timedelta(seconds=age), sequence_id="100")
    return MarketSnapshot(ContractSpec(symbol, D("1"), D("0.01"), D("1"), D("1000"), D("0.001")),
                          book, D("100"), None, at)


class Store:
    def __init__(self, targets=None):
        self.targets = targets if targets is not None else [target()]
        self.calls = []
        self.protect_failure = set()
        self.order_failure = set()
        self.pending_failure = set()
        self.on_protect = None

    def pending_targets(self, limit=100):
        assert limit == 100
        return self.targets

    def protect_positions(self, tenant, owner, instrument, quote, **kwargs):
        self.calls.append(("protect", tenant, owner, instrument, quote, kwargs))
        if self.on_protect:
            self.on_protect(tenant)
        if tenant in self.protect_failure:
            raise RuntimeError("not logged private data")
        return {"checked": 1, "items": []}

    def list_orders(self, tenant, owner, **kwargs):
        self.calls.append(("list", tenant, owner, kwargs))
        assert kwargs["status"] == "ACTIVE"
        if tenant in self.order_failure:
            raise RuntimeError("not logged")
        return {"items": [{"id": identity(10000 + tenant.int)}]}

    def process_pending(self, tenant, owner, order_id, quote, **kwargs):
        self.calls.append(("pending", tenant, owner, order_id, quote, kwargs))
        if tenant in self.pending_failure:
            raise RuntimeError("not logged")
        if quote is None:
            return {"fills": [], "reason": "MARKET_UNAVAILABLE"}
        return {"fills": [{"contracts": 1}], "position": {"status": "OPEN"}}


class Market:
    def __init__(self, values=None):
        self.calls = []
        self.values = list(values) if values is not None else [snapshot()]

    def get(self, symbol):
        self.calls.append(symbol)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def monitor(store=None, market=None, *, ticks=None):
    ticks = ticks if ticks is not None else [0.0]
    return PaperOrderMonitor(store if store is not None else Store(), market if market is not None else Market(),
                             clock=lambda: NOW + timedelta(seconds=ticks[0]), monotonic=lambda: ticks[0])


def test_protects_all_selected_owners_before_any_ordinary_order_and_shares_public_quote():
    store, market = Store([target(1), target(2)]), Market()
    result = monitor(store, market).run_once()
    assert [call[0] for call in store.calls] == ["protect", "protect", "list", "pending", "list", "pending"]
    assert market.calls == ["SOL_USDT"]
    assert result.targets == 2 and result.protected == 2 and result.attempted == 2 and result.fills == 2
    assert result.failed == 0 and not result.budget_exhausted
    for call in store.calls:
        assert call[2] == identity(call[1].int + 100)
    assert store.calls[0][5]["account_id"] == identity(201)
    assert store.calls[1][5]["account_id"] == identity(202)


def test_full_positions_are_monitored_even_without_pending_orders():
    store = Store()
    store.list_orders = lambda *_args, **_kwargs: {"items": []}
    result = monitor(store).run_once()
    assert result.protected == 1 and result.attempted == 0


def test_duplicate_timeframe_targets_do_not_process_same_owner_instrument_twice():
    store = Store([{**target(), "timeframe": "5m"}, {**target(), "timeframe": "1h"}])
    assert monitor(store).run_once().targets == 1


def test_one_owner_protection_failure_skips_its_entries_but_not_other_owners():
    store = Store([target(1), target(2)])
    store.protect_failure = {identity(1)}
    result = monitor(store).run_once()
    assert result.failed == 1 and result.attempted == 1
    assert [(row[0], row[1]) for row in store.calls if row[0] == "pending"] == [("pending", identity(2))]


@pytest.mark.parametrize("failure", ["order_failure", "pending_failure"])
def test_one_owner_list_or_fill_failure_is_isolated(failure):
    store = Store([target(1), target(2)])
    setattr(store, failure, {identity(1)})
    result = monitor(store).run_once()
    assert result.failed == 1 and result.fills == 1


def test_public_failure_passes_none_not_zero_and_allows_expiry_or_waiting_status():
    store, market = Store([target(1), target(2)]), Market([PaperMarketUnavailable("GATE_RATE_LIMITED", retry_after=30)])
    result = monitor(store, market).run_once()
    assert market.calls == ["SOL_USDT"] and result.unavailable == 1
    assert result.attempted == 2 and result.fills == 0
    assert all(call[4] is None for call in store.calls if call[0] in {"protect", "pending"})


def test_retry_after_is_respected_across_cycles_without_sleeping():
    ticks = [0.0]
    market = Market([PaperMarketUnavailable("GATE_RATE_LIMITED", retry_after=30), snapshot(NOW + timedelta(seconds=31))])
    worker = monitor(Store(), market, ticks=ticks)
    worker.run_once()
    ticks[0] = 20
    worker.run_once()
    assert len(market.calls) == 1
    ticks[0] = 31
    worker.run_once()
    assert len(market.calls) == 2


def test_stale_source_cannot_be_refreshed_by_local_capture_time():
    stale = snapshot(age=6)
    stale = replace(stale, book=replace(stale.book, captured_at=NOW, quote_id=""))
    store = Store()
    result = monitor(store, Market([stale])).run_once()
    assert result.unavailable == 1 and result.fills == 0
    assert store.calls[0][4] is None


def test_quote_refreshes_if_protection_work_ages_it_before_entries():
    ticks = [0.0]
    store = Store()
    store.on_protect = lambda _: ticks.__setitem__(0, 5.1)
    market = Market([snapshot(), snapshot(NOW + timedelta(seconds=5.1))])
    result = monitor(store, market, ticks=ticks).run_once()
    assert len(market.calls) == 2 and result.fills == 1
    assert store.calls[-1][4].book.captured_at == NOW + timedelta(seconds=5.1)


def test_supplied_now_advances_with_elapsed_time_not_frozen_at_cycle_start():
    ticks = [0.0]
    store = Store()
    store.on_protect = lambda _: ticks.__setitem__(0, 5.1)
    market = Market([snapshot(), snapshot(NOW + timedelta(seconds=5.1))])
    monitor(store, market, ticks=ticks).run_once(now=NOW)
    assert store.calls[-1][-1]["now"] == NOW + timedelta(seconds=5.1)


def test_bad_instrument_response_cannot_cross_fill_other_symbol():
    store = Store()
    result = monitor(store, Market([snapshot(symbol="BTC_USDT")])).run_once()
    assert result.unavailable == 1 and result.fills == 0


def test_budget_stops_scheduling_and_next_cycle_rotates_to_another_owner():
    ticks = [0.0]
    store = Store([target(1), target(2)])
    store.on_protect = lambda _: ticks.__setitem__(0, ticks[0] + 9)
    market = Market([snapshot(), snapshot(NOW + timedelta(seconds=9))])
    worker = monitor(store, market, ticks=ticks)
    result = worker.run_once()
    assert result.targets == 1 and result.budget_exhausted and result.attempted == 0
    worker.run_once()
    assert [row[1] for row in store.calls if row[0] == "protect"] == [identity(1), identity(2)]


def test_failed_head_of_bounded_window_does_not_starve_next_target():
    store = Store([target(1), target(2), target(3)])
    store.protect_failure = {identity(1), identity(2), identity(3)}
    market = Market([snapshot(), snapshot(), snapshot()])
    worker = monitor(store, market)
    for _ in range(3):
        worker.run_once(target_limit=1)
    assert [row[1] for row in store.calls if row[0] == "protect"] == [identity(1), identity(2), identity(3)]


def test_malformed_target_is_skipped_without_attempting_another_owner():
    store = Store([{"tenant_id": "not uuid"}, target()])
    result = monitor(store).run_once()
    assert result.failed == 1 and result.targets == 1


@pytest.mark.parametrize("kwargs", [
    {"target_limit": 0}, {"target_limit": True}, {"orders_per_target": 11}, {"positions_per_target": 11},
    {"budget_seconds": 0}, {"budget_seconds": float("nan")}, {"budget_seconds": True}, {"now": NOW.replace(tzinfo=None)},
])
def test_invalid_monitor_budgets_or_naive_time_rejected(kwargs):
    with pytest.raises(ValueError):
        monitor().run_once(**kwargs)


def test_worker_new_module_failure_cannot_stop_legacy_monitor(monkeypatch):
    import apps.paper_worker.main as worker
    calls = []
    class Pool:
        def __init__(self, *_args, **_kwargs): pass
        def open(self): calls.append("open")
        def close(self): calls.append("close")
    class NewMonitor:
        def __init__(self, *_args, **_kwargs): pass
        def run_once(self):
            calls.append("new")
            raise RuntimeError("not logged secret")
    class OldMonitor:
        def __init__(self, *_args, **_kwargs): pass
        def run_once(self):
            calls.append("old")
            return SimpleNamespace(tenants=0, attempted=0, closed=0, failed=0)
    class Shadow:
        def __init__(self, *_args, **_kwargs): pass
        def heartbeat(self, *_args, **_kwargs): pass
        def run_once(self): return SimpleNamespace(owners=0, captured=0, attempted=0, closed=0, failed=0)
    class QuoteSource:
        def close(self): calls.append("market_close")
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://fixture/gatescope_test")
    monkeypatch.setenv("GATESCOPE_WORKER_ONCE", "1")
    monkeypatch.setattr(worker, "PostgresPool", Pool)
    monkeypatch.setattr(worker, "PostgresPaperRepository", lambda *_: None)
    monkeypatch.setattr(worker, "PostgresPaperOrderRepository", lambda *_: None)
    monkeypatch.setattr(worker, "PaperOrderMonitor", NewMonitor)
    monkeypatch.setattr(worker, "PaperTradeMonitor", OldMonitor)
    monkeypatch.setattr(worker, "ShadowTradeRepository", Shadow)
    monkeypatch.setattr(worker, "ShadowTradeMonitor", Shadow)
    monkeypatch.setattr(worker, "GatePaperMarket", QuoteSource)
    monkeypatch.setattr(worker, "install_stop_handlers", lambda *_: None)
    monkeypatch.setattr(worker, "event", lambda name, **_kwargs: calls.append(name))
    worker.main()
    assert calls.index("new") < calls.index("old")
    assert "paper.orders.cycle_failed" in calls and "market_close" in calls and "close" in calls
    assert calls.count("open") == calls.count("close") == 3


def test_production_scheduling_runs_protection_while_shadow_is_still_busy(monkeypatch):
    import threading
    import apps.paper_worker.main as worker
    shadow_started, release_shadow, shadow_finished = (threading.Event() for _ in range(3))
    calls = []
    def plane(name, database_url, worker_id, subject_id, stopped, **kwargs):
        if name == "shadow":
            shadow_started.set()
            assert release_shadow.wait(timeout=2)
            shadow_finished.set()
        elif name == "orders":
            assert shadow_started.wait(timeout=2)
            assert not shadow_finished.is_set()
            calls.append("protection_not_blocked")
            stopped.set()
            release_shadow.set()
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://fixture/gatescope_test")
    monkeypatch.delenv("GATESCOPE_WORKER_ONCE", raising=False)
    monkeypatch.setattr(worker, "run_plane", plane)
    monkeypatch.setattr(worker, "install_stop_handlers", lambda *_: None)
    monkeypatch.setattr(worker, "event", lambda *_args, **_kwargs: None)
    worker.main()
    assert calls == ["protection_not_blocked"] and shadow_finished.is_set()
