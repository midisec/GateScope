from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import purge_unwatched_replay as purge

TARGET = "30000000-0000-0000-0000-000000000033"
JOB = "40000000-0000-0000-0000-000000000044"


class FakeConnection:
    def __init__(self, batches=()):
        self.calls = []
        self.batches = iter(batches)
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        result = None
        if "inspect_replay_purge" in sql:
            result = {"contexts": [{"context_id": "a" * 64}], "enumeration_bounded": True}
        if "open_replay_purge" in sql:
            result = {"contexts": 1}
        if "replay_purge_batch" in sql:
            result = next(self.batches)
            if isinstance(result, Exception):
                raise result
        return SimpleNamespace(fetchone=lambda: (result,))


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(purge, "storage_paths", lambda conn, root: {"APPLICATION": root})
    monkeypatch.setattr(purge, "check_storage", lambda paths: ("READY", [{"free_bytes": 3 * 1024**3}]))


def batch(count=1):
    return {"status": "BATCH_COMPLETE", "deleted_rows": count, "counts": {"market_pattern_events": count}}


def test_dry_run_is_readonly_and_creates_no_job_or_cursor(storage):
    conn = FakeConnection()
    result = purge.run(conn, instrument_id=TARGET)
    assert result["status"] == "DRY_RUN"
    assert result["job_id"] is None
    assert result["deleted_rows"] == 0 and result["physical_reclaimed_bytes"] is None
    assert conn.calls[0][0] == "SET TRANSACTION READ ONLY"
    assert any(sql == "SELECT gatescope_security.inspect_replay_purge(%s,1)" for sql, _ in conn.calls)
    assert all(not any(name in sql for name in ("open_replay", "close_replay", "replay_purge_batch", "INSERT", "UPDATE"))
               for sql, _ in conn.calls)


@pytest.mark.parametrize("kwargs", [{"max_rows": 501}, {"max_rows": 0}, {"seconds": 31},
                                  {"seconds": 0}, {"batch_rows": 501}, {"max_rows": True}, {"apply": 1}])
def test_non_overridable_row_and_time_budget(storage, kwargs):
    with pytest.raises(ValueError, match="INVALID_PURGE_BUDGET"):
        purge.run(FakeConnection(), instrument_id=TARGET, **kwargs)


def test_low_disk_does_not_even_open_job(storage, monkeypatch):
    monkeypatch.setattr(purge, "check_storage", lambda paths: ("PAUSED_LOW_DISK", []))
    conn = FakeConnection()
    result = purge.run(conn, instrument_id=TARGET, apply=True)
    assert result["status"] == "PAUSED_LOW_DISK" and not conn.calls


def test_actual_all_storage_scopes_have_one_gib_floor(tmp_path, monkeypatch):
    app = tmp_path / "app"
    wal = tmp_path / "wal"
    app.mkdir()
    wal.mkdir()
    monkeypatch.setattr(purge.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=purge.MIN_FREE_BYTES - (1 if Path(path) == wal else 0), total=100 * 1024**3))
    status, measurements = purge.check_storage({"APPLICATION": app, "POSTGRES_WAL": wal})
    assert status == "PAUSED_LOW_DISK"
    assert len(measurements) == 2
    assert purge.check_storage({"APPLICATION": app})[0] == "READY"
    assert purge.check_storage({"POSTGRES_DATA": tmp_path / "missing"})[0] == "PAUSED_STORAGE_UNVERIFIED"


def test_multiple_committed_batches_and_resume_same_job(storage):
    conn = FakeConnection([batch(2), batch(2), batch(1)])
    result = purge.run(conn, instrument_id=TARGET, apply=True, job_id=JOB, max_rows=5, batch_rows=2)
    assert result["status"] == "BUDGET_REACHED"
    assert result["deleted_rows"] == 5 and result["committed_batches"] == 3
    assert result["counts"] == {"market_pattern_events": 5}
    assert result["job_closed"] is True
    assert [args[1] for sql, args in conn.calls if "replay_purge_batch" in sql] == [2, 2, 1]
    assert any(args == (JOB, TARGET) for sql, args in conn.calls if "open_replay_purge" in sql)
    assert conn.calls[-1][1] == (JOB, "PAUSED")


def test_empty_phases_are_bounded_even_if_database_never_finishes(storage):
    conn = FakeConnection([batch(0)] * 65)
    result = purge.run(conn, instrument_id=TARGET, apply=True)
    assert result["committed_batches"] == 64 and result["deleted_rows"] == 0
    assert result["job_closed"] is True


def test_complete_closes_job_done(storage):
    conn = FakeConnection([{"status": "COMPLETE", "deleted_rows": 0, "counts": {}}])
    result = purge.run(conn, instrument_id=TARGET, apply=True, job_id=JOB)
    assert result["status"] == "COMPLETE" and conn.calls[-1][1] == (JOB, "DONE")


def test_failed_batch_rolls_back_without_counting_or_leaking_sql(storage):
    conn = FakeConnection([batch(2), RuntimeError("SECRET SQL postgresql://secret")])
    result = purge.run(conn, instrument_id=TARGET, apply=True, max_rows=5, batch_rows=2)
    assert result["status"] == "FAILED_SAFE_ROLLBACK"
    assert result["deleted_rows"] == 2 and result["committed_batches"] == 1
    assert conn.rollbacks == 1 and result["job_closed"] is True
    assert "SECRET" not in str(result) and "postgresql" not in str(result)


def test_low_disk_before_commit_rolls_back_latest_batch(storage, monkeypatch):
    # Before open, before manifest commit, before batch, before batch commit.
    results = iter(["READY", "READY", "READY", "PAUSED_LOW_DISK"])
    monkeypatch.setattr(purge, "check_storage", lambda paths: (next(results), []))
    conn = FakeConnection([batch(1)])
    result = purge.run(conn, instrument_id=TARGET, apply=True)
    assert result["status"] == "PAUSED_LOW_DISK"
    assert result["deleted_rows"] == 0 and result["committed_batches"] == 0
    assert conn.rollbacks == 1 and result["job_closed"] is True


def test_unexpected_deleted_table_cannot_be_reported_as_success(storage):
    conn = FakeConnection([{"status": "BATCH_COMPLETE", "deleted_rows": 1, "counts": {"bars": 1}}])
    result = purge.run(conn, instrument_id=TARGET, apply=True)
    assert result["status"] == "FAILED_SAFE_ROLLBACK" and result["deleted_rows"] == 0
    assert conn.rollbacks == 1


def test_unknown_or_remote_database_paths_refuse_without_any_mutation(storage, monkeypatch):
    def refuse(conn, root):
        raise purge.StoragePause("PAUSED_DATABASE_HOST_UNVERIFIED")
    monkeypatch.setattr(purge, "storage_paths", refuse)
    conn = FakeConnection()
    result = purge.run(conn, instrument_id=TARGET, apply=True)
    assert result["status"] == "PAUSED_DATABASE_HOST_UNVERIFIED" and not conn.calls


def test_migration_does_not_bypass_immutability_or_delete_candles():
    source = (Path(__file__).resolve().parents[2] / "migrations/versions/0053_unwatched_replay_purge.py").read_text()
    assert "disable trigger" not in source.lower()
    assert "session_replication_role" not in source
    assert "DELETE FROM bars" not in source and "VACUUM" not in source
    assert "rolsuper OR rolbypassrls" in source
    assert "PURGE_PROTECTED_PARTIAL" in source
    assert "pg_advisory_xact_lock_shared(hashtextextended('replay-instrument:'" in source
    assert "pg_advisory_xact_lock(hashtextextended('replay-instrument:'" in source
