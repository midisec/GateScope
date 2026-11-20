from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_testnet_execution import TestnetProtectionCommand as ProtectionCommand
from gatescope.application.testnet_execution import TestnetExecutionMonitor as ExecutionMonitor
from gatescope.credential_crypto import CredentialEnvelope, CredentialMaterial
from gatescope_executor.gate_testnet import GateMutationAmbiguous, GateOrderSnapshot
from gatescope_executor.testnet_execution import DrillState, TestnetExecutionCoordinator as Coordinator
from tests.unit.test_testnet_execution import FakeGate, MemoryJournal, _plan


def test_worker_reloads_exit_preparation_from_journal_before_any_exchange_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnknownExitGate(FakeGate):
        def submit_reduce_only_exit(self, **kwargs) -> GateOrderSnapshot:
            self.mutations.append("FAIL_SAFE_EXIT")
            raise GateMutationAmbiguous("GATE_NETWORK_AMBIGUOUS")

    exchange, journal = UnknownExitGate(), MemoryJournal()
    opened = Coordinator(exchange, journal).execute(_plan())
    assert opened.stop_order_id and opened.take_profit_order_id
    exchange.price_orders.pop(opened.stop_order_id)
    material = CredentialMaterial("test-key-123456789", "test-secret-123456789")
    repository = SimpleNamespace(protection_journal=lambda *args, **kwargs: journal)
    decryptor = SimpleNamespace(reveal=lambda *args, **kwargs: material)
    monitor = ExecutionMonitor(repository, decryptor, worker_id="test-worker", worker_subject_id=uuid4())
    monkeypatch.setattr("gatescope.application.testnet_execution.GateFuturesTestnetAdapter", lambda _: exchange)

    def command() -> ProtectionCommand:
        from uuid import UUID
        return ProtectionCommand(
            id=UUID(_plan().intent_id), public_drill_id="testnet-test", tenant_id=uuid4(), exchange_account_id=uuid4(),
            envelope=CredentialEnvelope(b"test", b"nonce", b"wrapped", "test-v1"), encryption_context="test",
            contract="BTC_USDT", side="LONG", filled_contracts=10,
            stop_order_id=opened.stop_order_id, take_profit_order_id=opened.take_profit_order_id,
            execution_events=tuple({"event_type": event, "payload": dict(payload)} for event, payload in journal.events),
        )

    first = monitor._reconcile_protection(command())
    second = monitor._reconcile_protection(command())
    assert first.state is DrillState.RECONCILIATION_PENDING and second.state is DrillState.RECONCILIATION_PENDING
    assert second.safe_code == "DEGRADED_PROTECTION_EXIT_RECONCILIATION_PENDING"
    assert exchange.mutations.count("FAIL_SAFE_EXIT") == 1


def test_worker_rejects_verified_protection_without_durable_trigger_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = FakeGate()
    journal = MemoryJournal()
    opened = Coordinator(exchange, journal).execute(_plan())
    repository = SimpleNamespace(protection_journal=lambda *args, **kwargs: journal)
    decryptor = SimpleNamespace(reveal=lambda *args, **kwargs: CredentialMaterial("test-key", "test-secret"))
    monitor = ExecutionMonitor(repository, decryptor, worker_id="test-worker", worker_subject_id=uuid4())
    monkeypatch.setattr("gatescope.application.testnet_execution.GateFuturesTestnetAdapter", lambda _: exchange)
    command = ProtectionCommand(
        id=uuid4(), public_drill_id="testnet-test", tenant_id=uuid4(), exchange_account_id=uuid4(),
        envelope=CredentialEnvelope(b"test", b"nonce", b"wrapped", "test-v1"), encryption_context="test",
        contract="BTC_USDT", side="LONG", filled_contracts=10,
        stop_order_id=opened.stop_order_id, take_profit_order_id=opened.take_profit_order_id,
    )
    result = monitor._reconcile_protection(command)
    assert result.state is DrillState.RECONCILIATION_PENDING and result.frozen
    assert exchange.position_size == Decimal("10")
    assert exchange.mutations == ["ENTRY", "STOP_LOSS", "TAKE_PROFIT"]
