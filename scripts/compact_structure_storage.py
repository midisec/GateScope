#!/usr/bin/env python3
"""Bounded, resumable lossless archive packing. Dry-run unless --apply is set.

No rows, failures, return samples or candle revisions are removed. Repacking
creates reusable database space; it does NOT promise an immediate `df` decrease.
Never runs VACUUM FULL, rebuilds large indexes, or disables immutable triggers.
Run directly on the PostgreSQL host, not through a forwarding tunnel. The
2 GiB application/data/WAL floor is mandatory, not a peak-WAL space guarantee.
Each invocation stops at its row/time budget; rerun explicitly to resume. No
background full-table rewrite is scheduled by this script.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import time

import psycopg
from psycopg.types.json import Jsonb

from prepare_service_envs import ROOT, SECRET_DIR, read_env
from gatescope.quant.structure_archive_codec import (
    canonical_record,
    forecast_comparison,
    pack_structure_record,
    unpack_structure_record,
)

MIN_FREE_BYTES = 2 * 1024**3


class StoragePause(Exception):
    """Safe, fixed-code refusal; never include database or credential details."""


def storage_paths(conn, root):
    """Run on the database host: a remote server path is not a local disk check.

    Unix sockets and loopback PostgreSQL are supported. Do not run through an
    SSH tunnel: inet_server_addr cannot distinguish a forwarded loopback socket.
    Resolve pg_wal separately because it can be a symlink to another filesystem.
    """
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        row = conn.execute("SELECT current_setting('data_directory'),host(inet_server_addr())").fetchone()
    if not row or not isinstance(row[0], str) or not Path(row[0]).is_absolute():
        raise StoragePause("PAUSED_STORAGE_UNVERIFIED")
    if row[1] is not None:
        try:
            address = ipaddress.ip_interface(row[1]).ip
            # IPv4-mapped IPv6 must be judged by the embedded actual address,
            # not by accepting the entire mapped-address namespace as local.
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            local = address.is_loopback
        except ValueError:
            local = False
        if not local:
            raise StoragePause("PAUSED_DATABASE_HOST_UNVERIFIED")
    return {"APPLICATION": Path(root), "POSTGRES_DATA": Path(row[0]), "POSTGRES_WAL": Path(row[0]) / "pg_wal"}


def check_storage(paths):
    """All scopes must be measurable and above the same non-overridable floor."""
    measurements = []
    for scope, path in paths.items():
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise OSError()
            usage = shutil.disk_usage(resolved)
            measurements.append({"scope": scope, "path": str(resolved), "device": resolved.stat().st_dev,
                                 "free_bytes": usage.free, "total_bytes": usage.total})
        except OSError:
            measurements.append({"scope": scope, "path": None, "device": None, "free_bytes": None,
                                 "total_bytes": None})
    status = "READY"
    if any(item["free_bytes"] is None for item in measurements):
        status = "PAUSED_STORAGE_UNVERIFIED"
    elif any(item["free_bytes"] < MIN_FREE_BYTES for item in measurements):
        status = "PAUSED_LOW_DISK"
    return status, measurements


def restore_forecast(conn, context, candidate, summary):
    reference = summary["_evidence"]
    if reference.get("path") != "first.forecast":
        raise ValueError("STAGE_COMPARISON_INTEGRITY_FAILED")
    row = conn.execute("""SELECT gatescope_security.structure_event_record(event_json,event_blob)
      FROM market_pattern_events WHERE context_id=%s AND candidate_id=%s AND sequence=%s""",
      (context, candidate, reference["sequence"])).fetchone()
    original = unpack_structure_record(row[0]).get("first", {}).get("forecast") if row else None
    if not isinstance(original, dict) or hashlib.sha256(canonical_record(original)).hexdigest() != reference["sha256"]:
        raise ValueError("STAGE_COMPARISON_INTEGRITY_FAILED")
    return original


def repack_event(conn, row, *, apply=False):
    context, candidate, sequence, original, before = row
    header, body = pack_structure_record(original)
    if body is None:
        return 0
    decoded = unpack_structure_record({**header, "_archive_data": base64.b64encode(body).decode("ascii")})
    if decoded != original:
        raise ValueError("ARCHIVE_ROUNDTRIP_CHANGED")
    projected = conn.execute(
        "SELECT pg_column_size(%s::jsonb)+octet_length(%s::bytea)", (Jsonb(header), body)
    ).fetchone()[0]
    # Keep PostgreSQL's existing TOAST representation if it is already smaller.
    if projected >= before * 0.95:
        return 0
    if apply:
        changed = conn.execute(
            "SELECT gatescope_security.repack_structure_event(%s,%s,%s,%s,%s,%s)",
            (context, candidate, sequence, Jsonb(original), Jsonb(header), body),
        ).fetchone()[0]
        if not changed:
            return 0
        persisted = conn.execute(
            """SELECT gatescope_security.structure_event_record(event_json,event_blob)
          FROM market_pattern_events WHERE context_id=%s AND candidate_id=%s AND sequence=%s""",
            (context, candidate, sequence),
        ).fetchone()[0]
        if unpack_structure_record(persisted) != original:
            raise ValueError("ARCHIVE_PERSISTED_INTEGRITY_FAILED")
    return before - projected


def compact_stage(conn, row, *, apply=False):
    context, candidate, phase, original, before = row
    if not isinstance(original, dict) or "_evidence" in original:
        return 0
    evidence = conn.execute(
        """SELECT sequence,gatescope_security.structure_event_record(event_json,event_blob)
      FROM market_pattern_events WHERE context_id=%s AND candidate_id=%s ORDER BY sequence LIMIT 1""",
        (context, candidate),
    ).fetchone()
    if not evidence:
        return 0
    record = unpack_structure_record(evidence[1])
    if record.get("first", {}).get("forecast") != original:
        return 0  # Never discard a forecast that cannot be recovered exactly.
    summary = forecast_comparison(original, evidence[0])
    if summary is None or "_evidence" not in summary:
        return 0
    if restore_forecast(conn, context, candidate, summary) != original:
        raise ValueError("STAGE_COMPARISON_INTEGRITY_FAILED")
    projected = conn.execute("SELECT pg_column_size(%s::jsonb)", (Jsonb(summary),)).fetchone()[0]
    if projected >= before * 0.95:
        return 0
    if apply:
        result = conn.execute(
            """UPDATE market_structure_stages SET forecast_json=%s
          WHERE context_id=%s AND candidate_id=%s AND entry_phase=%s AND forecast_json=%s""",
            (Jsonb(summary), context, candidate, phase, Jsonb(original)),
        )
        if result.rowcount != 1:
            return 0
        stored = conn.execute(
            "SELECT forecast_json FROM market_structure_stages WHERE context_id=%s AND candidate_id=%s AND entry_phase=%s",
            (context, candidate, phase),
        ).fetchone()[0]
        if (
            stored != summary
            or stored["_evidence"]["sha256"] != hashlib.sha256(canonical_record(original)).hexdigest()
            or restore_forecast(conn, context, candidate, stored) != original
        ):
            raise ValueError("STAGE_COMPARISON_INTEGRITY_FAILED")
    return before - projected


def run(conn, *, mode="events", apply=False, max_rows=500, seconds=20, batch_rows=32, root=ROOT):
    if (mode not in ("events", "stages") or type(max_rows) is not int or not 1 <= max_rows <= 10000
        or type(seconds) is not int or not 1 <= seconds <= 50
        or type(batch_rows) is not int or not 1 <= batch_rows <= 32):
        raise ValueError("INVALID_STORAGE_BUDGET")
    job = "structure-storage-1:" + mode
    report = {
        "status": "BUDGET_REACHED",
        "mode": mode,
        "apply": apply,
        "scanned": 0,
        "packed": 0,
        "committed_batches": 0,
        "estimated_saved_bytes": 0,
        "logical_estimated_saved_bytes": 0,
        "physical_reclaimed_bytes": None,
        "estimate_basis": "PAYLOAD_REPRESENTATION_ONLY_EXCLUDES_HEAP_INDEX_TOAST_OVERHEAD_AND_WAL",
        "minimum_free_bytes": MIN_FREE_BYTES,
        "space_policy": "REUSABLE_DATABASE_SPACE_NOT_IMMEDIATE_FILESYSTEM_RECLAIM",
        "disk_before": [],
        "disk_after": [],
        "last_committed_cursor": None,
    }
    started = time.monotonic()
    cursor = None
    try:
        paths = storage_paths(conn, root)
        status, report["disk_before"] = check_storage(paths)
        report["disk_after"] = report["disk_before"]
        if status != "READY":
            report["status"] = status
            return report
    except StoragePause as error:
        report["status"] = str(error)
        return report
    except Exception:
        report["status"] = "PAUSED_STORAGE_UNVERIFIED"
        return report
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        state = conn.execute(
            "SELECT cursor_json FROM gatescope_security.structure_storage_progress WHERE job=%s", (job,)
        ).fetchone()
        if state:
            cursor = state[0]
    report["start_cursor"] = cursor
    report["last_committed_cursor"] = cursor
    while report["scanned"] < max_rows and time.monotonic() - started < seconds:
        status, report["disk_after"] = check_storage(paths)
        if status != "READY":
            report["status"] = status
            break
        try:
            with conn.transaction():
                if not apply:
                    conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("SET LOCAL lock_timeout='1s'")
                conn.execute("SET LOCAL statement_timeout='5s'")
                # A dry run never takes the maintenance lock or writes progress.
                if apply:
                    locked = conn.execute("SELECT pg_try_advisory_xact_lock(7342297)").fetchone()[0]
                    if not locked:
                        report["status"] = "BUSY"
                        break
                    state = conn.execute(
                        "SELECT cursor_json FROM gatescope_security.structure_storage_progress WHERE job=%s", (job,),
                    ).fetchone()
                    if state:
                        cursor = state[0]
                        report["last_committed_cursor"] = cursor
                count = min(batch_rows, max_rows - report["scanned"])
                if mode == "events":
                    where = "AND (context_id,candidate_id,sequence)>(%s,%s,%s)" if cursor else ""
                    rows = conn.execute(
                        """SELECT context_id,candidate_id,sequence,event_json,pg_column_size(event_json)
                      FROM market_pattern_events WHERE event_blob IS NULL """ + where
                        + " ORDER BY context_id,candidate_id,sequence LIMIT %s", (*(cursor or []), count),
                    ).fetchall()
                    operation = repack_event
                else:
                    where = "AND (context_id,candidate_id,entry_phase)>(%s,%s,%s)" if cursor else ""
                    rows = conn.execute(
                        """SELECT context_id,candidate_id,entry_phase,forecast_json,pg_column_size(forecast_json)
                      FROM market_structure_stages WHERE forecast_json IS NOT NULL AND NOT forecast_json ? '_evidence' """
                        + where + " ORDER BY context_id,candidate_id,entry_phase LIMIT %s", (*(cursor or []), count),
                    ).fetchall()
                    operation = compact_stage
                if not rows:
                    report["status"] = "COMPLETE"
                    break
                saved = packed = 0
                processed = []
                for row in rows:
                    if processed and time.monotonic() - started >= seconds:
                        break
                    status, report["disk_after"] = check_storage(paths)
                    if status != "READY":
                        raise StoragePause(status)  # Roll back this whole batch, including progress.
                    benefit = operation(conn, row, apply=apply)
                    saved += benefit
                    packed += int(benefit > 0)
                    processed.append(row)
                next_cursor = list(processed[-1][:3])
                if apply:
                    status, report["disk_after"] = check_storage(paths)
                    if status != "READY":
                        raise StoragePause(status)
                    checked = hashlib.sha256()
                    for row in processed:
                        checked.update(canonical_record({"key": list(row[:3]), "value": row[3]}))
                    conn.execute(
                        """INSERT INTO gatescope_security.structure_storage_audit
                      (job,scanned,packed,estimated_saved_bytes,checked_digest) VALUES(%s,%s,%s,%s,%s)""",
                        (job, len(processed), packed, saved, checked.hexdigest()),
                    )
                    conn.execute(
                        """INSERT INTO gatescope_security.structure_storage_progress(job,cursor_json,scanned,packed,estimated_saved_bytes)
                      VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job) DO UPDATE SET cursor_json=EXCLUDED.cursor_json,
                      scanned=structure_storage_progress.scanned+EXCLUDED.scanned,
                      packed=structure_storage_progress.packed+EXCLUDED.packed,
                      estimated_saved_bytes=structure_storage_progress.estimated_saved_bytes+EXCLUDED.estimated_saved_bytes,updated_at=now()""",
                        (job, Jsonb(next_cursor), len(processed), packed, saved),
                    )
            # Counts/cursor only describe committed batches, never rolled-back work.
            cursor = next_cursor
            report["scanned"] += len(processed)
            report["packed"] += packed
            report["estimated_saved_bytes"] += saved
            report["logical_estimated_saved_bytes"] += saved
            if apply:
                report["committed_batches"] += 1
                report["last_committed_cursor"] = cursor
        except StoragePause as error:
            report["status"] = str(error)
            report["rolled_back_batch"] = True
            break
        except Exception as error:
            report["status"] = "FAILED_BATCH"
            report["error_type"] = type(error).__name__
            report["rolled_back_batch"] = True
            break
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["scan_cursor"] = cursor
    final_status, report["disk_after"] = check_storage(paths)
    if final_status != "READY" and report["status"] in {"BUDGET_REACHED", "COMPLETE"}:
        report["status"] = final_status
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mode", choices=("events", "stages"), default="events")
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--batch-rows", type=int, default=32, help="1..32 rows per transaction; start conservatively with 8")
    args = parser.parse_args()
    source = read_env(SECRET_DIR / "formal-runtime.env")
    url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        report = run(conn, mode=args.mode, apply=args.apply, max_rows=args.max_rows, seconds=args.seconds, batch_rows=args.batch_rows)
        print(json.dumps(report))
        if report["status"] == "FAILED_BATCH":
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "error_type": type(error).__name__,
                    "details": "private database output suppressed",
                }
            )
        )
        raise SystemExit(1) from None
