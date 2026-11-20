from __future__ import annotations

import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database import Database
from gate_client import GateClient
from scanner import BackgroundScanner

from gatescope.migrations import upgrade_database
from gatescope.worker_runtime import database_path, install_stop_handlers


def main() -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL") or os.environ.get("GATESCOPE_MARKET_DATABASE_URL")
    if database_url:
        _formal_main(database_url)
        return
    root = Path(__file__).resolve().parents[2]
    database = Database(database_path(root))
    upgrade_database(root, database.path)
    scanner = BackgroundScanner(
        database,
        GateClient(os.environ.get("GATESCOPE_PUBLIC_MARKET_PROXY", "")),
        update_seconds=int(os.environ.get("GATESCOPE_MARKET_UPDATE_SECONDS", "15")),
        catalog_refresh_seconds=int(os.environ.get("GATESCOPE_CATALOG_REFRESH_SECONDS", "1800")),
    )
    install_stop_handlers(scanner.stop)
    if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
        scanner.refresh_catalog()
        return
    scanner.run_forever()


def _run_coinglass_loop(charts, tests, stopped, *, once=False, on_status=lambda status: None):
    """Independent provider scheduling, never driven by a Gate polling cycle.

    At most one configuration test and one chart job per turn keeps both queues
    moving. Actual HTTP requests still share the credential's request pacer.
    """
    status = {"provider_tests_processed": 0, "coinglass_chart_jobs_processed": 0, "coinglass_loop_cycles": 0}
    while not stopped.is_set():
        worked = failed = False
        for runtime, counter in (
            (tests, "provider_tests_processed"),
            (charts, "coinglass_chart_jobs_processed"),
        ):
            if runtime is None or stopped.is_set():
                continue
            try:
                processed = bool(runtime.run_one())
                worked |= processed
                status[counter] += int(processed)
            except Exception:
                failed = True
                status["coinglass_loop_error"] = "COINGLASS_BACKGROUND_FAILED"
        status["coinglass_loop_cycles"] += 1
        status["coinglass_loop_at"] = int(time.time())
        if not failed:
            status.pop("coinglass_loop_error", None)
        on_status(dict(status))
        if once:
            return
        # No busy loop for empty/error queues; ready jobs resume promptly, with
        # a small cooperative yield even when local-only jobs need no HTTP.
        stopped.wait(2.0 if failed or not worked else 0.05)


def _run_history_maintenance(history_sync, storage_guard, stopped):
    """Capacity gates bulk backfill only, never live polling or catalog refresh."""
    pressure = storage_guard.check()
    status = history_sync.run_cycle(live=False, stopped=stopped.is_set) if pressure.allowed else {"history_pages": 0}
    return {**status, "history_state": pressure.state, "history_storage": pressure.heartbeat()}


def _catalog_provider_ready(repository):
    # History may be disabled, so its result cannot be the sole source of a
    # provider-wide 429 cooldown. Live polling and catalog share this backoff.
    retry_at = repository.provider_retry_at()
    return retry_at is None or retry_at <= datetime.now(timezone.utc)


def _formal_main(database_url: str) -> None:
    from gatescope.adapters.postgres import PostgresPool
    from gatescope.adapters.postgres_coinglass import PostgresCoinglassRepository
    from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository
    from gatescope.application.coinglass_runtime import CoinglassTestRuntime
    from gatescope.application.coinglass_chart_runtime import CoinglassChartRuntime
    from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
    from gatescope.application.market_sync import MarketSyncRuntime
    from gatescope.application.storage_pressure import StoragePressureGuard
    from gatescope.adapters.gate_history import GateHistoryArchive
    from gatescope.adapters.postgres_chart_cache import PostgresChartCache
    from gatescope.credential_crypto import RsaOaepCredentialDecryptor

    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("formal market-worker requires a PostgreSQL URL")
    pool = PostgresPool(database_url, min_size=1, max_size=4)
    repository = PostgresMarketIngestRepository(pool)
    coinglass_repository = PostgresCoinglassRepository(pool)
    client = GateClient(os.environ.get("GATESCOPE_PUBLIC_MARKET_PROXY", ""), timeout=(3, 7), retries=0)
    archive = GateHistoryArchive(
        os.environ.get(
            "GATESCOPE_HISTORY_ARCHIVE_DIR",
            str(Path(__file__).resolve().parents[2] / "data" / "market-archives"),
        )
    )
    warmer = PostgresChartCache(pool)
    sync = MarketSyncRuntime(repository, client, warmer=warmer)
    maintenance_client = GateClient(
        os.environ.get("GATESCOPE_PUBLIC_MARKET_PROXY", ""), timeout=(3, 7), retries=0
    )
    history_sync = MarketSyncRuntime(repository, maintenance_client, archive=archive, warmer=warmer)
    history_guard = StoragePressureGuard(Path(__file__).resolve().parents[2], database_url, pool=pool)
    worker_id = os.environ.get("GATESCOPE_WORKER_ID", f"market-{socket.gethostname()}")[:80]
    coinglass_private_key = os.environ.get("GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY", "").strip()
    coinglass_key_version = os.environ.get("GATESCOPE_COINGLASS_KEY_VERSION", "").strip()
    coinglass_runtime = None
    coinglass_charts = None
    if coinglass_private_key or coinglass_key_version:
        if not coinglass_private_key or not coinglass_key_version:
            raise RuntimeError("Coinglass wrapping private key configuration is incomplete")
        coinglass_runtime = CoinglassTestRuntime(
            coinglass_repository,
            RsaOaepCredentialDecryptor.from_base64_der(
                coinglass_private_key,
                coinglass_key_version,
            ),
            worker_id,
        )
        coinglass_charts = CoinglassChartRuntime(
            PostgresCoinglassContextRepository(pool),
            RsaOaepCredentialDecryptor.from_base64_der(coinglass_private_key, coinglass_key_version),
        )
    cycle_seconds = max(1, int(os.environ.get("GATESCOPE_MARKET_UPDATE_SECONDS", "5")))
    catalog_seconds = max(300, int(os.environ.get("GATESCOPE_CATALOG_REFRESH_SECONDS", "1800")))
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    pool.open()
    catalog_count = 0
    last_catalog = 0.0
    next_catalog_attempt = 0.0
    history_status = {}

    def maintenance():
        nonlocal catalog_count, last_catalog, next_catalog_attempt
        status = _run_history_maintenance(history_sync, history_guard, stopped)
        started = time.monotonic()
        if stopped.is_set():
            return status
        if (
            (not last_catalog or started - last_catalog >= catalog_seconds)
            and started >= next_catalog_attempt
            and not status.get("rate_limited")
            and _catalog_provider_ready(repository)
        ):
            try:
                catalog = maintenance_client.fetch_catalog(include_btc_futures=True)
                repository.refresh_catalog(catalog)
                catalog_count = len(catalog)
                last_catalog = time.monotonic()
            except Exception as exc:
                from gate_client import GateRateLimited
                delay = exc.retry_after if isinstance(exc, GateRateLimited) else 60
                if isinstance(exc, GateRateLimited):
                    repository.pause_provider(datetime.now(timezone.utc), delay)
                next_catalog_attempt = time.monotonic() + delay
                status["catalog_error"] = True
        return status

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-maintenance")
    provider_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coinglass-provider")
    provider_status = {}
    provider_status_lock = threading.Lock()

    def provider_progress(current):
        with provider_status_lock:
            provider_status.clear()
            provider_status.update(current)

    provider_future = (
        provider_executor.submit(
            _run_coinglass_loop,
            coinglass_charts,
            coinglass_runtime,
            stopped,
            once=os.environ.get("GATESCOPE_WORKER_ONCE") == "1",
            on_status=provider_progress,
        )
        if coinglass_charts is not None or coinglass_runtime is not None
        else None
    )
    future = None
    try:
        while not stopped.is_set():
            started = time.monotonic()
            status: dict[str, Any] = {
                "catalog_count": catalog_count,
                "subscription_count": 0,
                "updated_subscriptions": 0,
                "inserted_bars": 0,
                "provider_tests_processed": 0,
                "degraded": False,
                "safe_error_code": None,
                **history_status,
            }
            try:
                # Existing subscriptions keep updating when catalog refresh is unavailable.
                repository.heartbeat(worker_id, running=True, status=status)
                status.update(
                    sync.run_cycle(
                        progress=lambda delta, status=status: repository.heartbeat(
                            worker_id, running=True, status={**status, **delta}
                        ),
                        stopped=stopped.is_set,
                        history=False,
                    )
                )
                if future is not None and future.done():
                    try:
                        maintenance_status = future.result()
                        for key in ("history_pages", "history_state", "history_storage"):
                            if key in maintenance_status:
                                history_status[key] = maintenance_status[key]
                        status.update(history_status)
                    except Exception:
                        status["maintenance_error"] = "MARKET_MAINTENANCE_FAILED"
                    future = None
                if future is None and not stopped.is_set():
                    future = executor.submit(maintenance)
                status["maintenance_running"] = future is not None
                with provider_status_lock:
                    status.update(provider_status)
                status["coinglass_loop_running"] = provider_future is not None and not provider_future.done()
                if provider_future is not None and provider_future.done():
                    try:
                        provider_future.result()
                    except Exception:
                        status["coinglass_loop_error"] = "COINGLASS_BACKGROUND_FAILED"
                status["catalog_count"] = catalog_count
                repository.heartbeat(worker_id, running=True, status=status)
            except Exception:
                status["degraded"] = True
                status["safe_error_code"] = "MARKET_WORKER_CYCLE_FAILED"
                repository.heartbeat(worker_id, running=True, status=status)
            if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
                break
            stopped.wait(max(1.0, cycle_seconds - (time.monotonic() - started)))
    finally:
        stopped.set()
        executor.shutdown(wait=True, cancel_futures=True)
        provider_executor.shutdown(wait=True, cancel_futures=True)
        if coinglass_runtime is not None:
            coinglass_runtime.close()
        if coinglass_charts is not None:
            coinglass_charts.close()
        try:
            repository.heartbeat(worker_id, running=False, status={})
        finally:
            pool.close()


if __name__ == "__main__":
    main()
