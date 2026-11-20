from __future__ import annotations

import os
import socket
import threading
import time
from uuid import NAMESPACE_URL, uuid5

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from gatescope.adapters.postgres_paper_orders import PostgresPaperOrderRepository
from gatescope.adapters.postgres_shadow import ShadowTradeRepository
from gatescope.adapters.postgres_worker_health import PostgresWorkerHealth
from gatescope.adapters.gate_paper_market import GatePaperMarket
from gatescope.application.paper_monitor import PaperTradeMonitor
from gatescope.application.paper_order_monitor import PaperOrderMonitor
from gatescope.application.shadow_monitor import ShadowTradeMonitor
from gatescope.observability import event
from gatescope.worker_runtime import install_stop_handlers


PLANES = {
    "orders": ("paper-order-worker", "paper.orders", "PAPER_ORDER", 1),
    "legacy": ("paper-legacy-worker", "paper.worker", "PAPER_LEGACY", 2),
    "shadow": ("shadow-worker", "shadow.worker", "SHADOW", 5),
}


def run_plane(plane, database_url, worker_id, subject_id, stopped, *, once=False):
    """Independent provider/pool ownership prevents shadow queries delaying exits.

    This is scheduling isolation, not a hard real-time protection guarantee.
    """
    workload, event_prefix, error_prefix, delay = PLANES[plane]
    pool = PostgresPool(database_url, min_size=1, max_size=2)
    market = None
    health = PostgresWorkerHealth(pool)
    try:
        pool.open()
        monitor: PaperOrderMonitor | PaperTradeMonitor | ShadowTradeMonitor
        if plane == "orders":
            market = GatePaperMarket()
            monitor = PaperOrderMonitor(PostgresPaperOrderRepository(pool), market)
        elif plane == "legacy":
            monitor = PaperTradeMonitor(PostgresPaperRepository(pool), worker_subject_id=subject_id)
        else:
            monitor = ShadowTradeMonitor(ShadowTradeRepository(pool))
        while not stopped.is_set():
            started = time.monotonic()
            counts = {}
            safe_error = None
            try:
                result = monitor.run_once()
                counts = {key: value for key, value in vars(result).items() if key != "elapsed_seconds"}
                safe_error = f"{error_prefix}_PARTIAL_FAILURE" if result.failed else None
                if plane == "orders" and not safe_error and counts.get("unavailable"):
                    safe_error = "PAPER_ORDER_MARKET_UNAVAILABLE"
                event(f"{event_prefix}.cycle", worker_id=worker_id,
                      elapsed_seconds=round(time.monotonic()-started, 3), **counts)
            except Exception:
                safe_error = f"{error_prefix}_CYCLE_FAILED"
                event(f"{event_prefix}.cycle_failed", level=40, worker_id=worker_id)
            try:
                health.record(workload, worker_id, running=True, healthy=safe_error is None,
                              safe_error=safe_error, counters=counts, elapsed=time.monotonic()-started)
            except Exception:
                # The monitoring sink must not suppress protection attempts.
                event("paper.business_heartbeat_failed", level=30, workload=workload, worker_id=worker_id)
            if once:
                break
            stopped.wait(delay)
    except Exception:
        # Initialization failures cannot leave an apparently healthy supervisor
        # with one permanently absent business plane.
        event("paper.plane_failed", level=40, workload=workload, worker_id=worker_id)
        stopped.set()
        raise RuntimeError("PAPER_PLANE_STARTUP_FAILED") from None
    finally:
        try:
            health.record(workload, worker_id, running=False)
        except Exception:
            pass
        if market is not None:
            try:
                market.close()
            except Exception:
                event("paper.orders.market_close_failed", level=30, worker_id=worker_id)
        pool.close()


def main() -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("paper-worker requires GATESCOPE_DATABASE_URL")
    worker_id = os.environ.get("GATESCOPE_WORKER_ID", f"paper-{socket.gethostname()}")[:80]
    subject_id = uuid5(NAMESPACE_URL, f"gatescope:paper-worker:{worker_id}")
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    event("paper.worker.started", worker_id=worker_id)
    threads = []
    try:
        if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
            # Deterministic diagnostics, not production scheduling.
            for plane in ("orders", "shadow", "legacy"):
                run_plane(plane, database_url, worker_id, subject_id, stopped, once=True)
        else:
            for plane in ("legacy", "shadow"):
                def background(selected=plane):
                    try:
                        run_plane(selected, database_url, worker_id, subject_id, stopped)
                    except Exception:
                        # Safe code already logged; avoid threading tracebacks
                        # that might expose a malformed resource configuration.
                        pass
                thread = threading.Thread(target=background, name=f"paper-{plane}", daemon=True)
                thread.start()
                threads.append(thread)
            run_plane("orders", database_url, worker_id, subject_id, stopped)
    finally:
        stopped.set()
        # Only each owning thread closes its resources. systemd's existing
        # TimeoutStopSec remains the outer fence for exceptionally slow queries.
        for thread in threads:
            thread.join(timeout=5)
            if thread.is_alive():
                event("paper.plane_shutdown_pending", level=30, workload=thread.name, worker_id=worker_id)
        event("paper.worker.stopped", worker_id=worker_id)


if __name__ == "__main__":
    main()
