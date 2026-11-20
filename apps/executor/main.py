from __future__ import annotations

import os
import socket
import threading
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from gatescope.adapters.gate_credentials import GateCredentialProbe
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_exchange_accounts import PostgresExchangeAccountRepository
from gatescope.adapters.postgres_testnet_execution import PostgresTestnetExecutionRepository
from gatescope.application.credential_validation import CredentialValidationMonitor
from gatescope.application.testnet_execution import TestnetExecutionMonitor
from gatescope.credential_crypto import RsaOaepCredentialDecryptor
from gatescope.observability import event
from gatescope.worker_runtime import install_stop_handlers


def main() -> None:
    if os.environ.get("LIVE_TRADING_ENABLED", "false").strip().lower() not in {"0", "false", "no", "off"}:
        raise RuntimeError("LIVE trading remains hard-disabled")
    workload = os.environ.get("GATESCOPE_EXECUTOR_WORKLOAD", "credential-validation").strip().lower()
    environment = os.environ.get("GATESCOPE_EXECUTION_ENVIRONMENT", "PAPER").strip().upper()
    if workload not in {"credential-validation", "gate-testnet-execution"}:
        raise RuntimeError("executor workload is not allowlisted")
    if workload == "credential-validation" and environment != "PAPER":
        raise RuntimeError("credential validation uses the non-trading PAPER control plane")
    if workload == "gate-testnet-execution" and environment != "TESTNET":
        raise RuntimeError("Gate execution workload requires explicit TESTNET environment")
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("credential validator requires GATESCOPE_DATABASE_URL")
    private_key = os.environ.get("GATESCOPE_CREDENTIAL_WRAP_PRIVATE_KEY", "")
    key_version = os.environ.get("GATESCOPE_CREDENTIAL_KEY_VERSION", "")
    decryptor = RsaOaepCredentialDecryptor.from_base64_der(private_key, key_version)
    worker_id = os.environ.get("GATESCOPE_WORKER_ID", f"executor-{socket.gethostname()}")[:80]
    subject_id = uuid5(NAMESPACE_URL, f"gatescope:{workload}:{worker_id}")
    pool = PostgresPool(database_url, min_size=1, max_size=4)
    if workload == "credential-validation":
        monitor: CredentialValidationMonitor | TestnetExecutionMonitor = CredentialValidationMonitor(
            PostgresExchangeAccountRepository(pool),
            decryptor,
            GateCredentialProbe(),
            worker_id=worker_id,
            worker_subject_id=subject_id,
        )
    else:
        monitor = TestnetExecutionMonitor(
            PostgresTestnetExecutionRepository(pool),
            decryptor,
            worker_id=worker_id,
            worker_subject_id=subject_id,
        )
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    pool.open()
    event("executor.started", worker_id=worker_id, workload=workload, environment=environment)
    try:
        while not stopped.is_set():
            try:
                cycle = monitor.run_once()
                event("executor.cycle", worker_id=worker_id, workload=workload, **asdict(cycle))
            except Exception:
                event("executor.cycle_failed", level=40, worker_id=worker_id, workload=workload)
                stopped.wait(5)
                continue
            if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
                break
            stopped.wait(1 if cycle.attempted else 3)
    finally:
        pool.close()
        event("executor.stopped", worker_id=worker_id, workload=workload)


if __name__ == "__main__":
    main()
