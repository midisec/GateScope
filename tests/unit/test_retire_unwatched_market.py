import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest
from psycopg.pq import TransactionStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import retire_unwatched_market as retire

INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
VERSION = "structure-study-3.2"


def tracking_state():
    ts = int(NOW.timestamp()) - 120
    shape = {"id": "a" * 64, "pattern": "double_bottom", "name": "双底", "confidence": .75,
        "points": [{"time": ts - 120, "price": 99}], "lines": [],
        "forecast": {"status": "PENDING", "target": {"price": 110}}}
    record = {"id": "a" * 64, "pattern": "double_bottom", "phase": "FORMING", "sequence": 1,
        "source_mode": "RECORDED", "observed_time": ts, "first_observed_time": ts,
        "snapshot_at": NOW.isoformat(), "deadline_time": ts + 3600,
        "first": copy.deepcopy(shape), "latest": shape,
        "stages": {"FORMING": {"score_bucket": "HIGH", "time": ts, "sampled": True,
            "prediction": {"probability": .6}}}}
    return {"last_ts": ts, "last_index": 10, "step_seconds": 60, "active": [record["id"]],
        "records": {record["id"]: record}}


class FakeDatabase:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.identity = ("BTC_USDT", "FUTURES_USDT")
        self.role = True
        self.busy = None
        self.blocker = None
        self.late_blocker = False
        self.checks = 0
        self.calls = []
        self.counts = {table: 1 for table in retire.HOT_TABLES}
        self.rows = [("1m", VERSION, tracking_state())]
        self.events = [(1, "FORMING")]
        self.journals = []
        self.rollbacks = 0
        self.checkpoint_bytes = 2000

    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.counts, self.rows, self.events, self.journals))
        try:
            yield self
        except Exception:
            self.counts, self.rows, self.events, self.journals = before
            self.rollbacks += 1
            raise

    def execute(self, query, args=()):
        self.calls.append((query, args))
        rows, count = [], 0
        if query.startswith(("SET LOCAL", "SELECT set_config", "LOCK TABLE")):
            pass
        elif "rolsuper OR rolbypassrls" in query:
            rows = [(self.role,)]
        elif "pg_try_advisory_xact_lock" in query:
            rows = [(args[0] != self.busy,)]
        elif "SELECT symbol,market" in query:
            assert args == (INSTRUMENT,)
            rows = [self.identity]
        elif query.startswith("SELECT r.id FROM alert_rules"):
            assert args[0] == str(INSTRUMENT) and "FOR UPDATE OF r NOWAIT" in query
        elif query.startswith("SELECT instrument_id,timeframe FROM market_sync_state"):
            assert args[0] == INSTRUMENT and "FOR UPDATE NOWAIT" in query
            rows = [(INSTRUMENT, "1m")]
        elif "FROM market_structure_returns" in query:
            rows = [(False,)]
        elif query.startswith("SELECT EXISTS("):
            assert len(args) == 1 and str(args[0]) == str(INSTRUMENT)
            reason = next(code for code, sql in retire.DEPENDENCIES if sql in query)
            if reason == "WATCHED":
                self.checks += 1
            rows = [(reason == self.blocker or (self.late_blocker and self.checks > 1),)]
        elif "octet_length(checkpoint_json::text)" in query:
            rows = [(r[0], r[1], self.checkpoint_bytes) for r in self.rows]
        elif query.startswith("SELECT count(*)"):
            table = next(table for table in retire.HOT_TABLES if f"FROM {table} " in query)
            rows = [(self.counts[table],)]
        elif "SELECT timeframe,from_time,to_time FROM market_history_deferred" in query:
            rows = [("1m", i * 60, i * 60) for i in range(min(args[1], self.counts["market_history_deferred"]))]
        elif "SELECT timeframe,version,state,cursor" in query:
            rows = [("1m", VERSION, "RUNNING", 100, 1000, NOW)]
        elif "SELECT timeframe,version,checkpoint_json" in query:
            rows = copy.deepcopy(self.rows)
        elif "SELECT sequence,phase" in query:
            rows = [self.events[-1]]
        elif query.startswith("DELETE FROM"):
            table = next(table for table in retire.HOT_TABLES if query.startswith(f"DELETE FROM {table} "))
            assert args[0] == INSTRUMENT
            count = len(args[1].obj) if table == "market_history_deferred" else self.counts[table]
            self.counts[table] -= count
            if table == "market_pattern_tracking":
                self.rows = []
        else:
            raise AssertionError(query)
        return SimpleNamespace(fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows, rowcount=count)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(retire, "storage_paths", lambda *args: {"APPLICATION": Path("/safe")})
    monkeypatch.setattr(retire, "capacity", lambda _: None)
    def journal(conn, instrument, timeframe, context, rows, source, *, version):
        assert instrument == INSTRUMENT and source == "RECORDED"
        conn.connection.journals.extend(copy.deepcopy(rows))
        conn.connection.events.extend((r["sequence"], r["phase"]) for r in rows)
    monkeypatch.setattr(retire, "write_journal", journal)
    monkeypatch.setattr(retire.annual, "project_journal", lambda *args: None)


def run(conn, **kwargs):
    return retire.run(conn, instrument_id=INSTRUMENT, expected_symbol="BTC_USDT",
        expected_market="FUTURES_USDT", now=NOW, **kwargs)


def test_default_dry_run_reports_only_counts_and_work_list_without_mutation(environment):
    conn = FakeDatabase()
    result = run(conn)
    assert result["status"] == "DRY_RUN" and result["censored_candidates"] == 1
    assert result["cancelled_studies"][0]["previous_state"] == "RUNNING"
    assert all(value == 1 for value in conn.counts.values()) and not conn.journals
    assert not any(sql.startswith(("DELETE", "UPDATE", "INSERT")) for sql, _ in conn.calls)
    assert "checkpoint_json" not in repr(result) and "forecast" not in repr(result)


def test_apply_archives_censored_before_target_only_hot_deletion_and_audits(environment):
    conn, audit = FakeDatabase(), []
    original = copy.deepcopy(conn.rows[0][2])
    result = run(conn, apply=True, audit=lambda value: audit.append(copy.deepcopy(value)))
    assert result["status"] == "COMMITTED" and [r["status"] for r in audit] == ["PLAN", "COMMITTED"]
    record = conn.journals[0]["record"]
    assert record["phase"] == "CENSORED" and record["reason"] == "MONITORING_STOPPED"
    assert record["first"] == original["records"]["a" * 64]["first"]
    assert record["observed_time"] == original["last_ts"] + 60
    assert record["sequence"] == 2 and record["latest"]["confirmed"] is False
    assert record["stages"] == original["records"]["a" * 64]["stages"]
    assert all(value == 0 for value in conn.counts.values())
    deletes = [sql.split()[2] for sql, _ in conn.calls if sql.startswith("DELETE")]
    assert deletes == list(retire.HOT_TABLES)
    assert conn.events[0] == (1, "FORMING")
    sql = [sql for sql, _ in conn.calls]
    assert "LOCK TABLE alert_rules IN SHARE MODE NOWAIT" in sql
    assert any("FOR UPDATE OF r NOWAIT" in item for item in sql)
    assert any("SELECT instrument_id,timeframe FROM market_sync_state" in item and "FOR UPDATE NOWAIT" in item for item in sql)


@pytest.mark.parametrize("blocker", [code for code, _ in retire.DEPENDENCIES])
def test_any_owner_dependency_refuses_before_writes(environment, blocker):
    conn = FakeDatabase()
    conn.blocker = blocker
    with pytest.raises(retire.Refused, match=blocker):
        run(conn, apply=True, audit=lambda _: None)
    assert not conn.journals and all(conn.counts.values())


@pytest.mark.parametrize("lock", retire.LOCKS)
def test_busy_worker_lock_refuses_once_without_retry(environment, lock):
    conn = FakeDatabase()
    conn.busy = lock
    with pytest.raises(retire.Refused, match="WORKER_BUSY"):
        run(conn, apply=True, audit=lambda _: None)
    assert sum(args == (lock,) for _, args in conn.calls) == 1
    assert not conn.journals


def test_identity_role_size_and_evidence_guards(environment):
    for attribute, value, reason in [("identity", ("OTHER", "SPOT"), "INSTRUMENT_IDENTITY_MISMATCH"),
            ("role", False, "MAINTENANCE_ROLE_REQUIRED"), ("checkpoint_bytes", 20 * 1024 ** 2, "CHECKPOINT_BUDGET_EXCEEDED"),
            ("events", [(7, "CONFIRMED")], "RECORDED_EVIDENCE_MISMATCH")]:
        conn = FakeDatabase()
        setattr(conn, attribute, value)
        with pytest.raises(retire.Refused, match=reason):
            run(conn, apply=True, audit=lambda _: None)
        assert not conn.journals
    with pytest.raises(retire.Refused, match="ROW_BUDGET_EXCEEDED"):
        run(FakeDatabase(), max_rows=6)


def test_late_reference_or_disk_floor_and_projection_error_roll_back_all(environment, monkeypatch):
    conn = FakeDatabase()
    conn.late_blocker = True
    with pytest.raises(retire.Refused, match="WATCHED"):
        run(conn, apply=True, audit=lambda _: None)
    assert not conn.journals
    conn = FakeDatabase()
    def failed_projection(*args):
        raise ValueError("private-marker")
    monkeypatch.setattr(retire.annual, "project_journal", failed_projection)
    with pytest.raises(ValueError):
        run(conn, apply=True, audit=lambda _: None)
    assert conn.rollbacks == 1 and not conn.journals and all(conn.counts.values())


def test_no_report_or_nested_transaction_cannot_claim_commit(environment):
    with pytest.raises(retire.Refused, match="DURABLE_REPORT_REQUIRED"):
        run(FakeDatabase(), apply=True)
    conn = FakeDatabase()
    conn.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(retire.Refused, match="OPEN_TRANSACTION_NOT_ALLOWED"):
        run(conn)


def test_report_failure_before_write_aborts_but_after_commit_is_not_false_rollback(environment):
    conn = FakeDatabase()
    def failure(_):
        raise OSError("private-marker")
    with pytest.raises(OSError):
        run(conn, apply=True, audit=failure)
    assert not conn.journals
    def after(value):
        if value["status"] == "COMMITTED":
            raise OSError("private-marker")
    result = run(conn, apply=True, audit=after)
    assert result["status"] == "COMMITTED" and result["audit_status"] == "COMMITTED_FINAL_REPORT_WRITE_FAILED"
    assert "private-marker" not in repr(result)


def test_capacity_floor_and_inaccessible_paths_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(retire.shutil, "disk_usage", lambda _: SimpleNamespace(free=retire.MIN_FREE_BYTES - 1))
    with pytest.raises(retire.Refused, match="LOW_DISK"):
        retire.capacity({"APP": tmp_path})
    with pytest.raises(retire.Refused, match="STORAGE_UNVERIFIED"):
        retire.capacity({"APP": tmp_path / "missing"})


def test_audit_file_is_exclusive_0600_and_does_not_follow_existing_symlink(tmp_path):
    path = tmp_path / "retire.jsonl"
    stream, append = retire.open_audit(path)
    try:
        append({"status": "PLAN"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        stream.close()
    with pytest.raises(FileExistsError):
        retire.open_audit(path)
    linked = tmp_path / "link.jsonl"
    linked.symlink_to(path)
    with pytest.raises(FileExistsError):
        retire.open_audit(linked)


def test_invalid_or_future_tracking_clock_never_fabricates_termination():
    state = tracking_state()
    state["last_ts"] = int(NOW.timestamp())
    with pytest.raises(retire.Refused, match="INVALID_TRACKING_CLOCK"):
        retire.censor_tracking("1m", VERSION, state, now=NOW)


@pytest.mark.parametrize("status", ["PENDING", "CONDITIONAL_UNSCORED", "TOUCHED", "AMBIGUOUS", "NOT_REACHED", "UNAVAILABLE"])
def test_only_unfinished_forecast_outcome_is_censored_and_original_target_is_unchanged(status):
    state = tracking_state()
    record = state["records"]["a" * 64]
    record["forecast_outcome"] = {"status": status, "target_price": 110}
    record["latest"]["forecast_outcome"] = copy.deepcopy(record["forecast_outcome"])
    original = copy.deepcopy(record)
    result = retire.censor_tracking("1m", VERSION, state, now=NOW)[0]["record"]
    if status in {"PENDING", "CONDITIONAL_UNSCORED"}:
        assert result["forecast_outcome"]["status"] == "CENSORED"
        assert result["forecast_outcome"]["reason"] == "MONITORING_STOPPED"
    else:
        assert result["forecast_outcome"] == original["forecast_outcome"]
    assert result["first"] == original["first"] and record == original


def test_unexpected_pending_recorded_returns_are_not_silently_discarded():
    state = tracking_state()
    state["pending_returns"] = [{"status": "PENDING"}]
    with pytest.raises(retire.Refused, match="PENDING_RECORDED_RETURNS"):
        retire.censor_tracking("1m", VERSION, state, now=NOW)


def test_many_deferred_rows_do_not_block_core_retirement_or_expand_batch(environment):
    conn = FakeDatabase()
    conn.counts["market_history_deferred"] = 10001
    result = run(conn, apply=True, audit=lambda _: None)
    assert result["status"] == "COMMITTED" and result["deferred_remaining"] is True
    assert result["hot_rows"]["market_history_deferred"] == 256
    assert conn.counts["market_history_deferred"] == 10001 - 256
    assert all(conn.counts[table] == 0 for table in retire.HOT_TABLES[:-1])
    assert result["censored_candidates"] == 1


def test_each_query_including_adapter_calls_has_remaining_deadline_limit():
    conn = FakeDatabase()
    tick = [17.5]
    limited = retire.DeadlineConnection(conn, 20, lambda: tick[0])
    limited.execute("SET LOCAL row_security=off")
    assert conn.calls[0] == ("SELECT set_config('statement_timeout',%s,true)", ("2500ms",))
    tick[0] = 19.9
    limited.execute("SET LOCAL row_security=off")
    assert 1 <= int(conn.calls[-2][1][0][:-2]) <= 100
    tick[0] = 20.1
    before = len(conn.calls)
    with pytest.raises(retire.Refused, match="TIME_BUDGET_EXCEEDED"):
        limited.execute("SET LOCAL row_security=off")
    assert len(conn.calls) == before
