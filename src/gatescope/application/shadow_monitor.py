"""Bounded, restart-safe shadow observation cycle independent of delivery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID


class ShadowStore(Protocol):
    def due_owners(self, limit: int = 20) -> list[tuple[UUID, UUID]]: ...
    def capture(self, tenant: UUID, owner: UUID, *, now: datetime, limit: int = 20) -> int: ...
    def advance(self, tenant: UUID, owner: UUID, *, now: datetime, limit: int = 50) -> dict[str, int]: ...


@dataclass(frozen=True)
class ShadowMonitorResult:
    owners: int
    captured: int
    attempted: int
    closed: int
    failed: int


class ShadowTradeMonitor:
    def __init__(self, store: ShadowStore) -> None:
        self.store = store

    def run_once(self, *, now: datetime | None = None, owner_limit: int = 20) -> ShadowMonitorResult:
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("shadow monitor time must include a timezone")
        owners = self.store.due_owners(max(1, min(owner_limit, 100)))
        captured = attempted = closed = failed = 0
        for tenant, owner in owners:
            try:
                captured += self.store.capture(tenant, owner, now=observed)
                outcome = self.store.advance(tenant, owner, now=observed)
                attempted += outcome["attempted"]
                closed += outcome["closed"]
            except Exception:
                # One tenant cannot stop the other tenants or the manual PAPER ledger.
                # No raw DB, notification or credential data is sent to logs.
                failed += 1
        return ShadowMonitorResult(len(owners), captured, attempted, closed, failed)
