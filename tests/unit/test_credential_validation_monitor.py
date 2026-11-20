from __future__ import annotations

from uuid import UUID

from gatescope.adapters.gate_credentials import GateProbeResult, GateRateLimited
from gatescope.adapters.postgres_exchange_accounts import CredentialValidationCommand
from gatescope.application.credential_validation import CredentialValidationMonitor
from gatescope.credential_crypto import CredentialEnvelope, CredentialMaterial


TENANT = UUID("10000000-0000-0000-0000-000000000001")
SUBJECT = UUID("10000000-0000-0000-0000-000000000002")
COMMAND = CredentialValidationCommand(
    id=UUID("10000000-0000-0000-0000-000000000003"),
    tenant_id=TENANT,
    exchange_account_id=UUID("10000000-0000-0000-0000-000000000004"),
    credential_id=UUID("10000000-0000-0000-0000-000000000005"),
    environment="TESTNET",
    envelope=CredentialEnvelope(b"cipher", b"0" * 12, b"wrapped", "v1"),
    encryption_context="tenant:context:bounded",
    attempt_count=1,
)


class FakeStore:
    def __init__(self) -> None:
        self.remaining = [COMMAND]
        self.successes = 0
        self.failures: list[tuple[str, int | None]] = []

    def due_tenants(self, *, limit: int = 20) -> list[UUID]:
        return [TENANT]

    def claim_next(self, tenant_id: UUID, worker_subject_id: UUID, **kwargs: object) -> CredentialValidationCommand | None:
        return self.remaining.pop(0) if self.remaining else None

    def complete_success(self, command: CredentialValidationCommand, worker_subject_id: UUID, **kwargs: object) -> None:
        self.successes += 1

    def complete_failure(
        self,
        command: CredentialValidationCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        safe_error_code: str,
        retry_after_seconds: int | None = None,
    ) -> bool:
        self.failures.append((safe_error_code, retry_after_seconds))
        return safe_error_code == "GATE_RATE_LIMITED"


class FakeDecryptor:
    def reveal(self, envelope: CredentialEnvelope, *, context: bytes) -> CredentialMaterial:
        return CredentialMaterial("key-value-12345678", "secret-value-12345678")


class SuccessProbe:
    def probe(self, environment: str, material: CredentialMaterial) -> GateProbeResult:
        return GateProbeResult({"trading_ready": False}, ())


class LimitedProbe:
    def probe(self, environment: str, material: CredentialMaterial) -> GateProbeResult:
        raise GateRateLimited(17)


def test_monitor_completes_read_probe_without_creating_trade_action() -> None:
    store = FakeStore()
    cycle = CredentialValidationMonitor(
        store,  # type: ignore[arg-type]
        FakeDecryptor(),  # type: ignore[arg-type]
        SuccessProbe(),  # type: ignore[arg-type]
        worker_id="worker-1",
        worker_subject_id=SUBJECT,
    ).run_once()
    assert cycle.succeeded == 1
    assert cycle.failed == 0
    assert store.successes == 1


def test_monitor_propagates_retry_after_to_bounded_store_retry() -> None:
    store = FakeStore()
    cycle = CredentialValidationMonitor(
        store,  # type: ignore[arg-type]
        FakeDecryptor(),  # type: ignore[arg-type]
        LimitedProbe(),  # type: ignore[arg-type]
        worker_id="worker-1",
        worker_subject_id=SUBJECT,
    ).run_once()
    assert cycle.retried == 1
    assert cycle.failed == 0
    assert store.failures == [("GATE_RATE_LIMITED", 17)]
