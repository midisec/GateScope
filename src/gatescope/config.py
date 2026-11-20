"""Strict runtime configuration and Phase 0 fail-closed gates."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from gatescope.domain import Environment


class ConfigurationError(RuntimeError):
    pass


def strict_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be an explicit boolean")


@dataclass(frozen=True)
class RuntimeSettings:
    db_path: Path
    public_origin: str | None
    cookie_secure: bool
    session_ttl_seconds: int
    bootstrap_username: str | None
    live_trading_enabled: bool
    execution_environment: Environment
    start_workers: bool

    @classmethod
    def from_env(cls, root: Path) -> "RuntimeSettings":
        public_origin = (os.environ.get("GATESCOPE_PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/") or None
        if public_origin:
            parsed = urlparse(public_origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
                raise ConfigurationError("GATESCOPE_PUBLIC_ORIGIN must be an http(s) origin without a path")

        try:
            environment = Environment(os.environ.get("GATESCOPE_EXECUTION_ENVIRONMENT", "PAPER").strip().upper())
        except ValueError as exc:
            raise ConfigurationError("GATESCOPE_EXECUTION_ENVIRONMENT is invalid") from exc

        live_enabled = strict_bool("LIVE_TRADING_ENABLED", False)
        if live_enabled:
            raise ConfigurationError("LIVE trading is unavailable during Phase 0")
        if environment is not Environment.PAPER:
            raise ConfigurationError("Phase 0 only permits GATESCOPE_EXECUTION_ENVIRONMENT=PAPER")

        cookie_secure = strict_bool("GATESCOPE_COOKIE_SECURE", bool(public_origin and public_origin.startswith("https://")))
        if public_origin and public_origin.startswith("https://") and not cookie_secure:
            raise ConfigurationError("HTTPS deployments require secure cookies")

        raw_path = os.environ.get("GATESCOPE_DB_PATH", "data/gate_patterns.db")
        db_path = Path(raw_path)
        if not db_path.is_absolute():
            db_path = root / db_path
        ttl = int(os.environ.get("GATESCOPE_SESSION_TTL_SECONDS", "43200"))
        if ttl < 900 or ttl > 86400:
            raise ConfigurationError("GATESCOPE_SESSION_TTL_SECONDS must be between 900 and 86400")

        username = (os.environ.get("GATESCOPE_BOOTSTRAP_USERNAME") or "").strip() or None
        password = os.environ.get("GATESCOPE_BOOTSTRAP_PASSWORD") or None
        if (username is None) != (password is None):
            raise ConfigurationError("bootstrap username and password must be provided together")

        start_workers = strict_bool("GATESCOPE_START_WORKERS", False)
        if start_workers:
            raise ConfigurationError("Web API cannot host background workers; use dedicated worker entrypoints")

        return cls(
            db_path=db_path,
            public_origin=public_origin,
            cookie_secure=cookie_secure,
            session_ttl_seconds=ttl,
            bootstrap_username=username,
            live_trading_enabled=live_enabled,
            execution_environment=environment,
            start_workers=False,
        )
