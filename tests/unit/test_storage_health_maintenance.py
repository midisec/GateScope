"""Pure/fake-connection checks; never connects to any database or external app."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import storage_health_maintenance as maintenance


class FakeDatabase:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.calls, self.transactions = [], []
        self.readonly = None
        self.superuser, self.owned = False, True
        self.busy, self.alter_failure, self.changed_policy = None, None, False
        self.kind, self.relations_reads = "r", 0
        self.settings = dict(zip(maintenance.SETTINGS,
            ("on", "on", "50", "0.2", "-1", "2", "200", "0"), strict=True))
        self.options = {name: {} for name in maintenance.TABLES}
        self.spaces = []
        self.history_state = "PAUSED_DISABLED"
        self.history_age = 0
        self.owner_checks = 0

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.options)
        self.readonly = False
        try:
            yield self
        except Exception:
            self.options = before
            raise
        finally:
            self.transactions.append(self.readonly)
            self.readonly = None

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        rows = []
        if sql == "SET TRANSACTION READ ONLY":
            self.readonly = True
        elif sql.startswith("SET LOCAL"):
            pass
        elif "SELECT name,setting FROM pg_settings" in sql:
            rows = list(self.settings.items())
        elif sql == maintenance.RELATION_SQL:
            self.relations_reads += 1
            if self.changed_policy and self.relations_reads == 2:
                self.options[maintenance.TABLES[0]]["autovacuum_vacuum_threshold"] = "25"
            for index, name in enumerate(sorted(maintenance.TABLES)):
                current = self.options[name]
                parent = [key + "=" + value for key, value in current.items() if not key.startswith("toast.")]
                toast = [key[6:] + "=" + value for key, value in current.items() if key.startswith("toast.")]
                rows.append((index + 100, name, self.kind, self.owned, parent, 4_000_000.,
                    4_000_000_000, 3_000_000_000, 2_000_000_000, 9_000_000_000,
                    50_000, 400_000, None, None, toast))
        elif sql.startswith("SELECT pg_database_size"):
            rows = [(27_000_000_000, datetime(2026, 9, 19, tzinfo=timezone.utc))]
        elif "FROM worker_heartbeats" in sql:
            rows = [(workload, datetime.now(timezone.utc) - timedelta(seconds=self.history_age), self.history_state)
                    for workload in maintenance.HISTORY_WORKLOADS]
        elif "FROM pg_tablespace" in sql:
            rows = [(str(path),) for path in self.spaces]
        elif "SELECT rolsuper FROM pg_roles" in sql:
            rows = [(self.superuser,)]
            self.owner_checks += 1
        elif sql.startswith("LOCK TABLE public."):
            assert self.readonly is False
            if self.busy and f"public.{self.busy} " in sql:
                raise RuntimeError("private SQL and credential detail")
        elif sql.startswith("ALTER TABLE public."):
            assert self.readonly is False
            table = sql.split()[2][7:]
            assert table in maintenance.TABLES
            if table == self.alter_failure:
                raise RuntimeError("secret database error")
            for setting in sql.split(" SET (", 1)[1][:-1].split(","):
                key, value = setting.split("=", 1)
                assert key in maintenance.profile(table)
                self.options[table][key] = value
        else:
            raise AssertionError(sql)
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


@pytest.fixture
def disk(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "storage_paths", lambda *_: {"APPLICATION": tmp_path,
        "POSTGRES_DATA": tmp_path, "POSTGRES_WAL": tmp_path})
    probe = SimpleNamespace(free=6 * maintenance.GIB, total=70 * maintenance.GIB, used=64 * maintenance.GIB)
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda _: probe)
    return probe


def test_default_inspect_is_readonly_metadata_without_business_scan(disk):
    conn = FakeDatabase()
    result = maintenance.run(conn)
    assert result["mode"] == "INSPECT" and result["action_status"] == "DRY_RUN"
    assert conn.transactions == [True]
    assert result["physical_reclaimed_bytes"] is None
    assert result["alerts"] == ["AUTOVACUUM_PROFILE_PENDING"]
    assert {row["table"] for row in result["tables"]} == set(maintenance.TABLES)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "VACUUM", "LOCK")) for sql, _ in conn.calls)
    assert not any("FROM market_pattern_events" in sql or "FROM market_structure_stages" in sql for sql, _ in conn.calls)
    event = next(row for row in result["tables"] if row["table"] == "market_pattern_events")
    assert event["current_vacuum_trigger_estimate"] == 800050
    assert event["proposed_vacuum_trigger_estimate"] == 9000
    assert event["live_estimate"] == 50000  # Not substituted for planner row count.


def test_apply_only_fixed_reloptions_and_is_idempotent(disk):
    conn = FakeDatabase()
    result = maintenance.run(conn, apply=True)
    assert result["action_status"] == "APPLIED" and len(result["applied_tables"]) == 3
    sql = [query for query, _ in conn.calls]
    assert sum(query.startswith("ALTER TABLE") for query in sql) == 3
    assert sum(query.startswith("LOCK TABLE") for query in sql) == 3
    assert max(i for i, query in enumerate(sql) if query.startswith("LOCK TABLE")) < min(i for i, query in enumerate(sql) if query.startswith("ALTER TABLE"))
    for options in conn.options.values():
        assert options["vacuum_truncate"] == options["toast.vacuum_truncate"] == "false"
        assert options["autovacuum_vacuum_cost_limit"] == "50"
        assert options["autovacuum_vacuum_cost_delay"] == "20"
    conn.calls.clear()
    again = maintenance.run(conn, apply=True)
    assert again["action_status"] == "ALREADY_CONFIGURED"
    assert not any(query.startswith(("ALTER", "LOCK")) for query, _ in conn.calls)


@pytest.mark.parametrize("free", [0, maintenance.GIB, 2 * maintenance.GIB - 1])
def test_low_disk_reports_but_never_applies(disk, free):
    disk.free = free
    conn = FakeDatabase()
    assert "CRITICAL_LOW_DISK" in maintenance.run(conn)["alerts"]
    with pytest.raises(maintenance.Refused, match="APPLY_REQUIRES_2_GIB_FREE"):
        maintenance.run(conn, apply=True)
    assert not any(query.startswith(("ALTER", "LOCK")) for query, _ in conn.calls)


def test_exact_apply_floor_is_allowed_but_still_warns(disk):
    disk.free = 2 * maintenance.GIB
    result = maintenance.run(FakeDatabase(), apply=True)
    assert result["action_status"] == "APPLIED"
    assert "CRITICAL_LOW_DISK" in result["alerts"]


@pytest.mark.parametrize("setting", ["autovacuum", "track_counts"])
def test_global_disable_refuses_without_changing_global_configuration(disk, setting):
    conn = FakeDatabase()
    conn.settings[setting] = "off"
    with pytest.raises(maintenance.Refused, match="AUTOVACUUM_GLOBALLY_UNAVAILABLE"):
        maintenance.run(conn, apply=True)
    assert all(not options for options in conn.options.values())


def test_role_and_ordinary_table_guards(disk):
    conn = FakeDatabase()
    conn.owned = False
    with pytest.raises(maintenance.Refused, match="TABLE_OWNER_REQUIRED"):
        maintenance.run(conn, apply=True)
    conn.superuser = True
    conn.kind = "p"
    with pytest.raises(maintenance.Refused, match="ORDINARY_TABLE_REQUIRED"):
        maintenance.run(conn, apply=True)


def test_busy_lock_and_mid_apply_failure_roll_back_all(disk):
    for field in ("busy", "alter_failure"):
        conn = FakeDatabase()
        setattr(conn, field, "market_structure_stages")
        with pytest.raises(RuntimeError):
            maintenance.run(conn, apply=True)
        assert all(not options for options in conn.options.values())
    result = maintenance.safe_failure(RuntimeError("postgresql://private:secret@host"))
    assert result["reason"] == "STORAGE_CHECK_FAILED"
    assert "private" not in str(result) and "secret" not in str(result)


def test_concurrent_option_change_is_not_overwritten(disk):
    conn = FakeDatabase()
    conn.changed_policy = True
    with pytest.raises(maintenance.Refused, match="POLICY_CHANGED_DURING_INSPECTION"):
        maintenance.run(conn, apply=True)
    assert all(not options for options in conn.options.values())


def test_existing_more_frequent_slower_profile_is_not_weakened():
    old = {"autovacuum_vacuum_threshold": "100", "autovacuum_vacuum_scale_factor": "0.0001",
        "autovacuum_vacuum_cost_delay": "40", "autovacuum_vacuum_cost_limit": "10"}
    wanted = maintenance.desired_options("market_pattern_events", old, FakeDatabase().settings)
    assert all(wanted[key] == value for key, value in old.items())
    assert all(wanted["toast." + key] == value for key, value in old.items())
    with pytest.raises(maintenance.Refused, match="TABLE_NOT_ALLOWED"):
        maintenance.profile("users")


def test_external_tablespace_capacity_is_checked_without_exposing_path(disk, tmp_path, monkeypatch):
    conn = FakeDatabase()
    external = tmp_path / "private-tablespace"
    external.mkdir()
    conn.spaces = [external]
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda path:
        SimpleNamespace(free=maintenance.GIB, total=2 * maintenance.GIB, used=maintenance.GIB) if path == external else disk)
    report = maintenance.run(conn)
    assert report["disks"][-1]["scope"] == "TABLESPACE_1"
    assert "private-tablespace" not in str(report)
    with pytest.raises(maintenance.Refused, match="APPLY_REQUIRES_2_GIB_FREE"):
        maintenance.run(conn, apply=True)


def test_unknown_storage_and_nested_transaction_fail_closed(disk, tmp_path, monkeypatch):
    with pytest.raises(maintenance.Refused, match="STORAGE_UNVERIFIED"):
        maintenance.disk_report({"APP": tmp_path / "missing"})
    conn = FakeDatabase()
    conn.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(maintenance.Refused, match="OPEN_TRANSACTION_NOT_ALLOWED"):
        maintenance.run(conn)


def test_each_sql_gets_remaining_deadline_and_late_execution_refuses():
    conn = FakeDatabase()
    values = iter((0, 19.5, 20.1))
    deadline = maintenance.Deadline(conn, 20, lambda: next(values))
    with pytest.raises(maintenance.Refused, match="TIME_BUDGET_EXCEEDED"):
        deadline.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
    assert conn.calls[0][0] == "SET LOCAL statement_timeout='500ms'"


def test_manual_hold_is_not_claimed_for_stale_or_missing_worker_proof(disk):
    conn = FakeDatabase()
    conn.history_state = "PAUSED_LOW_DISK"
    assert "HISTORY_MANUAL_HOLD_NOT_CONFIRMED" in maintenance.run(conn)["alerts"]
    conn.history_state = "PAUSED_DISABLED"
    conn.history_age = 300
    assert "HISTORY_MANUAL_HOLD_NOT_CONFIRMED" in maintenance.run(conn)["alerts"]
    assert not any("UPDATE" in query and not query.startswith("LOCK") for query, _ in conn.calls)


def test_postcommit_check_failure_keeps_truthful_applied_status(disk, monkeypatch):
    conn = FakeDatabase()
    original = maintenance.disk_report
    count = 0
    def probe(paths):
        nonlocal count
        count += 1
        if count == 6:
            raise maintenance.Refused("STORAGE_UNVERIFIED")
        return original(paths)
    monkeypatch.setattr(maintenance, "disk_report", probe)
    result = maintenance.run(conn, apply=True)
    assert result["action_status"] == "APPLIED"
    assert "POSTCOMMIT_STORAGE_CHECK_FAILED" in result["alerts"]
    assert all(options for options in conn.options.values())


def test_timer_never_applies_or_restarts_and_manual_hold_is_explicit():
    root = Path(__file__).resolve().parents[2]
    service = (root / "deploy/systemd/gatescope-storage-health.service").read_text()
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert "--apply" not in exec_start and "--seconds 20" in exec_start
    assert "Restart=" not in service and "OnFailure=" not in service
    assert "MemoryMax=192M" in service and "CPUQuota=20%" in service
    hold = (root / "deploy/systemd/gatescope-history-manual-hold.conf").read_text()
    assert "Environment=GATESCOPE_HISTORY_TASKS_ENABLED=false" in hold
    assert "ExecStart=" not in hold and "Restart=" not in hold
    timer = (root / "deploy/systemd/gatescope-storage-health.timer").read_text()
    assert "OnUnitActiveSec=15min" in timer


def test_cli_never_prints_private_connection_exception(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["storage_health_maintenance.py"])
    def failed_config(_):
        raise RuntimeError("postgresql://private:secret@host cg_private-marker")
    monkeypatch.setattr(maintenance, "read_env", failed_config)
    assert maintenance.main() == 1
    output = capsys.readouterr()
    assert '"reason": "STORAGE_CHECK_FAILED"' in output.out
    assert "private" not in output.out and "secret" not in output.out
    assert output.err == ""
