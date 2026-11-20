from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from gatescope.adapters.postgres_paper import OpenPaperTrade, PaperTrade
from gatescope.application.paper_monitor import PaperTradeMonitor


TENANT = UUID("10000000-0000-0000-0000-000000000001")
OWNER = UUID("10000000-0000-0000-0000-000000000002")
WORKER = UUID("10000000-0000-0000-0000-000000000003")


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def due_tenants(self, *, limit: int = 20) -> list[UUID]:
        assert limit == 20
        return [TENANT]

    def open_trade_targets(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        limit: int = 100,
        after_public_trade_id: str | None = None,
    ) -> list[OpenPaperTrade]:
        assert tenant_id == TENANT and worker_subject_id == WORKER and limit == 100
        assert after_public_trade_id is None
        return [
            OpenPaperTrade(TENANT, OWNER, "paper_00000000000000000000000000"),
            OpenPaperTrade(TENANT, OWNER, "paper_00000000000000000000000001"),
        ]

    def reconcile_trade(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        public_trade_id: str,
        *,
        now: datetime | None = None,
    ) -> PaperTrade:
        self.calls.append(public_trade_id)
        if public_trade_id.endswith("1"):
            raise RuntimeError("isolated fixture failure")
        return cast(PaperTrade, SimpleNamespace(state="CLOSED"))


def test_monitor_advances_bounded_trades_and_isolates_one_failure() -> None:
    store = FakeStore()
    monitor = PaperTradeMonitor(store, worker_subject_id=WORKER)
    result = monitor.run_once(now=datetime(2026, 9, 7, tzinfo=timezone.utc))
    assert result.tenants == 1
    assert result.attempted == 2
    assert result.closed == 1
    assert result.failed == 1
    assert len(store.calls) == 2
