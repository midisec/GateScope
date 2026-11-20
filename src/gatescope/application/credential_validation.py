"""Bounded orchestration for isolated, non-mutating credential validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from gatescope.adapters.gate_credentials import (
    GateCredentialProbe,
    GateRateLimited,
    SafeGateProbeError,
)
from gatescope.adapters.postgres_exchange_accounts import CredentialValidationCommand
from gatescope.credential_crypto import RsaOaepCredentialDecryptor


class CredentialValidationStore(Protocol):
    def due_tenants(self, *, limit: int = 20) -> list[UUID]: ...

    def claim_next(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> CredentialValidationCommand | None: ...

    def complete_success(
        self,
        command: CredentialValidationCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        capabilities: dict[str, object],
        warning_codes: tuple[str, ...],
    ) -> None: ...

    def complete_failure(
        self,
        command: CredentialValidationCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        safe_error_code: str,
        retry_after_seconds: int | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class CredentialValidationCycle:
    tenants: int
    attempted: int
    succeeded: int
    retried: int
    failed: int


class CredentialValidationMonitor:
    def __init__(
        self,
        store: CredentialValidationStore,
        decryptor: RsaOaepCredentialDecryptor,
        probe: GateCredentialProbe,
        *,
        worker_id: str,
        worker_subject_id: UUID,
    ) -> None:
        self.store = store
        self.decryptor = decryptor
        self.probe = probe
        self.worker_id = worker_id
        self.worker_subject_id = worker_subject_id

    def run_once(
        self,
        *,
        tenant_limit: int = 20,
        commands_per_tenant: int = 10,
    ) -> CredentialValidationCycle:
        tenants = self.store.due_tenants(limit=max(1, min(tenant_limit, 100)))
        attempted = succeeded = retried = failed = 0
        for tenant_id in tenants:
            for _ in range(max(1, min(commands_per_tenant, 50))):
                command = self.store.claim_next(
                    tenant_id,
                    self.worker_subject_id,
                    worker_id=self.worker_id,
                    lease_seconds=30,
                )
                if command is None:
                    break
                attempted += 1
                try:
                    material = self.decryptor.reveal(
                        command.envelope,
                        context=command.encryption_context.encode("ascii"),
                    )
                except Exception:
                    self.store.complete_failure(
                        command,
                        self.worker_subject_id,
                        worker_id=self.worker_id,
                        safe_error_code="CREDENTIAL_DECRYPT_FAILED",
                    )
                    failed += 1
                    continue
                try:
                    result = self.probe.probe(command.environment, material)
                except GateRateLimited as exc:
                    was_retried = self.store.complete_failure(
                        command,
                        self.worker_subject_id,
                        worker_id=self.worker_id,
                        safe_error_code=exc.safe_code,
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                    retried += int(was_retried)
                    failed += int(not was_retried)
                except SafeGateProbeError as exc:
                    was_retried = self.store.complete_failure(
                        command,
                        self.worker_subject_id,
                        worker_id=self.worker_id,
                        safe_error_code=exc.safe_code,
                    )
                    retried += int(was_retried)
                    failed += int(not was_retried)
                except Exception:
                    self.store.complete_failure(
                        command,
                        self.worker_subject_id,
                        worker_id=self.worker_id,
                        safe_error_code="GATE_PROBE_INTERNAL_ERROR",
                    )
                    failed += 1
                else:
                    self.store.complete_success(
                        command,
                        self.worker_subject_id,
                        worker_id=self.worker_id,
                        capabilities=result.capabilities,
                        warning_codes=result.warning_codes,
                    )
                    succeeded += 1
                finally:
                    del material
        return CredentialValidationCycle(len(tenants), attempted, succeeded, retried, failed)
