from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from database import Database
from gate_client import GateClient
from opportunity_scanner import OpportunityScanner

from gatescope.migrations import upgrade_database
from gatescope.observability import event
from gatescope.application.storage_pressure import StoragePressureGuard
from gatescope.worker_runtime import database_path, install_stop_handlers

INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d")


def run_pattern_cycle(pattern_studies, storage):
    """Live observations remain enabled while optional replay is paused."""
    tracking = pattern_studies.track_slice()
    pressure = storage.check()
    studying = False
    if pressure.allowed:
        pattern_studies.schedule()
        studying = pattern_studies.run_slice()
    return tracking, studying, pressure


def main() -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if database_url:
        _formal_main(database_url)
        return
    root = Path(__file__).resolve().parents[2]
    database = Database(database_path(root))
    upgrade_database(root, database.path)
    scanner = OpportunityScanner(database, GateClient(), INTERVALS)
    install_stop_handlers(scanner.stop)
    scanner.run_forever()


def _formal_main(database_url: str) -> None:
    from gatescope.adapters.postgres import PostgresPool
    from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
    from gatescope.adapters.postgres_strategy import PostgresStrategyRepository
    from gatescope.application.alerts import PostgresAlertWriter
    from gatescope.application.strategy_runtime import FormalStrategyRunner

    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("formal strategy-worker requires a PostgreSQL URL")
    worker_id = os.environ.get("GATESCOPE_WORKER_ID", f"strategy-{socket.gethostname()}")[:80]
    subject_id = uuid5(NAMESPACE_URL, f"gatescope:strategy-worker:{worker_id}")
    pool = PostgresPool(database_url, min_size=1, max_size=4)
    repository = PostgresStrategyRepository(pool)
    pattern_studies = PatternStudyRepository(pool)
    storage = StoragePressureGuard(Path(__file__).resolve().parents[2], database_url, pool=pool)
    runner = FormalStrategyRunner(
        repository, PostgresAlertWriter(pool), subject_id=subject_id,
    )
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    pool.open()
    event("strategy.worker.started", worker_id=worker_id)
    try:
        next_rules = 0.0
        previous_history_state = None
        while not stopped.is_set():
            try:
                if time.monotonic() >= next_rules:
                    result = runner.run_once()
                    next_rules = time.monotonic() + 5
                tracking, studying, pressure = run_pattern_cycle(pattern_studies, storage)
                if pressure.state != previous_history_state:
                    event("strategy.worker.history_state", worker_id=worker_id, **pressure.heartbeat())
                    previous_history_state = pressure.state
                repository.heartbeat(worker_id, running=True, status={
                    "tenant_count": result.tenants, "rule_count": result.rules,
                    "scanned_rules": result.scanned, "emitted_alerts": result.emitted,
                    "failed_rules": result.failed,
                    "safe_error_code": "STRATEGY_RULE_SCAN_FAILED" if result.failed else None,
                    "history_state": pressure.state, "storage_pressure": pressure.heartbeat(),
                })
                event(
                    "strategy.worker.cycle", worker_id=worker_id, tenants=result.tenants,
                    rules=result.rules, scanned=result.scanned, emitted=result.emitted,
                    failed=result.failed,
                )
            except Exception as exc:
                event(
                    "strategy.worker.cycle_failed", level=40, worker_id=worker_id,
                    error_type=type(exc).__name__, sqlstate=getattr(exc, "sqlstate", None),
                )
                stopped.wait(5)
                continue
            if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
                break
            stopped.wait(.25 if studying or tracking else 5 if result.rules else 15)
    finally:
        try:
            repository.heartbeat(worker_id, running=False, status={})
        finally:
            pool.close()
        event("strategy.worker.stopped", worker_id=worker_id)


if __name__ == "__main__":
    main()
