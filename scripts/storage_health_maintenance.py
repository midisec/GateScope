#!/usr/bin/env python3
"""Inspect storage without scanning business rows; explicit --apply tunes VACUUM.

Run on the database host. The default and the optional timer are read-only.
--apply only changes a fixed set of reloptions on three named public tables,
including their TOAST settings. It never issues VACUUM, deletes records, drops
indexes, restarts workers, or resumes historical jobs. Ordinary autovacuum makes
space reusable INSIDE PostgreSQL; this is not an OS-free-space recovery tool.

The conservative profile uses per-worker cost limits, NOT a global I/O cap.
PostgreSQL excludes explicitly tuned tables from automatic cost balancing.
Tail truncation is disabled to avoid its ACCESS EXCLUSIVE lock. A 2 GiB floor
is required to apply the profile; it is not a guarantee against later growth.
See deploy/systemd/gatescope-history-manual-hold.conf for an operator-installed
history hold: the free-space guard alone resumes above its upper threshold.

Exit 0: inspected/applied; 2: capacity/health warning; 1: safely refused/failed.
Only fixed codes and approved metadata are printed; never DSNs or exception text.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import shutil
import time

import psycopg
from psycopg.pq import TransactionStatus

from compact_structure_storage import storage_paths
from prepare_service_envs import ROOT, SECRET_DIR, read_env

GIB = 1024 ** 3
APPLY_MIN_FREE = 2 * GIB
TABLES = ("market_pattern_events", "market_structure_stages", "market_structure_returns")
HISTORY_WORKLOADS = ("market-worker", "strategy-worker", "research-worker")
PROFILE_VERSION = "low-impact-autovacuum-1"
SETTINGS = ("autovacuum", "track_counts", "autovacuum_vacuum_threshold",
    "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_cost_limit",
    "autovacuum_vacuum_cost_delay", "vacuum_cost_limit", "vacuum_cost_delay")
FIELDS = ("oid", "table", "kind", "owned", "options", "planner_rows",
    "heap_bytes", "index_bytes", "toast_bytes", "total_bytes", "live_estimate",
    "dead_estimate", "last_autovacuum", "last_autoanalyze", "toast_options")
RELATION_SQL = """SELECT c.oid,c.relname,c.relkind,
    c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user),c.reloptions,c.reltuples::float8,
    pg_relation_size(c.oid),pg_indexes_size(c.oid),
    CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END,
    pg_total_relation_size(c.oid),s.n_live_tup,s.n_dead_tup,s.last_autovacuum,s.last_autoanalyze,t.reloptions
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_stat_all_tables s ON s.relid=c.oid LEFT JOIN pg_class t ON t.oid=c.reltoastrelid
    WHERE n.nspname='public' AND c.relname=ANY(%s) ORDER BY c.relname"""


class Refused(Exception):
    """Callers must use fixed, non-sensitive codes only."""


class Deadline:
    def __init__(self, conn, seconds=20, clock=time.monotonic):
        self.conn, self.clock = conn, clock
        self.end = clock() + seconds

    def check(self):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise Refused("TIME_BUDGET_EXCEEDED")
        return remaining

    def execute(self, query, args=()):
        remaining = self.check()
        # Fixed prefix plus a bounded integer, never caller SQL or an identifier.
        milliseconds = max(1, min(3000, int(remaining * 1000)))
        self.conn.execute(f"SET LOCAL statement_timeout='{milliseconds}ms'")
        result = self.conn.execute(query, args)
        self.check()
        return result


def disk_report(paths):
    result = []
    for scope, path in paths.items():
        try:
            resolved = Path(path).resolve(strict=True)
            if not resolved.is_dir():
                raise OSError()
            usage = shutil.disk_usage(resolved)
            if usage.total <= 0 or usage.free < 0:
                raise OSError()
            result.append({"scope": scope, "free_bytes": usage.free,
                "total_bytes": usage.total, "used_percent": round(100 * usage.used / usage.total, 2)})
        except OSError:
            raise Refused("STORAGE_UNVERIFIED") from None
    if not result:
        raise Refused("STORAGE_UNVERIFIED")
    return result


def profile(table):
    if table not in TABLES:
        raise Refused("TABLE_NOT_ALLOWED")
    scale = "0.001" if table == "market_pattern_events" else "0.01"
    ordinary = {"autovacuum_enabled": "true", "autovacuum_vacuum_threshold": "5000",
        "autovacuum_vacuum_scale_factor": scale, "autovacuum_vacuum_cost_limit": "50",
        "autovacuum_vacuum_cost_delay": "20", "vacuum_truncate": "false"}
    return {**ordinary, **{"toast." + key: value for key, value in ordinary.items()}}


def parse_options(values):
    result = {}
    for value in values or ():
        if not isinstance(value, str) or "=" not in value:
            raise Refused("RELOPTIONS_UNVERIFIED")
        key, setting = value.split("=", 1)
        result[key] = setting
    return result


def desired_options(table, current, settings):
    """Do not make an already more frequent/gentler local policy less gentle."""
    desired = profile(table)
    for key, value in tuple(desired.items()):
        base = key.removeprefix("toast.")
        old = current.get(key)
        if old is None:
            old = current.get(base) if key.startswith("toast.") else None
        if old is None and base in {"autovacuum_vacuum_cost_limit", "autovacuum_vacuum_cost_delay"}:
            old = settings.get(base)
        if old is None or base in {"autovacuum_enabled", "vacuum_truncate"}:
            continue
        try:
            number = Decimal(old)
            if not number.is_finite():
                raise InvalidOperation()
            if base == "autovacuum_vacuum_cost_delay":
                number = max(number, Decimal(value))
            elif base == "autovacuum_vacuum_cost_limit":
                number = min(number, Decimal(value)) if number > 0 else Decimal(value)
            else:
                # A zero threshold is a valid stricter existing setting.
                number = min(number, Decimal(value)) if number >= 0 else Decimal(value)
            desired[key] = format(number, "f")
        except (InvalidOperation, ValueError, TypeError):
            raise Refused("RELOPTIONS_UNVERIFIED") from None
    return desired


def _same_option(first, second):
    if first is None:
        return False
    booleans = {"true": True, "on": True, "1": True, "false": False, "off": False, "0": False}
    if second in {"true", "false"}:
        return booleans.get(first) is booleans[second]
    try:
        return Decimal(first) == Decimal(second)
    except InvalidOperation:
        return False


def _threshold(rows, options, settings):
    if rows is None or rows < 0 or not math.isfinite(rows):
        return None
    return round(float(options.get("autovacuum_vacuum_threshold", settings["autovacuum_vacuum_threshold"]))
        + rows * float(options.get("autovacuum_vacuum_scale_factor", settings["autovacuum_vacuum_scale_factor"])), 1)


def inspect(conn, budget):
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        settings = dict(budget.execute("SELECT name,setting FROM pg_settings WHERE name=ANY(%s)", (list(SETTINGS),)).fetchall())
        if set(settings) != set(SETTINGS):
            raise Refused("DATABASE_SETTINGS_UNVERIFIED")
        rows = budget.execute(RELATION_SQL, (list(TABLES),)).fetchall()
        tables = []
        for row in rows:
            item = dict(zip(FIELDS, row, strict=True))
            current = parse_options(item.pop("options"))
            current.update({"toast." + key: value for key, value in parse_options(item.pop("toast_options")).items()})
            wanted = desired_options(item["table"], current, settings)
            # Output only known policy keys, not arbitrary extension reloptions.
            item["current_options"] = {key: value for key, value in current.items() if key in wanted}
            item["desired_options"] = wanted
            item["option_changes"] = {key: value for key, value in wanted.items() if not _same_option(current.get(key), value)}
            item["current_vacuum_trigger_estimate"] = _threshold(item["planner_rows"], current, settings)
            item["proposed_vacuum_trigger_estimate"] = _threshold(item["planner_rows"], wanted, settings)
            for key in ("last_autovacuum", "last_autoanalyze"):
                item[key] = item[key].isoformat() if item[key] else None
            tables.append(item)
        if {item["table"] for item in tables} != set(TABLES):
            raise Refused("REQUIRED_TABLE_MISSING")
        db = budget.execute("SELECT pg_database_size(current_database()),stats_reset FROM pg_stat_database WHERE datname=current_database()").fetchone()
        history = budget.execute("""SELECT workload,heartbeat_at,status_json->>'history_state'
            FROM worker_heartbeats WHERE workload=ANY(%s) ORDER BY workload""", (list(HISTORY_WORKLOADS),)).fetchall()
        spaces = budget.execute("SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global') ORDER BY oid LIMIT 33").fetchall()
        if len(spaces) > 32:
            raise Refused("TABLESPACE_BUDGET_EXCEEDED")
    return {"database_bytes": db[0], "stats_reset": db[1].isoformat() if db[1] else None,
        "settings": settings, "tables": tables,
        "history": [{"workload": row[0], "heartbeat_at": row[1].isoformat(),
            "age_seconds": round(max(0., (datetime.now(timezone.utc) - row[1]).total_seconds()), 1),
            "state": row[2] if row[2] in {"RUNNING", "PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE"} else "UNKNOWN"}
            for row in history]}, [Path(row[0]) for row in spaces if row[0]]


def run(conn, *, apply=False, root=ROOT, seconds=20, clock=time.monotonic):
    if type(apply) is not bool or type(seconds) is not int or not 1 <= seconds <= 30:
        raise Refused("INVALID_ARGUMENTS")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise Refused("OPEN_TRANSACTION_NOT_ALLOWED")
    budget = Deadline(conn, seconds, clock)
    # Existing local-host verification also resolves an external pg_wal mount.
    paths = storage_paths(conn, root)
    budget.check()
    details, tablespaces = inspect(conn, budget)
    paths.update({f"TABLESPACE_{index}": path for index, path in enumerate(tablespaces, 1)})
    disks = disk_report(paths)
    free = min(item["free_bytes"] for item in disks)
    alerts = []
    if free < 3 * GIB:
        alerts.append("CRITICAL_LOW_DISK")
    elif free < 5 * GIB:
        alerts.append("LOW_DISK_HEADROOM")
    if details["settings"]["autovacuum"] != "on" or details["settings"]["track_counts"] != "on":
        alerts.append("AUTOVACUUM_GLOBALLY_UNAVAILABLE")
    if any(item["option_changes"] for item in details["tables"]):
        alerts.append("AUTOVACUUM_PROFILE_PENDING")
    if any(item["state"] != "PAUSED_DISABLED" or item["age_seconds"] > 60
            for item in details["history"]) or len(details["history"]) != len(HISTORY_WORKLOADS):
        alerts.append("HISTORY_MANUAL_HOLD_NOT_CONFIRMED")
    report = {"schema": "storage-health-1", "profile": PROFILE_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(), "mode": "APPLY" if apply else "INSPECT",
        "action_status": "DRY_RUN", "alerts": alerts, "disks": disks, **details,
        "table_snapshot": "BEFORE_APPLY" if apply else "READ_ONLY_CURRENT",
        "applied_tables": [], "physical_reclaimed_bytes": None,
        "space_policy": "INTERNAL_REUSE_NOT_FILESYSTEM_RECLAIM",
        "history_policy": "NO_JOB_OR_WORKER_CHANGES_MANUAL_HOLD_DEPLOYMENT_REQUIRED",
        "statistics_policy": "ESTIMATES_NOT_LIVE_ROW_COUNTS_OR_RECLAIMABLE_BYTES"}
    if not apply:
        return report
    if free < APPLY_MIN_FREE:
        raise Refused("APPLY_REQUIRES_2_GIB_FREE")
    if "AUTOVACUUM_GLOBALLY_UNAVAILABLE" in alerts:
        raise Refused("AUTOVACUUM_GLOBALLY_UNAVAILABLE")
    if any(item["kind"] != "r" for item in details["tables"]):
        raise Refused("ORDINARY_TABLE_REQUIRED")
    changed = [item for item in details["tables"] if item["option_changes"]]
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout='250ms'")
        role = budget.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()
        if not role or (not role[0] and not all(item["owned"] for item in details["tables"])):
            raise Refused("TABLE_OWNER_REQUIRED")
        # Acquire all locks before changing any options. Ordinary autovacuum
        # uses this mode too; conflicting maintenance causes immediate refusal.
        for item in changed:
            if item["kind"] != "r":
                raise Refused("ORDINARY_TABLE_REQUIRED")
            table = item["table"]
            if table not in TABLES:
                raise Refused("TABLE_NOT_ALLOWED")
            budget.execute(f"LOCK TABLE public.{table} IN SHARE UPDATE EXCLUSIVE MODE NOWAIT")
        current_rows = budget.execute(RELATION_SQL, (list(TABLES),)).fetchall()
        current = {row[1]: dict(zip(FIELDS, row, strict=True)) for row in current_rows}
        for item in changed:
            current_item = current.get(item["table"])
            if not current_item or current_item["oid"] != item["oid"]:
                raise Refused("TABLE_CHANGED_DURING_INSPECTION")
            options = parse_options(current_item["options"])
            options.update({"toast." + key: value for key, value in parse_options(current_item["toast_options"]).items()})
            if {key: value for key, value in options.items() if key in item["desired_options"]} != item["current_options"]:
                raise Refused("POLICY_CHANGED_DURING_INSPECTION")
            if min(row["free_bytes"] for row in disk_report(paths)) < APPLY_MIN_FREE:
                raise Refused("APPLY_REQUIRES_2_GIB_FREE")
            # Table names, keys and numeric/boolean values originate solely from
            # the fixed profile plus validated Decimal values, never CLI SQL.
            clause = ",".join(key + "=" + value for key, value in item["option_changes"].items())
            budget.execute(f"ALTER TABLE public.{item['table']} SET ({clause})")
        budget.check()
        if min(row["free_bytes"] for row in disk_report(paths)) < APPLY_MIN_FREE:
            raise Refused("APPLY_REQUIRES_2_GIB_FREE")
    report["applied_tables"] = [item["table"] for item in changed]
    report["action_status"] = "APPLIED" if changed else "ALREADY_CONFIGURED"
    report["alerts"] = [code for code in alerts if code != "AUTOVACUUM_PROFILE_PENDING"]
    try:
        report["disks"] = disk_report(paths)
    except Exception:
        # The transaction already committed. Never present a postcheck failure
        # as evidence that the reloptions were rolled back.
        report["alerts"].append("POSTCOMMIT_STORAGE_CHECK_FAILED")
    return report


def safe_failure(error):
    if isinstance(error, Refused):
        return {"schema": "storage-health-1", "action_status": "REFUSED", "reason": str(error)}
    code = getattr(error, "sqlstate", None)
    return {"schema": "storage-health-1", "action_status": "FAILED",
        "reason": {"55P03": "LOCK_BUSY", "57014": "QUERY_BUDGET_EXCEEDED",
            "42501": "INSUFFICIENT_PRIVILEGE"}.get(code, "STORAGE_CHECK_FAILED")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Only set the fixed low-impact reloptions; never run VACUUM")
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    try:
        source = read_env(SECRET_DIR / "formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
            report = run(conn, apply=args.apply, seconds=args.seconds)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2 if report["alerts"] else 0
    except Exception as error:
        print(json.dumps(safe_failure(error), sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
