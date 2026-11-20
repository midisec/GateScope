#!/usr/bin/env python3
"""Explicit, bounded owner-only deletion of unreferenced REPLAY contexts.

Dry-run is read-only. Apply is irreversible: immutable live/RECORDED history,
bars and private ledgers are retained, but selected REPLAY evidence is deleted.
Deleted space can be reused by PostgreSQL; filesystem space is NOT promised.
Run on the actual PostgreSQL host, never via an SSH forwarding tunnel.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import time
from uuid import UUID, uuid4

import psycopg

from compact_structure_storage import StoragePause, storage_paths
from prepare_service_envs import ROOT, SECRET_DIR, read_env

MIN_FREE_BYTES = 1024**3
TABLES = frozenset(("market_structure_stages", "market_structure_returns",
                   "market_pattern_report_versions", "market_pattern_events", "market_pattern_reports"))


def check_storage(paths):
    measurements = []
    for scope, path in paths.items():
        try:
            resolved = Path(path).resolve(strict=True)
            if not resolved.is_dir():
                raise OSError()
            usage = shutil.disk_usage(resolved)
            measurements.append({"scope": scope, "path": str(resolved), "device": resolved.stat().st_dev,
                                 "free_bytes": usage.free, "total_bytes": usage.total})
        except OSError:
            measurements.append({"scope": scope, "path": None, "device": None,
                                 "free_bytes": None, "total_bytes": None})
    if not measurements or any(item["free_bytes"] is None for item in measurements):
        return "PAUSED_STORAGE_UNVERIFIED", measurements
    if any(item["free_bytes"] < MIN_FREE_BYTES for item in measurements):
        return "PAUSED_LOW_DISK", measurements
    return "READY", measurements


def _limits(conn, *, readonly=False):
    if readonly:
        conn.execute("SET TRANSACTION READ ONLY")
    conn.execute("SET LOCAL lock_timeout='1s'")
    conn.execute("SET LOCAL statement_timeout='3s'")
    conn.execute("SET LOCAL idle_in_transaction_session_timeout='5s'")


def _safe_failure(error):
    # SQL details, argument payloads and DSNs must never escape the CLI.
    code = getattr(error, "sqlstate", None)
    return {"55P03": "PAUSED_LOCK_BUSY", "57014": "PAUSED_QUERY_BUDGET",
            "42501": "PAUSED_OWNER_REQUIRED"}.get(code, "FAILED_SAFE_ROLLBACK")


def run(conn, *, instrument_id, apply=False, job_id=None, max_rows=100, seconds=10,
        batch_rows=100, root=ROOT):
    target = str(UUID(str(instrument_id)))
    job = str(UUID(str(job_id))) if job_id is not None else str(uuid4())
    if (type(apply) is not bool or type(max_rows) is not int or not 1 <= max_rows <= 500
        or type(seconds) is not int or not 1 <= seconds <= 30
        or type(batch_rows) is not int or not 1 <= batch_rows <= 500):
        raise ValueError("INVALID_PURGE_BUDGET")
    report = {"status": "BUDGET_REACHED", "apply": apply, "instrument_id": target,
              "job_id": job if apply else None, "job_closed": None, "deleted_rows": 0,
              "committed_batches": 0, "counts": {}, "minimum_free_bytes": MIN_FREE_BYTES,
              "physical_reclaimed_bytes": None, "disk_before": [], "disk_after": [],
              "completion_scope": "BOUNDED_JOB_MANIFEST_NOT_ENTIRE_INSTRUMENT",
              "space_policy": "LOGICAL_DELETIONS_ONLY_NO_FILESYSTEM_RECLAIM_PROMISE"}
    started = time.monotonic()
    opened = False
    try:
        paths = storage_paths(conn, root)
        status, report["disk_before"] = check_storage(paths)
        report["disk_after"] = report["disk_before"]
        if status != "READY":
            report["status"] = status
            return report
        if not apply:
            with conn.transaction():
                _limits(conn, readonly=True)
                preview = conn.execute("SELECT gatescope_security.inspect_replay_purge(%s,1)",
                                       (target,)).fetchone()[0]
            # This is a bounded context sample, never an entire-database count.
            report["preview"] = preview
            report["status"] = "DRY_RUN"
            return report
        with conn.transaction():
            _limits(conn)
            manifest = conn.execute("SELECT gatescope_security.open_replay_purge(%s,%s)",
                                    (job, target)).fetchone()[0]
            status, report["disk_after"] = check_storage(paths)
            if status != "READY":
                raise StoragePause(status)
        opened = True
        report["contexts"] = manifest["contexts"]
        # Empty phases/retained references can return zero, so bound calls too.
        for _ in range(64):
            if time.monotonic() - started >= seconds or report["deleted_rows"] >= max_rows:
                break
            status, report["disk_after"] = check_storage(paths)
            if status != "READY":
                report["status"] = status
                break
            with conn.transaction():
                _limits(conn)
                result = conn.execute("SELECT gatescope_security.replay_purge_batch(%s,%s)",
                                      (job, min(batch_rows, max_rows - report["deleted_rows"]))).fetchone()[0]
                deleted = result.get("deleted_rows")
                counts = result.get("counts")
                if (type(deleted) is not int or not 0 <= deleted <= min(batch_rows, max_rows - report["deleted_rows"])
                    or not isinstance(counts, dict) or not set(counts).issubset(TABLES)
                    or any(type(value) is not int or value < 0 for value in counts.values())
                    or sum(counts.values()) != deleted):
                    raise ValueError("INVALID_PURGE_RESULT")
                status, report["disk_after"] = check_storage(paths)
                if status != "READY":
                    raise StoragePause(status)
            # Update counters only after transaction commit; failed batches count zero.
            report["deleted_rows"] += deleted
            report["committed_batches"] += 1
            for table, amount in counts.items():
                report["counts"][table] = report["counts"].get(table, 0) + amount
            if result["status"] == "COMPLETE":
                report["status"] = "COMPLETE"
                break
    except StoragePause as error:
        report["status"] = str(error)
    except Exception as error:
        report["status"] = _safe_failure(error)
    finally:
        if opened:
            completion = "DONE" if report["status"] == "COMPLETE" else "PAUSED"
            if report["status"] == "FAILED_SAFE_ROLLBACK":
                completion = "FAILED"
            try:
                with conn.transaction():
                    _limits(conn)
                    conn.execute("SELECT gatescope_security.close_replay_purge(%s,%s)", (job, completion))
                report["job_closed"] = True
            except Exception:
                report["job_closed"] = False
                report["closure_note"] = "JOB_LEASE_EXPIRES_WITHIN_5_MINUTES_PARTIAL_TOMBSTONES_REMAIN"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-id", type=UUID, required=True)
    parser.add_argument("--job-id", type=UUID, help="Explicitly resume this same instrument's previous job")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=10)
    parser.add_argument("--batch-rows", type=int, default=100)
    args = parser.parse_args()
    try:
        source = read_env(SECRET_DIR / "formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            report = run(conn, instrument_id=args.instrument_id, apply=args.apply, job_id=args.job_id,
                         max_rows=args.max_rows, seconds=args.max_seconds, batch_rows=args.batch_rows)
    except Exception:
        report = {"status": "FAILED_CONFIGURATION_OR_CONNECTION", "deleted_rows": 0,
                  "physical_reclaimed_bytes": None}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"DRY_RUN", "COMPLETE", "BUDGET_REACHED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
