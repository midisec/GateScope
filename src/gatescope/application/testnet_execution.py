"""Bounded worker loop for explicit Gate TESTNET execution drills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

from gatescope.adapters.postgres_testnet_execution import (
    PostgresTestnetExecutionRepository,
    TestnetExecutionCommand,
    TestnetOutcomeRecord,
    TestnetProtectionCommand,
)
from gatescope.credential_crypto import RsaOaepCredentialDecryptor
from gatescope_executor.gate_testnet import GateFuturesTestnetAdapter, PositionSide
from gatescope_executor.testnet_execution import (
    DrillState,
    ProtectedPositionReconciler,
    TestnetExecutionCoordinator,
    TestnetExecutionOutcome,
    TestnetExecutionPlan,
)


@dataclass(frozen=True)
class TestnetExecutionCycle:
    tenants: int
    attempted: int
    protected: int
    fail_safe_closed: int
    frozen: int
    protection_checks: int
    protected_positions_closed: int


class TestnetExecutionMonitor:
    def __init__(
        self,
        repository: PostgresTestnetExecutionRepository,
        decryptor: RsaOaepCredentialDecryptor,
        *,
        worker_id: str,
        worker_subject_id: UUID,
    ) -> None:
        self.repository = repository
        self.decryptor = decryptor
        self.worker_id = worker_id
        self.worker_subject_id = worker_subject_id

    def run_once(self, *, tenant_limit: int = 20) -> TestnetExecutionCycle:
        tenants = self.repository.due_tenants(limit=max(1, min(tenant_limit, 100)))
        attempted = protected = fail_safe_closed = frozen = 0
        protection_checks = protected_positions_closed = 0
        for tenant_id in tenants:
            command = self.repository.claim_next(
                tenant_id,
                self.worker_subject_id,
                worker_id=self.worker_id,
            )
            if command is not None:
                attempted += 1
                outcome = self._execute(command)
                self.repository.complete(
                    command,
                    self.worker_subject_id,
                    worker_id=self.worker_id,
                    outcome=_outcome_record(outcome),
                )
                if outcome.state is DrillState.PROTECTED:
                    protected += 1
                if outcome.state is DrillState.CLOSED_FAIL_SAFE:
                    fail_safe_closed += 1
                if outcome.frozen:
                    frozen += 1
            protection = self.repository.claim_protected(
                tenant_id,
                self.worker_subject_id,
                worker_id=self.worker_id,
            )
            if protection is not None:
                protection_checks += 1
                monitor_outcome = self._reconcile_protection(protection)
                self.repository.complete_protection_check(
                    protection,
                    self.worker_subject_id,
                    worker_id=self.worker_id,
                    outcome=_outcome_record(monitor_outcome),
                )
                if monitor_outcome.state is DrillState.CLOSED_PROTECTED:
                    protected_positions_closed += 1
                if monitor_outcome.state is DrillState.CLOSED_FAIL_SAFE:
                    fail_safe_closed += 1
                if monitor_outcome.frozen:
                    frozen += 1
        return TestnetExecutionCycle(
            len(tenants), attempted, protected, fail_safe_closed, frozen,
            protection_checks, protected_positions_closed,
        )

    def _execute(self, command: TestnetExecutionCommand) -> TestnetExecutionOutcome:
        try:
            material = self.decryptor.reveal(
                command.envelope,
                context=command.encryption_context.encode("ascii"),
            )
            exchange = GateFuturesTestnetAdapter(material)
            return TestnetExecutionCoordinator(
                exchange,
                self.repository.journal(
                    command, self.worker_subject_id, worker_id=self.worker_id,
                ),
            ).execute(_plan(command))
        except Exception:
            # The event journal contains the last durably prepared/observed step.
            # Never replay an unclassified failure as a new exchange mutation.
            return TestnetExecutionOutcome(
                DrillState.RECONCILIATION_PENDING,
                "UNCLASSIFIED_EXECUTION_FAILURE",
                frozen=True,
            )

    def _reconcile_protection(
        self,
        command: TestnetProtectionCommand,
    ) -> TestnetExecutionOutcome:
        try:
            material = self.decryptor.reveal(
                command.envelope,
                context=command.encryption_context.encode("ascii"),
            )
            exchange = GateFuturesTestnetAdapter(material)
            prepared_exit_texts: list[str] = []
            stop_trigger_price = take_profit_trigger_price = None
            for item in command.execution_events:
                payload = item["payload"]
                request = payload.get("request", payload)
                if item["event_type"] in {"MONITOR_EXIT_PREPARED", "FAIL_SAFE_EXIT_PREPARED"}:
                    prepared_exit_texts.append(str(request["client_text"]))
                elif item["event_type"] == "STOP_LOSS_PREPARED":
                    stop_trigger_price = Decimal(str(request["trigger_price"]))
                elif item["event_type"] == "TAKE_PROFIT_PREPARED":
                    take_profit_trigger_price = Decimal(str(request["trigger_price"]))
            if command.stop_order_id and stop_trigger_price is None:
                raise ValueError("durable stop terms are missing")
            if command.take_profit_order_id and take_profit_trigger_price is None:
                raise ValueError("durable take-profit terms are missing")
            return ProtectedPositionReconciler(
                exchange,
                self.repository.protection_journal(
                    command,
                    self.worker_subject_id,
                    worker_id=self.worker_id,
                ),
                prepared_exit_texts=tuple(prepared_exit_texts),
            ).reconcile(
                intent_id=str(command.id),
                contract=command.contract,
                position_side=PositionSide(command.side),
                contracts=command.filled_contracts,
                stop_order_id=command.stop_order_id,
                take_profit_order_id=command.take_profit_order_id,
                stop_trigger_price=stop_trigger_price,
                take_profit_trigger_price=take_profit_trigger_price,
            )
        except Exception:
            return TestnetExecutionOutcome(
                DrillState.RECONCILIATION_PENDING,
                "UNCLASSIFIED_PROTECTION_MONITOR_FAILURE",
                filled_contracts=command.filled_contracts,
                stop_order_id=command.stop_order_id,
                take_profit_order_id=command.take_profit_order_id,
                frozen=True,
            )


def _plan(command: TestnetExecutionCommand) -> TestnetExecutionPlan:
    exact = command.exact_terms
    try:
        quantity = Decimal(str(exact["quantity"]))
        entry = Decimal(str(exact["entry_reference"]))
        stop = Decimal(str(exact["stop_price"]))
        target = Decimal(str(exact["take_profit_price"]))
        drift = Decimal(str(exact["max_entry_drift_bps"]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("frozen TESTNET terms are invalid") from exc
    if exact.get("quantity_unit") != "BASE":
        raise ValueError("only frozen base-quantity terms are supported")
    return TestnetExecutionPlan(
        intent_id=str(command.id),
        revision=1,
        contract=command.contract,
        position_side=PositionSide(command.side),
        base_quantity=quantity,
        entry_reference=entry,
        stop_price=stop,
        take_profit_price=target,
        max_entry_drift_bps=drift,
    )


def _outcome_record(outcome: TestnetExecutionOutcome) -> TestnetOutcomeRecord:
    return TestnetOutcomeRecord(
        state=outcome.state.value,
        safe_code=outcome.safe_code,
        entry_order_id=outcome.entry_order_id,
        filled_contracts=outcome.filled_contracts,
        stop_order_id=outcome.stop_order_id,
        take_profit_order_id=outcome.take_profit_order_id,
        fail_safe_order_id=outcome.fail_safe_order_id,
        frozen=outcome.frozen,
    )
