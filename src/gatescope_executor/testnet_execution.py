"""Fail-closed orchestration for one isolated Gate TESTNET execution drill.

The coordinator accepts frozen, server-generated terms only.  It deliberately
has no retry loop around exchange mutations.  Every mutation is journaled
before dispatch and ambiguous outcomes are reconciled from exchange facts.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from gatescope_executor.gate_testnet import (
    GateContractSpec,
    GateDeadlineExceeded,
    GateMutationAmbiguous,
    GateMutationRejected,
    GateQueryFailure,
    GateOrderSnapshot,
    GatePositionSnapshot,
    GatePriceOrderSnapshot,
    GateTradeFill,
    NormalizedEntry,
    NormalizedProtection,
    PositionSide,
    ProtectionKind,
    gate_client_text,
    normalize_entry,
    normalize_protection,
)


class DrillState(str, Enum):
    ENTRY_UNFILLED = "ENTRY_UNFILLED"
    ENTRY_UNKNOWN = "ENTRY_UNKNOWN"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    PROTECTED = "PROTECTED"
    CLOSED_PROTECTED = "CLOSED_PROTECTED"
    CLOSED_FAIL_SAFE = "CLOSED_FAIL_SAFE"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"
    PREFLIGHT_REJECTED = "PREFLIGHT_REJECTED"


@dataclass(frozen=True)
class TestnetExecutionPlan:
    __test__ = False

    intent_id: str
    revision: int
    contract: str
    position_side: PositionSide
    base_quantity: Decimal
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    max_entry_drift_bps: Decimal

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("execution plan revision must be positive")
        for value in (
            self.base_quantity,
            self.entry_reference,
            self.stop_price,
            self.take_profit_price,
            self.max_entry_drift_bps,
        ):
            if value <= 0 or not value.is_finite():
                raise ValueError("execution terms must be finite and positive")
        if self.position_side is PositionSide.LONG:
            valid_prices = self.stop_price < self.entry_reference < self.take_profit_price
        else:
            valid_prices = self.take_profit_price < self.entry_reference < self.stop_price
        if not valid_prices:
            raise ValueError("execution protection geometry is invalid")


@dataclass(frozen=True)
class TestnetExecutionOutcome:
    __test__ = False

    state: DrillState
    safe_code: str
    entry_order_id: str | None = None
    filled_contracts: int = 0
    stop_order_id: str | None = None
    take_profit_order_id: str | None = None
    fail_safe_order_id: str | None = None
    frozen: bool = False


class GateExecutionPort(Protocol):
    def set_deadline(self, deadline_monotonic: float | None) -> None: ...

    def get_contract(self, contract: str) -> GateContractSpec: ...

    def get_position(self, contract: str) -> GatePositionSnapshot: ...

    def submit_entry(self, request: NormalizedEntry) -> GateOrderSnapshot: ...

    def get_order(self, order_id_or_text: str) -> GateOrderSnapshot | None: ...

    def cancel_order(self, order_id: str) -> GateOrderSnapshot: ...

    def list_my_trades(self, contract: str, *, limit: int = 100) -> tuple[GateTradeFill, ...]: ...

    def submit_protection(self, request: NormalizedProtection) -> str: ...

    def get_price_order(self, order_id: str) -> GatePriceOrderSnapshot | None: ...

    def list_open_price_orders(self, contract: str) -> tuple[GatePriceOrderSnapshot, ...]: ...

    def cancel_price_order(self, order_id: str) -> GatePriceOrderSnapshot: ...

    def submit_reduce_only_exit(
        self,
        *,
        contract: str,
        position_side: PositionSide,
        contracts: int,
        client_text: str,
    ) -> GateOrderSnapshot: ...


class ExecutionJournal(Protocol):
    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Durably commit an event before returning to the caller."""


@dataclass(frozen=True)
class _EntryFact:
    order_id: str | None
    filled_contracts: int
    fill_price: Decimal | None
    terminal: bool = True


class TestnetExecutionCoordinator:
    """Run one TESTNET drill under exclusive-account and no-manual-trade rules."""

    __test__ = False

    def __init__(
        self,
        exchange: GateExecutionPort,
        journal: ExecutionJournal,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        hard_protection_deadline_ms: int = 3_000,
    ) -> None:
        if hard_protection_deadline_ms < 100 or hard_protection_deadline_ms > 5_000:
            raise ValueError("hard protection deadline must be in [100, 5000] ms")
        self.exchange = exchange
        self.journal = journal
        self.monotonic = monotonic
        self.hard_protection_deadline_ms = hard_protection_deadline_ms
        self._deadline_monotonic: float | None = None
        self._protection_order_ids: dict[ProtectionKind, str] = {}

    def execute(self, plan: TestnetExecutionPlan) -> TestnetExecutionOutcome:
        """Execute frozen terms; no mutation in this method is ever auto-retried."""
        self._protection_order_ids.clear()
        try:
            return self._execute(plan)
        finally:
            self.exchange.set_deadline(None)
            self._deadline_monotonic = None

    def _execute(self, plan: TestnetExecutionPlan) -> TestnetExecutionOutcome:

        try:
            spec = self.exchange.get_contract(plan.contract)
            before = self.exchange.get_position(plan.contract)
            self._require_flat_single_position(before)
            entry = normalize_entry(
                spec,
                position_side=plan.position_side,
                base_quantity=plan.base_quantity,
                entry_reference=plan.entry_reference,
                max_drift_bps=plan.max_entry_drift_bps,
                client_text=gate_client_text(plan.intent_id, plan.revision, "ENTRY"),
            )
        except (ValueError, RuntimeError) as exc:
            code = _safe_exception_code(exc, "TESTNET_PREFLIGHT_REJECTED")
            self.journal.append("PREFLIGHT_REJECTED", {"safe_code": code})
            return TestnetExecutionOutcome(DrillState.PREFLIGHT_REJECTED, code, frozen=True)

        self._prepared("ENTRY", _entry_payload(entry))
        fill_started = self.monotonic()
        self._deadline_monotonic = fill_started + self.hard_protection_deadline_ms / 1000
        self.exchange.set_deadline(self._deadline_monotonic)
        try:
            acknowledged = self.exchange.submit_entry(entry)
            fact = _entry_fact_from_order(acknowledged, entry)
        except GateMutationRejected as exc:
            self.journal.append("ENTRY_REJECTED", {"safe_code": exc.safe_code})
            return TestnetExecutionOutcome(DrillState.PREFLIGHT_REJECTED, exc.safe_code, frozen=True)
        except GateMutationAmbiguous as exc:
            self.journal.append("ENTRY_ACK_UNKNOWN", {"safe_code": exc.safe_code})
            try:
                reconciled = self._reconcile_entry(entry)
            except GateQueryFailure:
                reconciled = None
            if reconciled is None:
                self.journal.append("ENTRY_RECONCILIATION_PENDING", {"client_text": entry.client_text})
                return TestnetExecutionOutcome(
                    DrillState.RECONCILIATION_PENDING, "ENTRY_FACT_NOT_YET_PROVABLE", frozen=True,
                )
            fact = reconciled
        except GateQueryFailure:
            return TestnetExecutionOutcome(
                DrillState.RECONCILIATION_PENDING, "ENTRY_FACT_NOT_YET_PROVABLE", frozen=True,
            )

        if not fact.terminal:
            try:
                reconciled = self._reconcile_entry(entry)
            except GateQueryFailure:
                reconciled = None
            if reconciled is None or not reconciled.terminal:
                return TestnetExecutionOutcome(
                    DrillState.RECONCILIATION_PENDING,
                    "ENTRY_FINALITY_PENDING",
                    entry_order_id=fact.order_id,
                    filled_contracts=fact.filled_contracts,
                    frozen=True,
                )
            fact = reconciled

        self.journal.append(
            "ENTRY_RECONCILED",
            {
                "exchange_order_id": fact.order_id,
                "filled_contracts": fact.filled_contracts,
                "fill_price": str(fact.fill_price) if fact.fill_price is not None else None,
            },
        )
        if fact.filled_contracts == 0:
            return TestnetExecutionOutcome(
                DrillState.ENTRY_UNFILLED,
                "ENTRY_IOC_UNFILLED",
                entry_order_id=fact.order_id,
            )

        try:
            self._require_protection_budget()
            current = self.exchange.get_position(plan.contract)
            self._require_protection_budget()
        except GateDeadlineExceeded:
            return self._fail_safe(plan, fact, "PROTECTION_DEADLINE_EXCEEDED")
        except GateQueryFailure:
            return self._fail_safe(plan, fact, "POSITION_QUERY_FAILED_AFTER_FILL")
        expected_signed = (
            fact.filled_contracts
            if plan.position_side is PositionSide.LONG
            else -fact.filled_contracts
        )
        if current.mode != "single" or current.signed_size != expected_signed:
            self.journal.append(
                "POSITION_IDENTITY_MISMATCH",
                {"expected_signed_size": expected_signed, "observed_signed_size": current.signed_size},
            )
            return TestnetExecutionOutcome(
                DrillState.MANUAL_INTERVENTION_REQUIRED,
                "EXCLUSIVE_POSITION_INVARIANT_BROKEN",
                entry_order_id=fact.order_id,
                filled_contracts=fact.filled_contracts,
                frozen=True,
            )

        try:
            stop = normalize_protection(
                spec,
                position_side=plan.position_side,
                kind=ProtectionKind.STOP_LOSS,
                contracts=fact.filled_contracts,
                requested_trigger=plan.stop_price,
                current_reference=_protection_reference(current, spec),
            )
        except (ValueError, RuntimeError):
            return self._fail_safe(plan, fact, "STOP_TRIGGER_ALREADY_INVALID")
        try:
            stop_order_id = self._place_and_confirm_protection(stop)
        except GateDeadlineExceeded:
            return self._fail_safe(plan, fact, "PROTECTION_DEADLINE_EXCEEDED")
        except (GateQueryFailure, RuntimeError):
            stop_order_id = None
        if stop_order_id is None or self._deadline_exceeded(fill_started):
            code = "STOP_ACK_NOT_PROVABLE" if stop_order_id is None else "STOP_DEADLINE_EXCEEDED"
            return self._fail_safe(plan, fact, code, stop_order_id=stop_order_id)

        self.journal.append("STOP_ACTIVE", {"exchange_order_id": stop_order_id})
        # Once stop coverage is proven, TP and cleanup have their own bounded I/O.
        self.exchange.set_deadline(None)
        self._deadline_monotonic = None
        try:
            take_profit = normalize_protection(
                spec,
                position_side=plan.position_side,
                kind=ProtectionKind.TAKE_PROFIT,
                contracts=fact.filled_contracts,
                requested_trigger=plan.take_profit_price,
                current_reference=_protection_reference(current, spec),
            )
        except (ValueError, RuntimeError):
            return self._fail_safe(
                plan, fact, "TAKE_PROFIT_TRIGGER_ALREADY_INVALID", stop_order_id=stop_order_id,
            )
        try:
            take_profit_order_id = self._place_and_confirm_protection(take_profit)
        except (GateQueryFailure, RuntimeError):
            take_profit_order_id = None
        if take_profit_order_id is None:
            return self._fail_safe(
                plan,
                fact,
                "TAKE_PROFIT_ACK_NOT_PROVABLE",
                stop_order_id=stop_order_id,
            )
        self.journal.append("TAKE_PROFIT_ACTIVE", {"exchange_order_id": take_profit_order_id})
        self.journal.append(
            "POSITION_PROTECTED",
            {"stop_order_id": stop_order_id, "take_profit_order_id": take_profit_order_id},
        )
        return TestnetExecutionOutcome(
            DrillState.PROTECTED,
            "PROTECTION_VERIFIED",
            entry_order_id=fact.order_id,
            filled_contracts=fact.filled_contracts,
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
        )

    def _place_and_confirm_protection(self, request: NormalizedProtection) -> str | None:
        self._require_protection_budget()
        self._prepared(request.kind.value, _protection_payload(request))
        self._require_protection_budget()
        try:
            order_id = self.exchange.submit_protection(request)
        except GateMutationRejected as exc:
            self.journal.append(
                f"{request.kind.value}_REJECTED", {"safe_code": exc.safe_code},
            )
            return None
        except GateMutationAmbiguous as exc:
            self.journal.append(
                f"{request.kind.value}_ACK_UNKNOWN", {"safe_code": exc.safe_code},
            )
            matches = tuple(
                item
                for item in self.exchange.list_open_price_orders(request.contract)
                if _protection_matches(item, request)
            )
            if len(matches) != 1:
                self.journal.append(
                    f"{request.kind.value}_RECONCILIATION_UNSAFE",
                    {"exact_match_count": len(matches)},
                )
                return None
            order_id = matches[0].order_id
        self.journal.append(
            f"{request.kind.value}_ACKNOWLEDGED", {"exchange_order_id": order_id},
        )
        self._protection_order_ids[request.kind] = order_id
        self._require_protection_budget()
        observed = self.exchange.get_price_order(order_id)
        self._require_protection_budget()
        if observed is None or not _protection_matches(observed, request) or observed.status != "open":
            self.journal.append(
                f"{request.kind.value}_VERIFICATION_FAILED", {"exchange_order_id": order_id},
            )
            return None
        return order_id

    def _reconcile_entry(self, entry: NormalizedEntry) -> _EntryFact | None:
        self._require_protection_budget()
        observed = self.exchange.get_order(entry.client_text)
        if observed is not None:
            return _entry_fact_from_order(observed, entry)
        matching_fills = tuple(
            item
            for item in self.exchange.list_my_trades(entry.contract)
            if item.client_text == entry.client_text
        )
        if not matching_fills:
            return None
        signed_total = sum(item.signed_size for item in matching_fills)
        expected_sign = 1 if entry.signed_size > 0 else -1
        if signed_total * expected_sign <= 0 or abs(signed_total) > entry.contracts:
            raise RuntimeError("entry trade history violates the order identity")
        weighted = sum(
            (item.price * abs(item.signed_size) for item in matching_fills), Decimal("0"),
        )
        order_ids = {item.order_id for item in matching_fills}
        return _EntryFact(
            order_id=next(iter(order_ids)) if len(order_ids) == 1 else None,
            filled_contracts=abs(signed_total),
            fill_price=weighted / abs(signed_total),
            # Trade history proves fills, but cannot prove the IOC remainder is final.
            terminal=False,
        )

    def _fail_safe(
        self,
        plan: TestnetExecutionPlan,
        fact: _EntryFact,
        safe_code: str,
        *,
        stop_order_id: str | None = None,
    ) -> TestnetExecutionOutcome:
        # The expired protection budget must never block independent risk reduction.
        self.exchange.set_deadline(None)
        self._deadline_monotonic = None
        stop_order_id = stop_order_id or self._protection_order_ids.get(ProtectionKind.STOP_LOSS)
        take_profit_order_id = self._protection_order_ids.get(ProtectionKind.TAKE_PROFIT)
        self.journal.append("FAIL_SAFE_STARTED", {"safe_code": safe_code})
        exit_text = gate_client_text(plan.intent_id, plan.revision, "FAIL_SAFE_EXIT")
        exit_request = {
            "contract": plan.contract,
            "position_side": plan.position_side.value,
            "contracts": fact.filled_contracts,
            "client_text": exit_text,
            "reduce_only": True,
        }
        self._prepared("FAIL_SAFE_EXIT", exit_request)
        exit_order: GateOrderSnapshot | None = None
        try:
            exit_order = self.exchange.submit_reduce_only_exit(
                contract=plan.contract,
                position_side=plan.position_side,
                contracts=fact.filled_contracts,
                client_text=exit_text,
            )
        except GateMutationRejected as exc:
            self.journal.append("FAIL_SAFE_EXIT_REJECTED", {"safe_code": exc.safe_code})
        except GateMutationAmbiguous as exc:
            self.journal.append("FAIL_SAFE_EXIT_ACK_UNKNOWN", {"safe_code": exc.safe_code})
            try:
                exit_order = self.exchange.get_order(exit_text)
            except GateQueryFailure:
                pass

        try:
            final_position = self.exchange.get_position(plan.contract)
        except GateQueryFailure:
            self.journal.append(
                "FAIL_SAFE_POSITION_QUERY_FAILED",
                {"safe_code": "FAIL_SAFE_EXIT_FACT_UNPROVABLE"},
            )
            return TestnetExecutionOutcome(
                DrillState.RECONCILIATION_PENDING,
                "FAIL_SAFE_EXIT_FACT_UNPROVABLE",
                entry_order_id=fact.order_id,
                filled_contracts=fact.filled_contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                fail_safe_order_id=exit_order.order_id if exit_order else None,
                frozen=True,
            )
        closed = final_position.signed_size == 0
        if closed:
            cleanup = ProtectedPositionReconciler(self.exchange, self.journal)
            try:
                clean = cleanup._cancel_if_open(stop_order_id, "STOP")
                clean = cleanup._cancel_if_open(take_profit_order_id, "TAKE_PROFIT") and clean
            except GateQueryFailure:
                clean = False
            if not clean:
                return TestnetExecutionOutcome(
                    DrillState.RECONCILIATION_PENDING,
                    "POSITION_CLOSED_ORPHAN_CANCEL_NOT_PROVABLE",
                    entry_order_id=fact.order_id, filled_contracts=fact.filled_contracts,
                    stop_order_id=stop_order_id, take_profit_order_id=take_profit_order_id,
                    fail_safe_order_id=exit_order.order_id if exit_order else None, frozen=True,
                )
        if closed:
            self.journal.append(
                "FAIL_SAFE_POSITION_CLOSED",
                {"exchange_order_id": exit_order.order_id if exit_order else None},
            )
            return TestnetExecutionOutcome(
                DrillState.CLOSED_FAIL_SAFE,
                safe_code,
                entry_order_id=fact.order_id,
                filled_contracts=fact.filled_contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                fail_safe_order_id=exit_order.order_id if exit_order else None,
                frozen=True,
            )
        self.journal.append(
            "MANUAL_INTERVENTION_REQUIRED",
            {"safe_code": "FAIL_SAFE_POSITION_STILL_OPEN", "signed_size": final_position.signed_size},
        )
        return TestnetExecutionOutcome(
            DrillState.RECONCILIATION_PENDING,
            "FAIL_SAFE_POSITION_STILL_OPEN",
            entry_order_id=fact.order_id,
            filled_contracts=fact.filled_contracts,
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
            fail_safe_order_id=exit_order.order_id if exit_order else None,
            frozen=True,
        )

    def _prepared(self, leg: str, payload: Mapping[str, Any]) -> None:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.journal.append(
            f"{leg}_PREPARED",
            {"request": dict(payload), "request_digest": hashlib.sha256(canonical.encode()).hexdigest()},
        )

    def _deadline_exceeded(self, fill_started: float) -> bool:
        return (self.monotonic() - fill_started) * 1000 >= self.hard_protection_deadline_ms

    def _require_protection_budget(self) -> None:
        if self._deadline_monotonic is not None and self.monotonic() >= self._deadline_monotonic:
            raise GateDeadlineExceeded("PROTECTION_DEADLINE_EXCEEDED")

    @staticmethod
    def _require_flat_single_position(position: GatePositionSnapshot) -> None:
        if position.mode != "single":
            raise RuntimeError("TESTNET_POSITION_MODE_NOT_SINGLE")
        if position.signed_size != 0:
            raise RuntimeError("TESTNET_ACCOUNT_NOT_EXCLUSIVELY_FLAT")


class ProtectedPositionReconciler:
    """Keep managing existing exposure even while new entries are frozen.

    Prepared exit identities come from the durable journal. A missing exchange
    acknowledgement never permits replaying that mutation on the next cycle.
    """

    def __init__(
        self,
        exchange: GateExecutionPort,
        journal: ExecutionJournal,
        *,
        prepared_exit_texts: tuple[str, ...] = (),
    ) -> None:
        self.exchange = exchange
        self.journal = journal
        self.prepared_exit_texts = list(prepared_exit_texts)

    def reconcile(
        self,
        *,
        intent_id: str,
        contract: str,
        position_side: PositionSide,
        contracts: int,
        stop_order_id: str | None,
        take_profit_order_id: str | None,
        stop_trigger_price: Decimal | None = None,
        take_profit_trigger_price: Decimal | None = None,
    ) -> TestnetExecutionOutcome:
        try:
            return self._reconcile(
                intent_id=intent_id,
                contract=contract,
                position_side=position_side,
                contracts=contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                stop_trigger_price=stop_trigger_price,
                take_profit_trigger_price=take_profit_trigger_price,
            )
        except GateQueryFailure:
            return TestnetExecutionOutcome(
                DrillState.RECONCILIATION_PENDING,
                "PROTECTION_QUERY_RETRY_PENDING",
                filled_contracts=contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                frozen=True,
            )

    def _reconcile(
        self,
        *,
        intent_id: str,
        contract: str,
        position_side: PositionSide,
        contracts: int,
        stop_order_id: str | None,
        take_profit_order_id: str | None,
        stop_trigger_price: Decimal | None,
        take_profit_trigger_price: Decimal | None,
    ) -> TestnetExecutionOutcome:
        position = self.exchange.get_position(contract)
        recovering_entry = contracts <= 0 or stop_order_id is None or take_profit_order_id is None
        if recovering_entry:
            entry_text = gate_client_text(intent_id, 1, "ENTRY")
            entry = self.exchange.get_order(entry_text)
            if entry is None:
                return self._pending("ENTRY_FACT_NOT_YET_PROVABLE")
            sign = 1 if position_side is PositionSide.LONG else -1
            if entry.contract != contract or entry.client_text != entry_text or entry.reduce_only or entry.signed_size * sign <= 0:
                return self._manual("ENTRY_RECONCILIATION_IDENTITY_MISMATCH")
            if entry.status != "finished":
                self.journal.append("ENTRY_CANCEL_PREPARED", {"exchange_order_id": entry.order_id})
                try:
                    self.exchange.cancel_order(entry.order_id)
                except (GateMutationAmbiguous, GateMutationRejected):
                    pass
                entry = self.exchange.get_order(entry_text)
                if entry is None or entry.status != "finished":
                    return self._pending("ENTRY_FINALITY_PENDING")
                if entry.contract != contract or entry.client_text != entry_text or entry.reduce_only or entry.signed_size * sign <= 0:
                    return self._manual("ENTRY_RECONCILIATION_IDENTITY_MISMATCH")
            contracts = entry.filled_contracts
            position = self.exchange.get_position(contract)
        expected_signed = contracts if position_side is PositionSide.LONG else -contracts
        if position.mode != "single":
            return self._manual("TESTNET_POSITION_MODE_CHANGED")
        if position.signed_size == 0:
            confirmation = self.exchange.get_position(contract)
            if confirmation.mode != "single" or confirmation.signed_size != 0:
                return self._pending("POSITION_ZERO_READ_NOT_STABLE")
            clean = self._cancel_if_open(stop_order_id, "STOP")
            clean = self._cancel_if_open(take_profit_order_id, "TAKE_PROFIT") and clean
            if not clean:
                return self._pending("ORPHAN_PROTECTION_CANCEL_NOT_PROVABLE")
            after_cleanup = self.exchange.get_position(contract)
            if after_cleanup.mode != "single" or after_cleanup.signed_size != 0:
                return self._pending("POSITION_CHANGED_DURING_PROTECTION_CLEANUP")
            self.journal.append("POSITION_CLOSED_VERIFIED", {"contract": contract})
            return TestnetExecutionOutcome(
                DrillState.CLOSED_FAIL_SAFE if recovering_entry or self.prepared_exit_texts else DrillState.CLOSED_PROTECTED,
                "EXCHANGE_POSITION_CLOSED",
                filled_contracts=contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                frozen=recovering_entry or bool(self.prepared_exit_texts),
            )
        if position.signed_size * expected_signed <= 0 or abs(position.signed_size) > contracts:
            return self._manual("PROTECTED_POSITION_SIZE_MISMATCH")

        remaining_contracts = abs(position.signed_size)
        signed_exit = -position.signed_size
        stop = self.exchange.get_price_order(stop_order_id) if stop_order_id else None
        target = self.exchange.get_price_order(take_profit_order_id) if take_profit_order_id else None
        stop_active = _active_exit_matches(
            stop, contract, signed_exit, ProtectionKind.STOP_LOSS, stop_trigger_price,
        )
        target_active = _active_exit_matches(
            target, contract, signed_exit, ProtectionKind.TAKE_PROFIT, take_profit_trigger_price,
        )
        if stop_active and target_active and not self.prepared_exit_texts:
            return TestnetExecutionOutcome(
                DrillState.PROTECTED,
                "PROTECTION_REVERIFIED",
                filled_contracts=contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
            )

        self.journal.append(
            "PROTECTION_DEGRADED",
            {"stop_active": stop_active, "take_profit_active": target_active,
             "remaining_contracts": remaining_contracts},
        )
        if self.prepared_exit_texts:
            prior = self.exchange.get_order(self.prepared_exit_texts[-1])
            if prior is None or prior.status != "finished":
                return self._pending("DEGRADED_PROTECTION_EXIT_RECONCILIATION_PENDING")
            if not _exit_identity_matches(prior, contract, position_side, self.prepared_exit_texts[-1]):
                return self._manual("MONITOR_EXIT_IDENTITY_MISMATCH")
            if prior.filled_contracts == 0 or len(self.prepared_exit_texts) >= 3:
                return self._pending("REDUCE_ONLY_EXIT_REQUIRES_MANUAL_REVIEW")
            # A new revision is allowed only after proving the previous exit is final.
            # It reduces the independently observed remainder, never the original size.
        revision = len(self.prepared_exit_texts) + 1
        exit_text = gate_client_text(intent_id, revision, "MONITOR_EXIT")
        exit_order = self.exchange.get_order(exit_text)
        if exit_order is not None:
            if not _exit_identity_matches(exit_order, contract, position_side, exit_text):
                return self._manual("MONITOR_EXIT_IDENTITY_MISMATCH")
            return self._pending("DEGRADED_PROTECTION_EXIT_RECONCILIATION_PENDING")
        if exit_order is None:
            self.journal.append(
                "MONITOR_EXIT_PREPARED",
                {
                    "contract": contract,
                    "contracts": remaining_contracts,
                    "client_text": exit_text,
                    "reduce_only": True,
                },
            )
            self.prepared_exit_texts.append(exit_text)
            try:
                exit_order = self.exchange.submit_reduce_only_exit(
                    contract=contract,
                    position_side=position_side,
                    contracts=remaining_contracts,
                    client_text=exit_text,
                )
            except GateMutationRejected as exc:
                self.journal.append("MONITOR_EXIT_REJECTED", {"safe_code": exc.safe_code})
            except GateMutationAmbiguous as exc:
                self.journal.append("MONITOR_EXIT_ACK_UNKNOWN", {"safe_code": exc.safe_code})
                exit_order = self.exchange.get_order(exit_text)
        final_position = self.exchange.get_position(contract)
        if final_position.signed_size != 0:
            return self._pending(
                "DEGRADED_PROTECTION_EXIT_RECONCILIATION_PENDING",
                fail_safe_order_id=exit_order.order_id if exit_order else None,
            )
        clean = self._cancel_if_open(stop_order_id, "STOP")
        clean = self._cancel_if_open(take_profit_order_id, "TAKE_PROFIT") and clean
        code = "DEGRADED_PROTECTION_POSITION_CLOSED"
        if not clean:
            return self._pending("POSITION_CLOSED_ORPHAN_CANCEL_NOT_PROVABLE")
        self.journal.append("DEGRADED_POSITION_CLOSED", {"safe_code": code})
        return TestnetExecutionOutcome(
            DrillState.CLOSED_FAIL_SAFE,
            code,
            filled_contracts=contracts,
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
            fail_safe_order_id=exit_order.order_id if exit_order else None,
            frozen=True,
        )

    def _cancel_if_open(self, order_id: str | None, leg: str) -> bool:
        if order_id is None:
            return True
        observed = self.exchange.get_price_order(order_id)
        if observed is None or observed.status != "open":
            return True
        self.journal.append(f"{leg}_CANCEL_PREPARED", {"exchange_order_id": order_id})
        try:
            canceled = self.exchange.cancel_price_order(order_id)
        except (GateMutationAmbiguous, GateMutationRejected) as exc:
            self.journal.append(f"{leg}_CANCEL_ACK_UNKNOWN", {"safe_code": exc.safe_code})
            after = self.exchange.get_price_order(order_id)
            return after is None or after.status != "open"
        if canceled.status != "open":
            return True
        after = self.exchange.get_price_order(order_id)
        return after is None or after.status != "open"

    @staticmethod
    def _pending(
        safe_code: str,
        *,
        fail_safe_order_id: str | None = None,
    ) -> TestnetExecutionOutcome:
        return TestnetExecutionOutcome(
            DrillState.RECONCILIATION_PENDING, safe_code,
            fail_safe_order_id=fail_safe_order_id, frozen=True,
        )

    @staticmethod
    def _manual(
        safe_code: str,
        *,
        fail_safe_order_id: str | None = None,
    ) -> TestnetExecutionOutcome:
        return TestnetExecutionOutcome(
            DrillState.MANUAL_INTERVENTION_REQUIRED,
            safe_code,
            fail_safe_order_id=fail_safe_order_id,
            frozen=True,
        )


def _entry_fact_from_order(order: GateOrderSnapshot, expected: NormalizedEntry) -> _EntryFact:
    if (
        order.contract != expected.contract
        or order.client_text != expected.client_text
        or order.signed_size != expected.signed_size
        or order.reduce_only
    ):
        raise RuntimeError("entry order fact identity mismatch")
    return _EntryFact(order.order_id, order.filled_contracts, order.fill_price, order.status == "finished")


def _protection_matches(
    observed: GatePriceOrderSnapshot,
    expected: NormalizedProtection,
) -> bool:
    order_type = (
        "plan-close-long-position"
        if expected.position_side is PositionSide.LONG
        else "plan-close-short-position"
    )
    return (
        observed.contract == expected.contract
        and observed.signed_size == expected.signed_size
        and observed.trigger_price == expected.trigger_price
        and observed.trigger_rule == expected.trigger_rule
        and observed.reduce_only
        and observed.order_type == order_type
        and observed.price_type == expected.price_type
    )


def _active_exit_matches(
    observed: GatePriceOrderSnapshot | None,
    contract: str,
    signed_size: int,
    kind: ProtectionKind,
    trigger_price: Decimal | None,
) -> bool:
    is_long = signed_size < 0
    rule = 2 if (is_long == (kind is ProtectionKind.STOP_LOSS)) else 1
    order_type = "plan-close-long-position" if is_long else "plan-close-short-position"
    return (
        observed is not None
        and observed.status == "open"
        and observed.contract == contract
        and observed.signed_size == signed_size
        and observed.reduce_only
        and observed.trigger_rule == rule
        and observed.order_type == order_type
        and observed.price_type == 1
        and (trigger_price is None or observed.trigger_price == trigger_price)
    )


def _exit_identity_matches(
    observed: GateOrderSnapshot,
    contract: str,
    position_side: PositionSide,
    client_text: str,
) -> bool:
    return (
        observed.contract == contract
        and observed.client_text == client_text
        and observed.reduce_only
        and observed.signed_size * (1 if position_side is PositionSide.LONG else -1) < 0
    )


def _entry_payload(request: NormalizedEntry) -> dict[str, Any]:
    return {
        "contract": request.contract,
        "signed_size": request.signed_size,
        "limit_price": str(request.limit_price),
        "tif": request.tif,
        "client_text": request.client_text,
        "reduce_only": False,
    }


def _protection_payload(request: NormalizedProtection) -> dict[str, Any]:
    return {
        "contract": request.contract,
        "kind": request.kind.value,
        "signed_size": request.signed_size,
        "trigger_price": str(request.trigger_price),
        "trigger_rule": request.trigger_rule,
        "price_type": request.price_type,
        "reduce_only": True,
    }


def _protection_reference(position: GatePositionSnapshot, spec: GateContractSpec) -> Decimal:
    reference = position.mark_price or spec.mark_price
    if reference <= 0 or not reference.is_finite():
        raise RuntimeError("TESTNET_MARK_PRICE_INVALID")
    return reference


def _safe_exception_code(exc: Exception, fallback: str) -> str:
    value = str(exc)
    if value.startswith("TESTNET_") and value.replace("_", "").isalnum():
        return value
    return fallback
