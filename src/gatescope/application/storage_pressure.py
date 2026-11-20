"""Fail-closed admission control for optional historical work, not live trading.

GATESCOPE_HISTORY_STORAGE_PATHS is a colon-separated, operator-verified list of
local filesystem paths covering PostgreSQL data, WAL and tablespaces. Mount
roots are valid when the service cannot traverse a private PGDATA directory.
The application filesystem is always checked too. Without this override, a
local database's data_directory and resolved pg_wal are checked automatically.
Remote database capacity cannot be inferred from application-host free space:
GATESCOPE_HISTORY_REMOTE_DB_CAPACITY=EXTERNALLY_MONITORED is an explicit operator
acknowledgement, not a remote disk measurement.

This is admission control between bounded slices/jobs, not a disk reservation
or cancellation of a transaction that has already started. It never changes
job outcomes or pauses the live alert/structure/PAPER workers.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Mapping

from psycopg.conninfo import conninfo_to_dict

GIB = 1024 ** 3
PAUSED_WAIT_SECONDS = 15
_STATES = frozenset({"RUNNING", "PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE"})
_SCOPES = frozenset({"LOCAL_DATABASE_EXPLICIT_PATHS", "LOCAL_DATABASE_AUTO",
    "REMOTE_DATABASE_EXTERNALLY_MONITORED", "UNVERIFIED"})


@dataclass(frozen=True)
class StoragePressure:
    state: str
    min_free_bytes: int | None
    checked_paths: int
    database_scope: str
    pause_below_bytes: int
    resume_above_bytes: int

    @property
    def allowed(self) -> bool:
        return self.state == "RUNNING"

    def heartbeat(self) -> dict[str, Any]:
        return {"state": self.state, "min_free_bytes": self.min_free_bytes,
            "checked_paths": self.checked_paths, "database_scope": self.database_scope,
            "pause_below_bytes": self.pause_below_bytes, "resume_above_bytes": self.resume_above_bytes}


def safe_storage_status(value: Any) -> dict[str, Any]:
    """No paths, DSNs, exception strings or arbitrary status fields in heartbeats."""
    if not isinstance(value, Mapping) or value.get("state") not in _STATES:
        return {}
    result: dict[str, Any] = {"state": value["state"],
        "database_scope": value.get("database_scope") if value.get("database_scope") in _SCOPES else "UNVERIFIED"}
    for key in ("min_free_bytes", "checked_paths", "pause_below_bytes", "resume_above_bytes"):
        item = value.get(key)
        result[key] = item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None
    return result


def _local_database(database_url: str, environ: Mapping[str, str]) -> bool:
    config = conninfo_to_dict(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    # libpq can resolve a host from PGHOST/PGHOSTADDR or an opaque service file.
    # A URL without a host alone is not proof that the database is on this host.
    address = config.get("hostaddr") or environ.get("PGHOSTADDR")
    host = config.get("host") or environ.get("PGHOST")
    if not address and not host and (config.get("service") or environ.get("PGSERVICE")):
        return False
    hosts = str(address or host or "").split(",")
    for host in hosts:
        if not host or host == "localhost" or host.startswith("/"):
            continue
        try:
            if ipaddress.ip_address(host).is_loopback:
                continue
        except ValueError:
            pass
        return False
    return True


def _database_paths(pool: Any) -> tuple[Path, ...]:
    with pool.public_transaction() as conn:
        conn.execute("SET LOCAL statement_timeout='2s'")
        row = conn.execute("SHOW data_directory").fetchone()
    if not row or not isinstance(row[0], str):
        raise ValueError("STORAGE_PATH_UNAVAILABLE")
    data = Path(row[0]).resolve(strict=True)
    # Resolving the link is important: WAL may live on a separate mount.
    return data, (data / "pg_wal").resolve(strict=True)


class StoragePressureGuard:
    def __init__(self, project_root: Path, database_url: str, *, pool: Any = None,
                 environ: Mapping[str, str] | None = None,
                 disk_probe: Callable[[Path], int] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 database_paths: Callable[[], tuple[Path, ...]] | None = None) -> None:
        env = os.environ if environ is None else environ
        self._root = project_root
        self._probe = disk_probe or (lambda path: shutil.disk_usage(path).free)
        self._clock = clock
        self._discover = database_paths or (lambda: _database_paths(pool))
        self._paths: tuple[Path, ...] | None = None
        self._scope = "UNVERIFIED"
        self._paused = False
        self._last_check = float("-inf")
        self._current: StoragePressure | None = None
        self._config_valid = True
        self._pause, self._resume, self._interval = 3 * GIB, 5 * GIB, 15.0
        self._enabled = True
        try:
            enabled = env.get("GATESCOPE_HISTORY_TASKS_ENABLED", "true").strip().lower()
            if enabled not in {"true", "false", "1", "0"}:
                raise ValueError("STORAGE_CONFIG_INVALID")
            self._enabled = enabled in {"true", "1"}
            pause = float(env.get("GATESCOPE_HISTORY_PAUSE_FREE_GIB", "3"))
            resume = float(env.get("GATESCOPE_HISTORY_RESUME_FREE_GIB", "5"))
            interval = float(env.get("GATESCOPE_HISTORY_CHECK_SECONDS", "15"))
            if not all(map(math.isfinite, (pause, resume, interval))) or not 0 < pause < resume or not 1 <= interval <= 60:
                raise ValueError("STORAGE_CONFIG_INVALID")
            self._pause, self._resume, self._interval = int(pause * GIB), int(resume * GIB), interval
            self._local = _local_database(database_url, env)
            configured = env.get("GATESCOPE_HISTORY_STORAGE_PATHS")
            self._configured = tuple(Path(part) for part in configured.split(os.pathsep)) if configured else ()
            if configured is not None and (not configured or not self._configured or
                    any(not path.is_absolute() for path in self._configured)):
                raise ValueError("STORAGE_CONFIG_INVALID")
            self._remote_acknowledged = env.get("GATESCOPE_HISTORY_REMOTE_DB_CAPACITY") == "EXTERNALLY_MONITORED"
        except Exception:
            # Bad optional-work configuration must not terminate live strategy.
            self._config_valid = False

    def check(self, *, force: bool = False) -> StoragePressure:
        now = self._clock()
        if not force and self._current is not None and now - self._last_check < self._interval:
            return self._current
        self._last_check = now
        free: int | None = None
        count = 0
        if not self._enabled:
            state = "PAUSED_DISABLED"
        elif not self._config_valid:
            state = "PAUSED_STORAGE_UNAVAILABLE"
        else:
            try:
                if self._paths is None:
                    if not self._local:
                        if not self._remote_acknowledged:
                            raise ValueError("REMOTE_DATABASE_CAPACITY_UNVERIFIED")
                        self._scope = "REMOTE_DATABASE_EXTERNALLY_MONITORED"
                        additional = self._configured
                    elif self._configured:
                        self._scope = "LOCAL_DATABASE_EXPLICIT_PATHS"
                        additional = self._configured
                    else:
                        additional = self._discover()
                        if len(additional) < 2:
                            raise ValueError("LOCAL_DATABASE_PATHS_INCOMPLETE")
                        self._scope = "LOCAL_DATABASE_AUTO"
                    self._paths = tuple(dict.fromkeys((self._root, *additional)))
                for path in self._paths:
                    available = self._probe(path)
                    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
                        raise ValueError("STORAGE_PROBE_INVALID")
                    count += 1
                    free = available if free is None else min(free, available)
                if free is None:
                    raise ValueError("STORAGE_PROBE_UNAVAILABLE")
                low = free <= self._resume if self._paused else free < self._pause
                state = "PAUSED_LOW_DISK" if low else "RUNNING"
            except Exception:
                # Do not silently probe a readable parent: that may be a different
                # filesystem from private/missing database or external WAL paths.
                state = "PAUSED_STORAGE_UNAVAILABLE"
        self._paused = state != "RUNNING"
        self._current = StoragePressure(state, free, count, self._scope, self._pause, self._resume)
        return self._current
