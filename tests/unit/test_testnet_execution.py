from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Mapping

import pytest

from gatescope_executor.gate_testnet import (
    GateContractSpec,
    GateMutationAmbiguous,
    GateQueryFailure,
    GateOrderSnapshot,
    GatePositionSnapshot,
    GatePriceOrderSnapshot,
    GateTradeFill,
    NormalizedEntry,
    NormalizedProtection,
    PositionSide,
    gate_client_text,
)
from gatescope_executor.testnet_execution import (
    DrillState,
    ProtectedPositionReconciler,
    TestnetExecutionCoordinator,
    TestnetExecutionPlan,
)


@dataclass
class MemoryJournal:
    events: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event_type, payload))


class FakeGate:
    def __init__(self) -> None:
        self.position_size = 0
        self.entry_ambiguous = False
        self.protection_ambiguous_kind: str | None = None
        self.protection_visible_after_ambiguity = True
        self.price_orders: dict[str, GatePriceOrderSnapshot] = {}
        self.mutations: list[str] = []
        self.last_entry: NormalizedEntry | None = None
        self.orders: dict[str, GateOrderSnapshot] = {}
        self.exit_sizes: list[int] = []
        self.deadline: float | None = None

    def set_deadline(self, deadline_monotonic: float | None) -> None:
        self.deadline = deadline_monotonic

    def get_contract(self, contract: str) -> GateContractSpec:
        return GateContractSpec(
            name=contract,
            quanto_multiplier=Decimal("0.0001"),
            order_price_round=Decimal("0.1"),
            order_size_min=1,
            order_size_max=1_000_000,
            order_price_deviate=Decimal("0.5"),
            mark_price=Decimal("100"),
            last_price=Decimal("100"),
            status="trading",
        )

    def get_position(self, contract: str) -> GatePositionSnapshot:
        return GatePositionSnapshot(contract, self.position_size, "single", Decimal("100"), Decimal("100"))

    def submit_entry(self, request: NormalizedEntry) -> GateOrderSnapshot:
        self.mutations.append("ENTRY")
        self.last_entry = request
        self.position_size = request.signed_size
        if self.entry_ambiguous:
            raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS")
        return self._entry_order(request)

    def get_order(self, order_id_or_text: str) -> GateOrderSnapshot | None:
        if order_id_or_text in self.orders:
            return self.orders[order_id_or_text]
        if self.last_entry and order_id_or_text == self.last_entry.client_text:
            return self._entry_order(self.last_entry)
        return None

    def cancel_order(self, order_id: str) -> GateOrderSnapshot:
        self.mutations.append("CANCEL_ENTRY")
        assert self.last_entry is not None
        return self._entry_order(self.last_entry)

    def list_my_trades(self, contract: str, *, limit: int = 100) -> tuple[GateTradeFill, ...]:
        return ()

    def submit_protection(self, request: NormalizedProtection) -> str:
        self.mutations.append(request.kind.value)
        order_id = str(200 + len(self.price_orders))
        snapshot = GatePriceOrderSnapshot(
            order_id,
            request.contract,
            request.signed_size,
            request.trigger_price,
            request.trigger_rule,
            "open",
            None,
            True,
            "plan-close-long-position" if request.position_side is PositionSide.LONG else "plan-close-short-position",
        )
        if self.protection_ambiguous_kind == request.kind.value:
            if self.protection_visible_after_ambiguity:
                self.price_orders[order_id] = snapshot
            raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS")
        self.price_orders[order_id] = snapshot
        return order_id

    def get_price_order(self, order_id: str) -> GatePriceOrderSnapshot | None:
        return self.price_orders.get(order_id)

    def list_open_price_orders(self, contract: str) -> tuple[GatePriceOrderSnapshot, ...]:
        return tuple(item for item in self.price_orders.values() if item.contract == contract)

    def cancel_price_order(self, order_id: str) -> GatePriceOrderSnapshot:
        self.mutations.append("CANCEL_PROTECTION")
        item = self.price_orders.pop(order_id)
        return item

    def submit_reduce_only_exit(
        self,
        *,
        contract: str,
        position_side: PositionSide,
        contracts: int,
        client_text: str,
    ) -> GateOrderSnapshot:
        self.mutations.append("FAIL_SAFE_EXIT")
        self.exit_sizes.append(contracts)
        signed = -contracts if position_side is PositionSide.LONG else contracts
        self.position_size = 0
        order = GateOrderSnapshot("999", contract, signed, 0, "finished", "filled", client_text, True, Decimal("99"))
        self.orders[client_text] = order
        return order

    @staticmethod
    def _entry_order(request: NormalizedEntry) -> GateOrderSnapshot:
        return GateOrderSnapshot(
            "101",
            request.contract,
            request.signed_size,
            0,
            "finished",
            "filled",
            request.client_text,
            False,
            Decimal("100"),
        )


def _plan() -> TestnetExecutionPlan:
    return TestnetExecutionPlan(
        intent_id="10000000-0000-0000-0000-000000000001",
        revision=1,
        contract="BTC_USDT",
        position_side=PositionSide.LONG,
        base_quantity=Decimal("0.001"),
        entry_reference=Decimal("100"),
        stop_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        max_entry_drift_bps=Decimal("50"),
    )


def test_stop_is_durably_prepared_and_verified_before_take_profit() -> None:
    exchange = FakeGate()
    journal = MemoryJournal()
    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    event_types = [item[0] for item in journal.events]

    assert outcome.state is DrillState.PROTECTED
    assert exchange.mutations == ["ENTRY", "STOP_LOSS", "TAKE_PROFIT"]
    assert event_types.index("STOP_LOSS_PREPARED") < event_types.index("STOP_ACTIVE")
    assert event_types.index("STOP_ACTIVE") < event_types.index("TAKE_PROFIT_PREPARED")
    assert event_types[-1] == "POSITION_PROTECTED"
    for prepared in ("ENTRY_PREPARED", "STOP_LOSS_PREPARED", "TAKE_PROFIT_PREPARED"):
        payload = dict(journal.events[event_types.index(prepared)][1])
        assert len(str(payload["request_digest"])) == 64


def test_ambiguous_entry_is_reconciled_by_stable_client_text_without_retry() -> None:
    exchange = FakeGate()
    exchange.entry_ambiguous = True
    journal = MemoryJournal()

    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())

    assert outcome.state is DrillState.PROTECTED
    assert exchange.mutations.count("ENTRY") == 1
    assert "ENTRY_ACK_UNKNOWN" in [item[0] for item in journal.events]


def test_ambiguous_stop_requires_one_exact_match_and_is_not_retried() -> None:
    exchange = FakeGate()
    exchange.protection_ambiguous_kind = "STOP_LOSS"
    journal = MemoryJournal()

    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())

    assert outcome.state is DrillState.PROTECTED
    assert exchange.mutations.count("STOP_LOSS") == 1
    assert "STOP_LOSS_ACK_UNKNOWN" in [item[0] for item in journal.events]


def test_unprovable_stop_causes_immediate_reduce_only_exit_and_freeze() -> None:
    exchange = FakeGate()
    exchange.protection_ambiguous_kind = "STOP_LOSS"
    exchange.protection_visible_after_ambiguity = False
    journal = MemoryJournal()

    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())

    assert outcome.state is DrillState.CLOSED_FAIL_SAFE
    assert outcome.frozen is True
    assert exchange.position_size == 0
    assert exchange.mutations == ["ENTRY", "STOP_LOSS", "FAIL_SAFE_EXIT"]
    assert "TAKE_PROFIT" not in exchange.mutations
    assert [item[0] for item in journal.events][-1] == "FAIL_SAFE_POSITION_CLOSED"


def test_existing_position_rejects_preflight_before_any_mutation() -> None:
    exchange = FakeGate()
    exchange.position_size = 3
    journal = MemoryJournal()

    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())

    assert outcome.state is DrillState.PREFLIGHT_REJECTED
    assert outcome.safe_code == "TESTNET_ACCOUNT_NOT_EXCLUSIVELY_FLAT"
    assert outcome.frozen is True
    assert exchange.mutations == []


def test_protection_monitor_releases_account_only_after_flat_and_sibling_cleanup() -> None:
    exchange = FakeGate()
    journal = MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id and opened.take_profit_order_id
    exchange.position_size = 0

    outcome = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id,
        contract="BTC_USDT",
        position_side=PositionSide.LONG,
        contracts=opened.filled_contracts,
        stop_order_id=opened.stop_order_id,
        take_profit_order_id=opened.take_profit_order_id,
    )

    assert outcome.state is DrillState.CLOSED_PROTECTED
    assert outcome.frozen is False
    assert exchange.price_orders == {}
    assert exchange.mutations[-2:] == ["CANCEL_PROTECTION", "CANCEL_PROTECTION"]


def test_protection_monitor_closes_position_when_a_protective_leg_disappears() -> None:
    exchange = FakeGate()
    journal = MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id and opened.take_profit_order_id
    exchange.price_orders.pop(opened.stop_order_id)

    outcome = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id,
        contract="BTC_USDT",
        position_side=PositionSide.LONG,
        contracts=opened.filled_contracts,
        stop_order_id=opened.stop_order_id,
        take_profit_order_id=opened.take_profit_order_id,
    )

    assert outcome.state is DrillState.CLOSED_FAIL_SAFE
    assert outcome.frozen is True
    assert exchange.position_size == 0
    assert "FAIL_SAFE_EXIT" in exchange.mutations


def _monitor(exchange: FakeGate, journal: MemoryJournal, opened: Any, **kwargs: Any) -> Any:
    return ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=opened.filled_contracts, stop_order_id=opened.stop_order_id,
        take_profit_order_id=opened.take_profit_order_id,
        stop_trigger_price=Decimal("95"), take_profit_trigger_price=Decimal("110"), **kwargs,
    )


def test_one_query_failure_keeps_risk_pending_and_next_read_recovers() -> None:
    class FlakyGate(FakeGate):
        fail_next = False

        def get_position(self, contract: str) -> GatePositionSnapshot:
            if self.fail_next:
                self.fail_next = False
                raise GateQueryFailure("GATE_NETWORK_UNAVAILABLE")
            return super().get_position(contract)

    exchange, journal = FlakyGate(), MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    exchange.fail_next = True
    pending = _monitor(exchange, journal, opened)
    assert pending.state is DrillState.RECONCILIATION_PENDING
    assert pending.frozen and pending.stop_order_id == opened.stop_order_id
    assert _monitor(exchange, journal, opened).state is DrillState.PROTECTED
    assert exchange.mutations == ["ENTRY", "STOP_LOSS", "TAKE_PROFIT"]


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_partial_stop_fill_reduces_only_the_observed_remainder(side: PositionSide) -> None:
    exchange, journal = FakeGate(), MemoryJournal()
    plan = _plan() if side is PositionSide.LONG else replace(
        _plan(), position_side=side, stop_price=Decimal("105"), take_profit_price=Decimal("90"),
    )
    opened = TestnetExecutionCoordinator(exchange, journal).execute(plan)
    exchange.position_size = 4 if side is PositionSide.LONG else -4
    assert opened.stop_order_id
    exchange.price_orders.pop(opened.stop_order_id)
    outcome = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=plan.intent_id, contract=plan.contract, position_side=side,
        contracts=opened.filled_contracts, stop_order_id=opened.stop_order_id,
        take_profit_order_id=opened.take_profit_order_id,
    )
    assert outcome.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.position_size == 0 and exchange.exit_sizes == [4]


@pytest.mark.parametrize("changed_size", [-10, 11])
def test_foreign_or_increased_position_never_triggers_an_automatic_exit(changed_size: int) -> None:
    exchange, journal = FakeGate(), MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    exchange.position_size = changed_size
    outcome = _monitor(exchange, journal, opened)
    assert outcome.state is DrillState.MANUAL_INTERVENTION_REQUIRED
    assert exchange.exit_sizes == []


@pytest.mark.parametrize("changes", [
    {"trigger_price": Decimal("1")}, {"trigger_rule": 1},
    {"price_type": 0}, {"order_type": "wrong"},
])
def test_changed_protection_terms_are_not_reported_as_verified(changes: dict[str, Any]) -> None:
    exchange, journal = FakeGate(), MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id
    exchange.price_orders[opened.stop_order_id] = replace(exchange.price_orders[opened.stop_order_id], **changes)
    outcome = _monitor(exchange, journal, opened)
    assert outcome.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.exit_sizes == [10]


def test_unknown_monitor_exit_is_not_replayed_after_a_process_restart() -> None:
    class UnknownExitGate(FakeGate):
        def submit_reduce_only_exit(self, **kwargs: Any) -> GateOrderSnapshot:
            self.mutations.append("FAIL_SAFE_EXIT")
            raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS")

    exchange, journal = UnknownExitGate(), MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id
    exchange.price_orders.pop(opened.stop_order_id)
    first = _monitor(exchange, journal, opened)
    assert first.state is DrillState.RECONCILIATION_PENDING
    prepared = tuple(str(payload["client_text"]) for event, payload in journal.events if event == "MONITOR_EXIT_PREPARED")
    restarted = ProtectedPositionReconciler(exchange, journal, prepared_exit_texts=prepared)
    second = restarted.reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=10, stop_order_id=opened.stop_order_id, take_profit_order_id=opened.take_profit_order_id,
    )
    assert second.state is DrillState.RECONCILIATION_PENDING
    assert exchange.mutations.count("FAIL_SAFE_EXIT") == 1


def test_a_confirmed_partial_exit_gets_a_new_identity_for_its_remainder() -> None:
    exchange, journal = FakeGate(), MemoryJournal()
    opened = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id
    exchange.price_orders.pop(opened.stop_order_id)
    previous = gate_client_text(_plan().intent_id, 1, "MONITOR_EXIT")
    exchange.orders[previous] = GateOrderSnapshot(
        "998", "BTC_USDT", -10, -4, "finished", "ioc", previous, True, Decimal("99"),
    )
    exchange.position_size = 4
    result = ProtectedPositionReconciler(exchange, journal, prepared_exit_texts=(previous,)).reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=10, stop_order_id=opened.stop_order_id, take_profit_order_id=opened.take_profit_order_id,
    )
    assert result.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.exit_sizes == [4]
    assert gate_client_text(_plan().intent_id, 2, "MONITOR_EXIT") in exchange.orders


def test_open_ioc_ack_is_never_treated_as_safely_unfilled() -> None:
    class OpenAckGate(FakeGate):
        @staticmethod
        def _entry_order(request: NormalizedEntry) -> GateOrderSnapshot:
            return replace(FakeGate._entry_order(request), left=request.signed_size, status="open", finish_as=None)

    exchange, journal = OpenAckGate(), MemoryJournal()
    outcome = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert outcome.state is DrillState.RECONCILIATION_PENDING
    assert outcome.safe_code == "ENTRY_FINALITY_PENDING" and outcome.frozen
    assert exchange.mutations == ["ENTRY"]


def test_delayed_entry_ack_is_recovered_and_reduced_without_reopening() -> None:
    exchange, journal = FakeGate(), MemoryJournal()
    entry = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    exchange.price_orders.clear()
    outcome = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=0, stop_order_id=None, take_profit_order_id=None,
    )
    assert entry.filled_contracts == 10 and outcome.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.mutations.count("ENTRY") == 1 and exchange.exit_sizes == [10]


def test_recovery_cancels_and_proves_open_entry_remainder_before_exiting() -> None:
    class OpenEntryGate(FakeGate):
        canceled = False

        def submit_entry(self, request: NormalizedEntry) -> GateOrderSnapshot:
            return replace(super().submit_entry(request), status="open", left=6)

        def get_order(self, order_id_or_text: str) -> GateOrderSnapshot | None:
            order = super().get_order(order_id_or_text)
            if order and self.last_entry and order_id_or_text == self.last_entry.client_text and not self.canceled:
                return replace(order, status="open", left=6)
            return replace(order, left=6) if order and order.client_text == self.last_entry.client_text else order

        def cancel_order(self, order_id: str) -> GateOrderSnapshot:
            self.canceled = True
            return super().cancel_order(order_id)

    exchange, journal = OpenEntryGate(), MemoryJournal()
    result = TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    assert result.state is DrillState.RECONCILIATION_PENDING
    exchange.position_size = 4
    result = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=4, stop_order_id=None, take_profit_order_id=None,
    )
    assert result.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.mutations == ["ENTRY", "CANCEL_ENTRY", "FAIL_SAFE_EXIT"]
    assert exchange.exit_sizes == [4]


def test_unresolved_entry_cancellation_does_not_submit_a_new_entry_or_exit() -> None:
    class StuckEntryGate(FakeGate):
        @staticmethod
        def _entry_order(request: NormalizedEntry) -> GateOrderSnapshot:
            return replace(FakeGate._entry_order(request), status="open", left=6)

        def cancel_order(self, order_id: str) -> GateOrderSnapshot:
            self.mutations.append("CANCEL_ENTRY")
            raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS")

    exchange, journal = StuckEntryGate(), MemoryJournal()
    TestnetExecutionCoordinator(exchange, journal).execute(_plan())
    result = ProtectedPositionReconciler(exchange, journal).reconcile(
        intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
        contracts=4, stop_order_id=None, take_profit_order_id=None,
    )
    assert result.state is DrillState.RECONCILIATION_PENDING and result.safe_code == "ENTRY_FINALITY_PENDING"
    assert exchange.mutations == ["ENTRY", "CANCEL_ENTRY"]


def test_expired_budget_starts_exit_without_attempting_a_late_stop() -> None:
    clock = [0.0]

    class DelayedGate(FakeGate):
        def get_position(self, contract: str) -> GatePositionSnapshot:
            if self.position_size:
                clock[0] = 3.0
            return super().get_position(contract)

        def submit_reduce_only_exit(self, **kwargs: Any) -> GateOrderSnapshot:
            assert self.deadline is None, "expired stop budget must not suppress the emergency exit"
            return super().submit_reduce_only_exit(**kwargs)

    exchange, journal = DelayedGate(), MemoryJournal()
    result = TestnetExecutionCoordinator(exchange, journal, monotonic=lambda: clock[0]).execute(_plan())
    assert result.state is DrillState.CLOSED_FAIL_SAFE
    assert result.safe_code == "PROTECTION_DEADLINE_EXCEEDED"
    assert exchange.mutations == ["ENTRY", "FAIL_SAFE_EXIT"]
    assert exchange.deadline is None


def test_journal_delay_cannot_dispatch_a_stop_after_the_deadline() -> None:
    clock = [0.0]

    class DelayedJournal(MemoryJournal):
        def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
            super().append(event_type, payload)
            if event_type == "STOP_LOSS_PREPARED":
                clock[0] = 3.0

    exchange = FakeGate()
    result = TestnetExecutionCoordinator(exchange, DelayedJournal(), monotonic=lambda: clock[0]).execute(_plan())
    assert result.state is DrillState.CLOSED_FAIL_SAFE
    assert exchange.mutations == ["ENTRY", "FAIL_SAFE_EXIT"]


@pytest.mark.parametrize("cleanup_query_fails", [False, True])
def test_late_stop_ack_is_retained_and_orphan_cleanup_remains_recoverable(cleanup_query_fails: bool) -> None:
    clock = [0.0]

    class LateStopGate(FakeGate):
        fail_cleanup = cleanup_query_fails

        def submit_protection(self, request: NormalizedProtection) -> str:
            order_id = super().submit_protection(request)
            clock[0] = 3.0
            return order_id

        def get_price_order(self, order_id: str) -> GatePriceOrderSnapshot | None:
            if self.position_size == 0 and self.fail_cleanup:
                self.fail_cleanup = False
                raise GateQueryFailure("GATE_NETWORK_UNAVAILABLE")
            return super().get_price_order(order_id)

    exchange, journal = LateStopGate(), MemoryJournal()
    result = TestnetExecutionCoordinator(exchange, journal, monotonic=lambda: clock[0]).execute(_plan())
    assert result.stop_order_id == "200" and exchange.position_size == 0
    assert "TAKE_PROFIT" not in exchange.mutations
    if cleanup_query_fails:
        assert result.state is DrillState.RECONCILIATION_PENDING
        result = ProtectedPositionReconciler(exchange, journal).reconcile(
            intent_id=_plan().intent_id, contract="BTC_USDT", position_side=PositionSide.LONG,
            contracts=10, stop_order_id="200", take_profit_order_id=None,
        )
    assert result.state is DrillState.CLOSED_FAIL_SAFE and exchange.price_orders == {}
    assert "STOP_CANCEL_PREPARED" in [event for event, _ in journal.events]
