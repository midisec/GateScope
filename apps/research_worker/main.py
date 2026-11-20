"""Historical work runs outside the real-time strategy/notification process."""
from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.adapters.postgres_structure_research import StructureResearchRepository
from gatescope.adapters.postgres_portfolio_research import PortfolioResearchRepository
from gatescope.observability import event
from gatescope.application.storage_pressure import PAUSED_WAIT_SECONDS, StoragePressureGuard, safe_storage_status
from gatescope.worker_runtime import install_stop_handlers


def heartbeat(pool: PostgresPool, worker_id: str, *, running: bool, status: dict[str,Any]) -> None:
    safe = {"mode":str(status.get("mode","IDLE"))[:32],
        "processed_bars":max(0,int(status.get("processed_bars",0))),
        "total_bars":max(0,int(status.get("total_bars",0))),
        "legacy_processed":bool(status.get("legacy_processed",False)),
        "structure_slice":bool(status.get("structure_slice",False)),
        "degraded":bool(status.get("failed",False)),
        "safe_error_code":"RESEARCH_CYCLE_FAILED" if status.get("failed") else None}
    storage = safe_storage_status(status.get("storage_pressure"))
    if storage:
        safe.update(history_state=storage["state"], storage_pressure=storage)
    with pool.public_transaction() as conn:
        conn.execute("""INSERT INTO worker_heartbeats(workload,worker_id,running,heartbeat_at,status_json)
            VALUES('research-worker',%s,%s,now(),%s)
            ON CONFLICT(workload) DO UPDATE SET worker_id=EXCLUDED.worker_id,running=EXCLUDED.running,
              heartbeat_at=EXCLUDED.heartbeat_at,status_json=EXCLUDED.status_json""", (worker_id[:80],running,Jsonb(safe)))


def main() -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL","").replace("postgresql+psycopg://","postgresql://",1)
    if not database_url.startswith(("postgresql://","postgres://")):
        raise RuntimeError("research-worker requires a PostgreSQL database configuration")
    # formal-strategy.env is deliberately reused for its least-privilege DB
    # role; never reuse that service's worker ID or its heartbeat workload.
    worker_id = os.environ.get("GATESCOPE_RESEARCH_WORKER_ID","formal-research-primary")[:80] or "formal-research-primary"
    pool = PostgresPool(database_url,min_size=1,max_size=2)
    legacy,structures,portfolios = BacktestRepository(pool),StructureResearchRepository(pool),PortfolioResearchRepository(pool)
    storage = StoragePressureGuard(Path(__file__).resolve().parents[2],database_url,pool=pool)
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    once = os.environ.get("GATESCOPE_WORKER_ONCE")=="1"
    pool.open()
    event("research.worker.started",worker_id=worker_id)
    try:
        previous_history_state = None
        while not stopped.is_set():
            processed = False
            pressure = storage.check()
            try:
                if pressure.state != previous_history_state:
                    event("research.worker.history_state",worker_id=worker_id,**pressure.heartbeat())
                    previous_history_state = pressure.state
                heartbeat(pool,worker_id,running=True,status={
                    "mode":"POLLING" if pressure.allowed else pressure.state,
                    "storage_pressure":pressure.heartbeat()})
                old = chunk = portfolio = False
                if pressure.allowed and not stopped.is_set():
                    old = legacy.run_one(progress=lambda done,total:heartbeat(pool,worker_id,running=True,
                        status={"mode":"INDICATOR","processed_bars":done,"total_bars":total,
                            "storage_pressure":storage.check().heartbeat()}))
                # A long admitted job may consume its budget; check again before
                # each next queue. Never turn a paused claim into FAILED/SUCCEEDED.
                pressure = storage.check()
                if pressure.allowed and not stopped.is_set():
                    chunk = structures.run_slice(max_bars=32,budget_seconds=1.5)
                pressure = storage.check()
                if pressure.allowed and not stopped.is_set():
                    portfolio = portfolios.run_one(progress=lambda done,total:heartbeat(pool,worker_id,running=True,
                        status={"mode":"PORTFOLIO","processed_bars":done,"total_bars":total,
                            "storage_pressure":storage.check().heartbeat()}))
                pressure = storage.check()
                processed = old or chunk or portfolio
                heartbeat(pool,worker_id,running=True,status={"mode":pressure.state if not pressure.allowed else
                    "PORTFOLIO" if portfolio else "STRUCTURE" if chunk else "IDLE",
                    "legacy_processed":old,"structure_slice":chunk,"storage_pressure":pressure.heartbeat()})
            except Exception as exc:
                event("research.worker.cycle_failed",level=40,worker_id=worker_id,error_type=type(exc).__name__)
                heartbeat(pool,worker_id,running=True,status={"mode":"RETRY","failed":True,
                    "storage_pressure":pressure.heartbeat()})
            if once:
                break
            stopped.wait(PAUSED_WAIT_SECONDS if not pressure.allowed else .25 if processed else 5)
    finally:
        try:
            heartbeat(pool,worker_id,running=False,status={"mode":"STOPPED"})
        finally:
            pool.close()
        event("research.worker.stopped",worker_id=worker_id)


if __name__=="__main__":
    main()
