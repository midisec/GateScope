#!/usr/bin/env python3
"""Retire ONE explicitly confirmed, completely unreferenced market's hot state.

Dry-run by default. Never deletes bars, immutable events/reports, research or
trade evidence, inactive sync state, or Coinglass caches. One bounded atomic
transaction, no lock retry and no trigger bypass. This is NOT a permanent
instrument ban: a later explicit subscription/chart request can recreate state.
Run locally on the database host, never through SSH port forwarding. Apply
requires a new 0600 JSONL report file in an existing operator-owned directory.
The first durable entry is a PLAN, not evidence that the transaction committed.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from compact_structure_storage import storage_paths
from prepare_service_envs import ROOT, SECRET_DIR, read_env
from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters.postgres_structure_archive import context_id, write_journal
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.quant.structure_lifecycle import append_transition

MIN_FREE_BYTES = 1024 ** 3
HOT_TABLES = ("market_pattern_tracking", "market_pattern_studies", "market_live_candles",
    "market_chart_snapshots", "market_history_deferred")
DEFERRED_BATCH_ROWS = 256
LOCKS = (7342281, 7342282, 7342283, 7342284)
DEPENDENCIES = (
    ("WATCHED", "SELECT 1 FROM watchlist_items wi JOIN watchlists w ON w.id=wi.watchlist_id WHERE wi.instrument_id=%s AND w.deleted_at IS NULL"),
    ("ACTIVE_ALERT", "SELECT 1 FROM alert_rules r JOIN alert_rule_versions v ON v.alert_rule_id=r.id AND v.tenant_id=r.tenant_id AND v.version=r.current_version WHERE r.status='ACTIVE' AND v.condition_json->>'instrument_id'=%s"),
    ("ACTIVE_MARKET_SYNC", "SELECT 1 FROM market_sync_state WHERE instrument_id=%s AND active"),
    ("PENDING_PAPER_ORDER", "SELECT 1 FROM paper_orders WHERE instrument_id=%s AND status IN ('NEW','OPEN','PARTIALLY_FILLED')"),
    ("OPEN_PAPER_POSITION", "SELECT 1 FROM paper_positions WHERE instrument_id=%s AND status='OPEN'"),
    ("OPEN_LEGACY_PAPER_POSITION", "SELECT 1 FROM trade_state_current WHERE instrument_id=%s AND state='OPEN'"),
    ("PENDING_PAPER_PROPOSAL", "SELECT 1 FROM trade_proposals p JOIN proposal_status_current s ON s.proposal_id=p.id JOIN execution_decisions d ON d.id=p.execution_decision_id JOIN signal_evidence_snapshots e ON e.id=d.signal_evidence_snapshot_id WHERE e.instrument_id=%s AND s.status IN ('AWAITING_CONFIRMATION','AUTHORIZED')"),
    ("ACTIVE_SHADOW_OBSERVATION", "SELECT 1 FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id WHERE f.instrument_id=%s AND s.state IN ('WAITING_DELIVERY','PENDING','OPEN')"),
    ("PENDING_RESEARCH", "SELECT 1 FROM research_backtests WHERE instrument_id=%s AND state IN ('QUEUED','RUNNING')"),
    ("PENDING_PORTFOLIO", "SELECT 1 FROM research_portfolios p JOIN research_backtests r ON r.id=ANY(p.source_run_ids) WHERE r.instrument_id=%s AND p.state IN ('QUEUED','RUNNING')"),
)


class Refused(Exception):
    """Fixed, safe reason only; CLI must not print connection/row exception text."""


class DeadlineConnection:
    """Bound EVERY query, including SQL issued inside journal/project adapters."""
    def __init__(self, connection, deadline, monotonic):
        self.connection, self.deadline, self.monotonic = connection, deadline, monotonic

    def execute(self, query, args=()):
        remaining = self.deadline - self.monotonic()
        if remaining <= .001:
            raise Refused("TIME_BUDGET_EXCEEDED")
        self.connection.execute("SELECT set_config('statement_timeout',%s,true)",
            (f"{max(1, min(5000, int(remaining * 1000)))}ms",))
        return self.connection.execute(query, args)


def capacity(paths):
    try:
        for path in paths.values():
            resolved = Path(path).resolve(strict=True)
            if not resolved.is_dir():
                raise Refused("STORAGE_UNVERIFIED")
            if shutil.disk_usage(resolved).free < MIN_FREE_BYTES:
                raise Refused("LOW_DISK")
    except OSError:
        raise Refused("STORAGE_UNVERIFIED") from None


def assert_unused(conn, instrument):
    for reason, query in DEPENDENCIES:
        argument = str(instrument) if reason == "ACTIVE_ALERT" else instrument
        if conn.execute("SELECT EXISTS(" + query + ")", (argument,)).fetchone()[0]:
            raise Refused(reason)


def censor_tracking(timeframe, version, state, *, now):
    """Pure projection: stop observation at the last actually observed close."""
    if timeframe not in TIMEFRAME_SECONDS or not isinstance(state, dict):
        raise Refused("INVALID_TRACKING_STATE")
    # Live tracking does not normally run advance_returns. Do not discard an
    # unexpected older checkpoint that contains unfinished return observations.
    if state.get("pending_returns"):
        raise Refused("PENDING_RECORDED_RETURNS")
    active = state.get("active", [])
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active) or len(set(active)) != len(active):
        raise Refused("INVALID_TRACKING_STATE")
    if not active:
        return []
    last_ts, index, step = state.get("last_ts"), state.get("last_index"), state.get("step_seconds")
    if (type(last_ts) is not int or type(index) is not int or type(step) is not int
            or index < 0 or step != TIMEFRAME_SECONDS[timeframe]):
        raise Refused("INVALID_TRACKING_CLOCK")
    observed = last_ts + step
    if not 0 < observed <= int(now.timestamp()):
        raise Refused("INVALID_TRACKING_CLOCK")
    records = state.get("records")
    if not isinstance(records, dict):
        raise Refused("INVALID_TRACKING_STATE")
    journal = []
    for candidate in active:
        record = records.get(candidate)
        if (not isinstance(record, dict) or record.get("id") != candidate
                or record.get("phase") not in {"FORMING", "FORMED"}
                or record.get("source_mode", "RECORDED") != "RECORDED"
                or type(record.get("sequence")) is not int or record["sequence"] < 1
                or type(record.get("observed_time")) is not int or record["observed_time"] > observed
                or not isinstance(record.get("stages"), dict) or not record["stages"]):
            raise Refused("INVALID_ACTIVE_RECORD")
        frozen = copy.deepcopy(record)
        outcome = frozen.get("forecast_outcome")
        if isinstance(outcome, dict) and outcome.get("status") in {"PENDING", "CONDITIONAL_UNSCORED"}:
            frozen["forecast_outcome"] = {**outcome, "status": "CENSORED", "observed_time": observed,
                "reason": "MONITORING_STOPPED"}
            frozen["latest"]["forecast_outcome"] = copy.deepcopy(frozen["forecast_outcome"])
        append_transition(frozen, "CENSORED", index, observed, "MONITORING_STOPPED", journal, now.isoformat())
    return journal


def run(conn, *, instrument_id, expected_symbol, expected_market, apply=False, max_rows=2000,
        max_checkpoint_bytes=16 * 1024 ** 2, seconds=20, root=ROOT, audit=None,
        now=None, monotonic=time.monotonic):
    instrument = UUID(str(instrument_id))
    if (not isinstance(expected_symbol, str) or not 1 <= len(expected_symbol) <= 100
            or expected_market not in {"SPOT", "FUTURES_USDT", "FUTURES_BTC"}
            or type(max_rows) is not int or not 1 <= max_rows <= 10000
            or type(max_checkpoint_bytes) is not int or not 1 <= max_checkpoint_bytes <= 64 * 1024 ** 2
            or type(seconds) is not int or not 1 <= seconds <= 30):
        raise Refused("INVALID_ARGUMENTS")
    if apply and audit is None:
        raise Refused("DURABLE_REPORT_REQUIRED")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise Refused("OPEN_TRANSACTION_NOT_ALLOWED")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise Refused("INVALID_CLOCK")
    paths = storage_paths(conn, root)
    capacity(paths)
    start = monotonic()

    def budget():
        if monotonic() - start >= seconds:
            raise Refused("TIME_BUDGET_EXCEEDED")

    with conn.transaction():
        conn = DeadlineConnection(conn, start + seconds, monotonic)
        conn.execute("SET LOCAL lock_timeout='1s'")
        conn.execute("SET LOCAL row_security=off")
        role = conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        if not role or not role[0]:
            raise Refused("MAINTENANCE_ROLE_REQUIRED")
        for lock in LOCKS:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock,)).fetchone()[0]:
                raise Refused("WORKER_BUSY")
        # Alert instruments live in JSON, not a foreign key. A target instrument
        # lock alone cannot prevent a new rule or a paused rule being enabled.
        # SHARE blocks rule mutation only for this bounded transaction; regular
        # rule evaluation SELECTs and unrelated notification writes still run.
        try:
            conn.execute("LOCK TABLE alert_rules IN SHARE MODE NOWAIT")
        except psycopg.errors.LockNotAvailable:
            raise Refused("DEPENDENCY_WRITER_BUSY") from None
        target = conn.execute("SELECT symbol,market FROM instruments WHERE id=%s FOR UPDATE NOWAIT", (instrument,)).fetchone()
        if target != (expected_symbol, expected_market):
            raise Refused("INSTRUMENT_IDENTITY_MISMATCH")
        locked_alerts = conn.execute("""SELECT r.id FROM alert_rules r JOIN alert_rule_versions v
            ON v.alert_rule_id=r.id AND v.tenant_id=r.tenant_id AND v.version=r.current_version
            WHERE v.condition_json->>'instrument_id'=%s ORDER BY r.id LIMIT %s FOR UPDATE OF r NOWAIT""",
            (str(instrument), max_rows + 1)).fetchall()
        locked_sync = conn.execute("""SELECT instrument_id,timeframe FROM market_sync_state
            WHERE instrument_id=%s ORDER BY timeframe LIMIT %s FOR UPDATE NOWAIT""",
            (instrument, max_rows + 1)).fetchall()
        if len(locked_alerts) > max_rows or len(locked_sync) > max_rows:
            raise Refused("DEPENDENCY_LOCK_BUDGET_EXCEEDED")
        assert_unused(conn, instrument)
        sizes = conn.execute("""SELECT timeframe,version,octet_length(checkpoint_json::text)
            FROM market_pattern_tracking WHERE instrument_id=%s ORDER BY timeframe,version
            LIMIT %s FOR UPDATE NOWAIT""", (instrument, max_rows + 1)).fetchall()
        if len(sizes) > max_rows or sum(row[2] for row in sizes) > max_checkpoint_bytes:
            raise Refused("CHECKPOINT_BUDGET_EXCEEDED")
        counts = {}
        for table in HOT_TABLES[:-1]:
            counts[table] = conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {table} WHERE instrument_id=%s LIMIT %s) bounded",
                (instrument, max_rows + 1)).fetchone()[0]
        deferred_rows = conn.execute("""SELECT timeframe,from_time,to_time FROM market_history_deferred
            WHERE instrument_id=%s ORDER BY timeframe,from_time,to_time LIMIT %s FOR UPDATE NOWAIT""",
            (instrument, DEFERRED_BATCH_ROWS + 1)).fetchall()
        deferred_remaining = len(deferred_rows) > DEFERRED_BATCH_ROWS
        deferred_keys = [dict(zip(("timeframe", "from_time", "to_time"), row, strict=True))
            for row in deferred_rows[:DEFERRED_BATCH_ROWS]]
        counts[HOT_TABLES[-1]] = len(deferred_keys)
        if sum(counts.values()) > max_rows:
            raise Refused("ROW_BUDGET_EXCEEDED")
        work = conn.execute("""SELECT timeframe,version,state,cursor,total,requested_at
            FROM market_pattern_studies WHERE instrument_id=%s ORDER BY timeframe FOR UPDATE NOWAIT""", (instrument,)).fetchall()
        tracking = conn.execute("""SELECT timeframe,version,checkpoint_json FROM market_pattern_tracking
            WHERE instrument_id=%s ORDER BY timeframe,version""", (instrument,)).fetchall()
        batches, tracking_summary, writes = [], [], sum(counts.values())
        for timeframe, version, state in tracking:
            budget()
            journal = censor_tracking(timeframe, version, state, now=current)
            context = context_id(instrument, timeframe, version=version)
            if conn.execute("SELECT EXISTS(SELECT 1 FROM market_structure_returns WHERE context_id=%s AND status='PENDING')", (context,)).fetchone()[0]:
                raise Refused("PENDING_RECORDED_RETURNS")
            for item in journal:
                prior = conn.execute("""SELECT sequence,phase FROM market_pattern_events
                    WHERE context_id=%s AND candidate_id=%s ORDER BY sequence DESC LIMIT 1""",
                    (context, item["candidate_id"])).fetchone()
                if prior != (item["sequence"] - 1, item["from_phase"]):
                    raise Refused("RECORDED_EVIDENCE_MISMATCH")
            writes += sum(1 + len(item["record"]["stages"]) for item in journal)
            if writes > max_rows:
                raise Refused("ROW_BUDGET_EXCEEDED")
            batches.append((timeframe, version, context, journal))
            tracking_summary.append({"timeframe": timeframe, "version": version, "censored": len(journal)})
        summary = {"status": "PLAN", "apply": apply, "instrument_id": str(instrument),
            "symbol": expected_symbol, "market": expected_market, "planned_at": current.isoformat(),
            "hot_rows": counts, "censored_candidates": sum(item["censored"] for item in tracking_summary),
            "tracking": tracking_summary, "cancelled_studies": [dict(zip(
                ("timeframe", "version", "previous_state", "cursor", "total", "requested_at"),
                (*row[:5], row[5].isoformat()), strict=True)) for row in work],
            "deferred_batch_limit": DEFERRED_BATCH_ROWS, "deferred_remaining": deferred_remaining,
            "maintenance_lock": "BOUNDED_ALERT_RULE_WRITES_ONLY_OTHER_MARKET_READS_AND_NOTIFICATIONS_CONTINUE",
            "preserves": ["bars", "inactive_sync_state", "recorded_and_replay_evidence", "reports", "trade_and_research_ledgers", "coinglass"],
            "permanent_block": False}
        summary["plan_digest"] = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
        if audit is not None:
            audit(summary)
        if not apply:
            return {**summary, "status": "DRY_RUN"}
        # Recheck immediately before the only write batch, after all expensive
        # reads. No owner/tenant filter: another user's dependency blocks too.
        budget()
        capacity(paths)
        assert_unused(conn, instrument)
        for timeframe, version, context, journal in batches:
            budget()
            write_journal(conn, instrument, timeframe, context, journal, "RECORDED", version=version)
            annual.project_journal(conn, context, journal)
        for table in HOT_TABLES[:-1]:
            budget()
            deleted = conn.execute(f"DELETE FROM {table} WHERE instrument_id=%s", (instrument,)).rowcount
            if deleted != counts[table]:
                raise Refused("CONCURRENT_HOT_STATE_CHANGE")
        if deferred_keys:
            deleted = conn.execute("""DELETE FROM market_history_deferred WHERE instrument_id=%s
                AND (timeframe,from_time,to_time) IN (
                  SELECT timeframe,from_time,to_time FROM jsonb_to_recordset(%s)
                    AS keys(timeframe text,from_time bigint,to_time bigint))""",
                (instrument, Jsonb(deferred_keys))).rowcount
            if deleted != counts["market_history_deferred"]:
                raise Refused("CONCURRENT_HOT_STATE_CHANGE")
        budget()
        capacity(paths)
        assert_unused(conn, instrument)
    # If output fails here, PLAN remains; never claim the committed DB rolled back.
    result = {**summary, "status": "COMMITTED"}
    try:
        audit(result)
    except Exception:
        return {**result, "audit_status": "COMMITTED_FINAL_REPORT_WRITE_FAILED"}
    return result


def open_audit(path):
    path = Path(path)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise Refused("REPORT_PATH_INVALID")
    parent = path.parent.resolve(strict=True)
    info = parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise Refused("REPORT_DIRECTORY_NOT_PROTECTED")
    descriptor = os.open(parent / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(descriptor, "w", encoding="utf-8")
    def append(value):
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return stream, append


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-id", type=UUID, required=True)
    parser.add_argument("--expected-symbol", required=True)
    parser.add_argument("--expected-market", choices=("SPOT", "FUTURES_USDT", "FUTURES_BTC"), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-rows", type=int, default=2000)
    parser.add_argument("--max-checkpoint-bytes", type=int, default=16 * 1024 ** 2)
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    if args.apply and args.report is None:
        raise Refused("DURABLE_REPORT_REQUIRED")
    stream, audit = open_audit(args.report) if args.report else (None, None)
    try:
        source = read_env(SECRET_DIR / "formal-runtime.env")
        url = source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            result = run(conn, instrument_id=args.instrument_id, expected_symbol=args.expected_symbol,
                expected_market=args.expected_market, apply=args.apply, max_rows=args.max_rows,
                max_checkpoint_bytes=args.max_checkpoint_bytes, seconds=args.seconds, audit=audit)
        print(json.dumps(result))
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "REFUSED" if isinstance(error, Refused) else "FAILED",
            "reason": str(error) if isinstance(error, Refused) else type(error).__name__,
            "details": "private database output suppressed"}))
        raise SystemExit(1) from None
