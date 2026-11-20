from __future__ import annotations

import os
import socket
import threading
from uuid import UUID

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_telegram_config import (
    PostgresTelegramConfigRepository,
    TelegramConfigNotFound,
)
from gatescope.application.notifications import NotificationDispatcher, TelegramSender
from gatescope.credential_crypto import RsaOaepCredentialDecryptor
from gatescope.observability import event
from gatescope.worker_runtime import install_stop_handlers


def main() -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if database_url.startswith("postgresql+psycopg://"):
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("notification-worker requires GATESCOPE_DATABASE_URL")
    pool = PostgresPool(database_url, min_size=1, max_size=4)
    worker_id = os.environ.get("GATESCOPE_WORKER_ID", f"notification-{socket.gethostname()}")[:80]
    fallback_token = os.environ.get("GATESCOPE_TELEGRAM_BOT_TOKEN", "").strip()
    fallback_sender = TelegramSender(fallback_token) if fallback_token else None
    private_key = os.environ.get("GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY", "").strip()
    key_version = os.environ.get("GATESCOPE_TELEGRAM_KEY_VERSION", "").strip()
    decryptor = (
        RsaOaepCredentialDecryptor.from_base64_der(private_key, key_version)
        if private_key and key_version
        else None
    )
    if decryptor is None and fallback_sender is None:
        raise RuntimeError("notification-worker requires a Web configuration key or legacy Bot token")
    telegram_configs = PostgresTelegramConfigRepository(pool)

    def sender_for_tenant(tenant_id: UUID) -> TelegramSender:
        if decryptor is not None:
            try:
                token = telegram_configs.delivery_token(
                    tenant_id, dispatcher.subject_id, decryptor,
                )
                return TelegramSender(token)
            except TelegramConfigNotFound:
                pass
        if fallback_sender is not None:
            return fallback_sender
        raise RuntimeError("TG_NOT_CONFIGURED")

    dispatcher = NotificationDispatcher(pool, sender_for_tenant, worker_id=worker_id)
    from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
    from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
    market_queries=TelegramMarketRuntime(TelegramMarketRepository(pool),sender_for_tenant,worker_id,dispatcher.subject_id)
    stopped = threading.Event()
    install_stop_handlers(stopped.set)
    pool.open()
    event("notification.worker.started", worker_id=worker_id)
    try:
        while not stopped.is_set():
            try:
                processed = dispatcher.run_once()
                processed += market_queries.run_once()
            except Exception as exc:
                # Keep diagnostics useful without serialising exception text, which can
                # contain SQL values or upstream request details.
                event(
                    "notification.worker.cycle_failed",
                    level=40,
                    worker_id=worker_id,
                    error_type=type(exc).__name__,
                    sqlstate=getattr(exc, "sqlstate", None),
                )
                stopped.wait(5)
                continue
            if os.environ.get("GATESCOPE_WORKER_ONCE") == "1":
                break
            if processed == 0:
                stopped.wait(2)
    finally:
        pool.close()
        event("notification.worker.stopped", worker_id=worker_id)


if __name__ == "__main__":
    main()
