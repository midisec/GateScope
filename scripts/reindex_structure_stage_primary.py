#!/usr/bin/env python3
"""One-index guarded maintenance; default is a read-only density diagnostic.

Only public.market_structure_stages_pkey is accepted. No extension installation,
VACUUM FULL, table rewrite, row deletion or business-role privilege changes.
REINDEX CONCURRENTLY still reads the table twice and requires a new index copy,
sorting space and WAL. The disk watchdog is a cancellation threshold, not a
reservation or a guarantee against all allocation spikes. Run on the DB host.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import threading
import time

import psycopg
from psycopg.pq import TransactionStatus

from compact_structure_storage import StoragePause, check_storage, storage_paths
from prepare_service_envs import ROOT, SECRET_DIR, read_env

INDEX = "market_structure_stages_pkey"
TABLE = "market_structure_stages"
MIN_START_FREE_BYTES = 3 * 1024**3
MIN_GAIN_BYTES = 128 * 1024**2
MAX_LEAF_DENSITY = 70.0
DEFINITION = ("CREATE UNIQUE INDEX market_structure_stages_pkey ON public.market_structure_stages "
              "USING btree (context_id, candidate_id, entry_phase)")
LOCK_KEY = 7342308


def indexes(conn):
    return conn.execute("""SELECT coalesce(jsonb_agg(jsonb_build_object(
      'oid',c.oid,'name',c.relname,'table_oid',i.indrelid,'definition',pg_get_indexdef(c.oid),
      'tablespace_oid',coalesce(nullif(c.reltablespace,0),(SELECT dattablespace FROM pg_database WHERE datname=current_database())),
      'bytes',pg_relation_size(c.oid),'kind',c.relkind,'valid',i.indisvalid,'ready',i.indisready,
      'live',i.indislive,'primary',i.indisprimary,'unique',i.indisunique,'replica_identity',i.indisreplident,
      'clustered',i.indisclustered,'key_count',i.indnkeyatts,'attribute_count',i.indnatts,
      'keys',i.indkey::smallint[],'opclasses',i.indclass::oid[],'collations',i.indcollation::oid[],
      'options',i.indoption::smallint[],'predicate',pg_get_expr(i.indpred,i.indrelid),
      'expressions',pg_get_expr(i.indexprs,i.indrelid),
      'constraints',(SELECT coalesce(jsonb_agg(jsonb_build_object('oid',p.oid,'name',p.conname,'type',p.contype,
        'table_oid',p.conrelid)),'[]') FROM pg_constraint p WHERE p.conindid=c.oid),
      'other_dependency',EXISTS(SELECT 1 FROM pg_depend d WHERE
        (d.refclassid='pg_class'::regclass AND d.refobjid=c.oid AND d.classid<>'pg_constraint'::regclass)
        OR (d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype IN ('e','x'))))), '[]')
      FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND i.indrelid='public.market_structure_stages'::regclass""").fetchone()[0]


def _shape(row):
    if not row or not isinstance(row.get("name"), str):
        return False
    expected = DEFINITION.replace(INDEX, row["name"], 1)
    return (row.get("kind") == "i" and row.get("unique") is True
            and row.get("key_count") == row.get("attribute_count") == 3
            and row.get("predicate") is None and row.get("expressions") is None
            and row.get("replica_identity") is False and row.get("clustered") is False
            and row.get("other_dependency") is False
            and " ".join(row.get("definition", "").split()) == expected)


def valid_primary(rows):
    candidates = [row for row in rows if row["name"] == INDEX]
    if len(candidates) != 1:
        raise StoragePause("PAUSED_PRIMARY_INDEX_UNVERIFIED")
    row = candidates[0]
    if (not _shape(row) or not all(row.get(key) is True for key in ("valid", "ready", "live", "primary"))
        or len(row["constraints"]) != 1 or row["constraints"][0]["type"] != "p"
        or row["constraints"][0]["table_oid"] != row["table_oid"]):
        raise StoragePause("PAUSED_PRIMARY_INDEX_UNVERIFIED")
    return row


def _same_keys(left, right):
    return all(left.get(key) == right.get(key) for key in
               ("table_oid", "tablespace_oid", "keys", "opclasses", "collations", "options", "key_count", "attribute_count"))


def storage_tablespaces(conn, paths):
    """Measure real DB-default/heap/index locations, including tablespace links.

    The build connection forces temp_tablespaces='', so sort spill uses the
    checked database-default tablespace rather than an inherited unverified one.
    Non-default index definitions are independently refused by the shape guard.
    """
    rows = conn.execute("""WITH db AS (SELECT dattablespace FROM pg_database WHERE datname=current_database()),
      locations AS (
        SELECT 'DATABASE_TABLESPACE' scope,dattablespace oid FROM db
        UNION ALL SELECT 'INDEX_TABLESPACE',coalesce(nullif(c.reltablespace,0),db.dattablespace)
          FROM pg_class c CROSS JOIN db WHERE c.oid='public.market_structure_stages_pkey'::regclass
        UNION ALL SELECT 'HEAP_TABLESPACE',coalesce(nullif(c.reltablespace,0),db.dattablespace)
          FROM pg_class c CROSS JOIN db WHERE c.oid='public.market_structure_stages'::regclass)
      SELECT scope,pg_tablespace_location(oid) FROM locations""").fetchall()
    if {row[0] for row in rows} != {"DATABASE_TABLESPACE", "INDEX_TABLESPACE", "HEAP_TABLESPACE"} or len(rows) != 3:
        raise StoragePause("PAUSED_TABLESPACE_UNVERIFIED")
    result = dict(paths)
    for scope, location in rows:
        if not isinstance(location, str) or (location and not Path(location).is_absolute()):
            raise StoragePause("PAUSED_TABLESPACE_UNVERIFIED")
        result[scope] = Path(location) if location else paths["POSTGRES_DATA"]
    return result


def cleanup_candidates(rows, *, original, original_oids, owned_new_oids):
    """Only exact invalid artifacts of this invocation; never name-prefix alone."""
    current = valid_primary(rows)
    if not _same_keys(current, original) or current["constraints"] != original["constraints"]:
        raise StoragePause("PAUSED_PRIMARY_CONSTRAINT_CHANGED")
    allowed = []
    for row in rows:
        new = re.fullmatch(re.escape(INDEX) + r"_ccnew\d*", row["name"])
        old = re.fullmatch(re.escape(INDEX) + r"_ccold\d*", row["name"])
        belongs = ((new and row["oid"] not in original_oids and row["oid"] in owned_new_oids
                    and current["oid"] == original["oid"])
                   or (old and row["oid"] == original["oid"] and current["oid"] != original["oid"]))
        if not belongs:
            continue
        if (not _shape(row) or not _same_keys(row, original) or row.get("valid") is not False
            or row.get("primary") is not False or row["constraints"]):
            raise StoragePause("PAUSED_REINDEX_ARTIFACT_UNVERIFIED")
        allowed.append(row)
    return allowed


def density_decision(stats, index_bytes):
    density = stats.get("avg_leaf_density")
    if (isinstance(density, bool) or not isinstance(density, (int, float))
        or not math.isfinite(density) or not 0 <= density <= 100
        or type(stats.get("index_size")) is not int or stats["index_size"] <= 0
        or abs(stats["index_size"] - index_bytes) > max(1024**2, index_bytes * .02)):
        raise StoragePause("PAUSED_DENSITY_UNVERIFIED")
    # Diagnostic only: not an exact post-build size or a space reservation.
    estimated_gain = max(0, int(index_bytes * (1 - density / 90)))
    return {"leaf_density_percent": density, "estimated_index_gain_bytes": estimated_gain,
            "estimate_basis": "LEAF_DENSITY_VS_DEFAULT_90_PERCENT_NOT_GUARANTEED",
            "eligible": density < MAX_LEAF_DENSITY and estimated_gain >= MIN_GAIN_BYTES}


def monitor_build(build, done, paths, *, deadline, disk_check=check_storage, now=time.monotonic, observe=None):
    while not done.wait(.25):
        status, disk = disk_check(paths)
        reason = status if status != "READY" else "PAUSED_TIME_BUDGET" if now() >= deadline else None
        if reason:
            build.cancel()
            return reason, disk
        if observe is not None:
            try:
                observe()
            except Exception:
                build.cancel()
                return "PAUSED_PROGRESS_UNVERIFIED", disk
    return "FINISHED", disk_check(paths)[1]


def _limits(conn, timeout="3s"):
    conn.execute("SELECT set_config('lock_timeout','1s',false),set_config('statement_timeout',%s,false)", (timeout,))


def run(url, *, apply=False, max_seconds=180):
    if type(apply) is not bool or type(max_seconds) is not int or not 1 <= max_seconds <= 180:
        raise ValueError("INVALID_REINDEX_BUDGET")
    report = {"index": "public." + INDEX, "apply": apply, "status": "NOT_STARTED", "disk_before": [],
              "disk_after": [], "index_before_bytes": None, "index_after_bytes": None,
              "physical_index_reclaimed_bytes": None, "removed_invalid_artifacts": [],
              "minimum_start_free_bytes": MIN_START_FREE_BYTES, "cancel_below_free_bytes": 2 * 1024**3,
              "phase": "PRECHECK", "reindex_started": False, "build_query_ended": False}
    locked = False
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
            try:
                _limits(admin)
                paths = storage_paths(admin, ROOT)
                status, report["disk_before"] = check_storage(paths)
                if status != "READY":
                    raise StoragePause(status)
                before = indexes(admin)
                original = valid_primary(before)
                paths = storage_tablespaces(admin, paths)
                status, report["disk_before"] = check_storage(paths)
                if status != "READY":
                    raise StoragePause(status)
                if any(re.fullmatch(re.escape(INDEX) + r"_cc(?:new|old)\d*", row["name"]) for row in before):
                    raise StoragePause("PAUSED_EXISTING_REINDEX_ARTIFACT_MANUAL_REVIEW")
                report["index_before_bytes"] = original["bytes"]
                if not admin.execute("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pgstattuple')").fetchone()[0]:
                    raise StoragePause("PAUSED_DIAGNOSTIC_NOT_INSTALLED")
                _limits(admin, "10s")
                report["phase"] = "DIAGNOSTIC"
                with admin.transaction():
                    admin.execute("SET TRANSACTION READ ONLY")
                    stats = admin.execute("SELECT row_to_json(s) FROM public.pgstatindex('public.market_structure_stages_pkey'::regclass) s").fetchone()[0]
                report["diagnostic"] = density_decision(stats, original["bytes"])
                if not apply:
                    report["status"] = "DRY_RUN"
                    return report
                if not report["diagnostic"]["eligible"]:
                    raise StoragePause("PAUSED_INSUFFICIENT_EXPECTED_GAIN")
                status, report["disk_after"] = check_storage(paths)
                if status != "READY" or any(row["free_bytes"] < MIN_START_FREE_BYTES for row in report["disk_after"]):
                    raise StoragePause("PAUSED_START_DISK_BUDGET")
                _limits(admin)
                locked = admin.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]
                if not locked:
                    raise StoragePause("PAUSED_REINDEX_BUSY")
                if admin.execute("""SELECT EXISTS(SELECT 1 FROM pg_stat_progress_create_index WHERE relid=%s)
                  OR EXISTS(SELECT 1 FROM pg_stat_activity WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database())
                    AND pid<>pg_backend_pid() AND backend_xmin IS NOT NULL AND xact_start<now()-interval '2 minutes')""",
                    (original["table_oid"],)).fetchone()[0]:
                    raise StoragePause("PAUSED_EXISTING_BUILD_OR_LONG_TRANSACTION")
                # Recheck the identity after acquiring our maintenance lock.
                checked = valid_primary(indexes(admin))
                if checked["oid"] != original["oid"] or not _same_keys(checked, original):
                    raise StoragePause("PAUSED_PRIMARY_INDEX_CHANGED")
                errors = []
                owned_new_oids = set()
                with psycopg.connect(url, autocommit=True, connect_timeout=5) as build:
                    _limits(build, str(max_seconds) + "s")
                    build.execute("SET maintenance_work_mem='32MB'")
                    build.execute("SET max_parallel_maintenance_workers=0")
                    build.execute("SET temp_file_limit='1GB'")
                    build.execute("SET temp_tablespaces=''")
                    done = threading.Event()
                    _limits(admin, "1s")

                    def observe():
                        progress = admin.execute("SELECT index_relid FROM pg_stat_progress_create_index WHERE pid=%s AND relid=%s",
                                                 (build.info.backend_pid, original["table_oid"])).fetchone()
                        if progress and progress[0] and progress[0] != original["oid"]:
                            owned_new_oids.add(progress[0])

                    def work():
                        try:
                            build.execute("REINDEX INDEX CONCURRENTLY public.market_structure_stages_pkey")
                        except Exception as error:
                            errors.append(getattr(error, "sqlstate", None) or "BUILD_FAILED")
                        finally:
                            done.set()

                    thread = threading.Thread(target=work, daemon=True)
                    report["phase"] = "BUILD"
                    thread.start()
                    report["reindex_started"] = True
                    try:
                        outcome, report["disk_after"] = monitor_build(build, done, paths,
                                                                      deadline=time.monotonic() + max_seconds, observe=observe)
                    except KeyboardInterrupt:
                        build.cancel()
                        outcome = "PAUSED_OPERATOR_CANCELLED"
                    thread.join(timeout=5)
                    if thread.is_alive():
                        build.cancel()
                        raise StoragePause("PAUSED_CANCELLATION_PENDING_MANUAL_REVIEW")
                    if build.info.transaction_status != TransactionStatus.IDLE:
                        raise StoragePause("PAUSED_BUILD_TRANSACTION_NOT_IDLE_MANUAL_REVIEW")
                    report["build_query_ended"] = True
                    report["observed_build_index_oids"] = sorted(owned_new_oids)
                report["phase"] = "POST_BUILD_VALIDATION"
                after = indexes(admin)
                # Never remove an arbitrary similarly named index or a still-valid constraint.
                for artifact in cleanup_candidates(after, original=original, original_oids={row["oid"] for row in before}, owned_new_oids=owned_new_oids):
                    fresh = cleanup_candidates(indexes(admin), original=original, original_oids={row["oid"] for row in before}, owned_new_oids=owned_new_oids)
                    if not any(row["oid"] == artifact["oid"] and row["name"] == artifact["name"] for row in fresh):
                        raise StoragePause("PAUSED_REINDEX_ARTIFACT_CHANGED")
                    _limits(admin, "10s")
                    admin.execute(psycopg.sql.SQL("DROP INDEX CONCURRENTLY {}").format(psycopg.sql.Identifier("public", artifact["name"])))
                    report["removed_invalid_artifacts"].append({"oid": artifact["oid"], "name": artifact["name"]})
                final = indexes(admin)
                primary = valid_primary(final)
                report["remaining_reindex_artifacts"] = [{"oid": row["oid"], "name": row["name"], "valid": row["valid"]}
                    for row in final if re.fullmatch(re.escape(INDEX) + r"_cc(?:new|old)\d*", row["name"])]
                report["index_after_bytes"] = primary["bytes"]
                original_gone = not any(row["oid"] == original["oid"] for row in final)
                if primary["oid"] != original["oid"] and original_gone:
                    report["physical_index_reclaimed_bytes"] = original["bytes"] - primary["bytes"]
                    report["status"] = "REBUILT" if not errors and outcome == "FINISHED" else "REBUILT_AFTER_CANCELLATION"
                else:
                    report["status"] = outcome if outcome != "FINISHED" else "PAUSED_BUILD_FAILED" if errors else "PAUSED_RESULT_UNVERIFIED"
                if errors:
                    report["build_sqlstate"] = errors[0]
                if report["remaining_reindex_artifacts"]:
                    report["status"] = "PAUSED_UNOWNED_ARTIFACT_MANUAL_REVIEW"
                report["phase"] = "COMPLETE"
            except StoragePause as error:
                report["status"] = str(error)
            except Exception as error:
                report["status"] = ("PAUSED_QUERY_BUDGET" if getattr(error, "sqlstate", None) == "57014"
                                    else "PAUSED_ERROR_MANUAL_REVIEW")
            finally:
                if locked:
                    try:
                        admin.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                    except Exception:
                        report["lock_release"] = "CONNECTION_CLOSE_RELEASES_LOCK"
                if "paths" in locals():
                    report["disk_after"] = check_storage(paths)[1]
    except Exception:
        report["status"] = ("PAUSED_BUILD_STATE_UNKNOWN_MANUAL_REVIEW" if report["reindex_started"]
                            else "FAILED_CONNECTION_OR_CONFIGURATION")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        source = read_env(SECRET_DIR / "formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        report = run(url, apply=args.apply, max_seconds=args.max_seconds)
    except Exception:
        report = {"status": "FAILED_CONFIGURATION", "physical_index_reclaimed_bytes": None}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"DRY_RUN", "REBUILT", "REBUILT_AFTER_CANCELLATION"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
