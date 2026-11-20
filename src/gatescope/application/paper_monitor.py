"""Bounded orchestration loop for deterministic PAPER trade advancement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from gatescope.adapters.postgres_paper import OpenPaperTrade, PaperTrade


class PaperTradeStore(Protocol):
    def due_tenants(self, *, limit: int = 20) -> list[UUID]: ...

    def open_trade_targets(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        limit: int = 100,
        after_public_trade_id: str | None = None,
    ) -> list[OpenPaperTrade]: ...

    def reconcile_trade(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        public_trade_id: str,
        *,
        now: datetime | None = None,
    ) -> PaperTrade: ...


@dataclass(frozen=True)
class PaperMonitorResult:
    tenants: int
    attempted: int
    closed: int
    failed: int


class PaperTradeMonitor:
    def __init__(self, store: PaperTradeStore, *, worker_subject_id: UUID) -> None:
        self.store = store
        self.worker_subject_id = worker_subject_id
        self._tenant_cursors: dict[UUID, str] = {}

    def run_once(
        self,
        *,
        tenant_limit: int = 20,
        trades_per_tenant: int = 100,
        now: datetime | None = None,
    ) -> PaperMonitorResult:
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("paper monitor time must include a timezone")
        tenants = self.store.due_tenants(limit=max(1, min(tenant_limit, 100)))
        attempted = closed = failed = 0
        for tenant_id in tenants:
            cursor = self._tenant_cursors.get(tenant_id)
            targets = self.store.open_trade_targets(
                tenant_id,
                self.worker_subject_id,
                limit=max(1, min(trades_per_tenant, 500)),
                after_public_trade_id=cursor,
            )
            if not targets and cursor is not None:
                targets = self.store.open_trade_targets(
                    tenant_id,
                    self.worker_subject_id,
                    limit=max(1, min(trades_per_tenant, 500)),
                    after_public_trade_id=None,
                )
            if targets:
                self._tenant_cursors[tenant_id] = targets[-1].public_trade_id
            for target in targets:
                attempted += 1
                try:
                    trade = self.store.reconcile_trade(
                        target.tenant_id,
                        target.owner_user_id,
                        target.public_trade_id,
                        now=observed_at,
                    )
                except Exception:
                    failed += 1
                    continue
                if trade.state == "CLOSED":
                    closed += 1
        return PaperMonitorResult(len(tenants), attempted, closed, failed)
