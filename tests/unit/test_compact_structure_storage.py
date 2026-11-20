"""No production DB: exercise maintenance disk checks, transactions and evidence."""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import compact_structure_storage as storage
from tests.unit.test_structure_archive_codec import record


class FakeDatabase:
    """Transactional state deliberately rolls writes back if verification fails."""
    def __init__(self, data, count=0):
        self.data, self.address = str(data), "127.0.0.1"
        self.events, self.stages, self.progress, self.audit = {}, {}, {}, []
        self.calls, self.transactions = [], []
        self.read_only = None
        self.locked, self.fail_key, self.fail_audit = True, None, False
        self.corrupt_persisted, self.corrupt_reference, self.on_write = False, False, None
        self.rollbacks = 0
        for index in range(1, count+1):
            self.events[("context", f"candidate-{index:03}", 1)] = [record(), None, 2000]

    @contextmanager
    def transaction(self):
        previous = copy.deepcopy((self.events, self.stages, self.progress, self.audit))
        self.read_only = False
        try:
            yield self
        except Exception:
            self.events, self.stages, self.progress, self.audit = previous
            self.rollbacks += 1
            raise
        finally:
            self.transactions.append(self.read_only)
            self.read_only = None

    def _write(self):
        assert self.read_only is False, "write attempted inside a read-only transaction"

    def _envelope(self, value):
        header, body, _ = value
        return copy.deepcopy(header) if body is None else {**copy.deepcopy(header), "_archive_data": base64.b64encode(body).decode()}

    def execute(self, sql, args=()):
        self.calls.append((sql, args))
        rows, rowcount = [], 0
        if sql == "SET TRANSACTION READ ONLY":
            self.read_only = True
        elif sql.startswith("SET LOCAL"):
            pass
        elif "current_setting('data_directory')" in sql:
            rows = [(self.data, self.address)]
        elif "SELECT cursor_json" in sql:
            if args[0] in self.progress:
                rows = [(copy.deepcopy(self.progress[args[0]]),)]
        elif "pg_try_advisory_xact_lock" in sql:
            rows = [(self.locked,)]
        elif "SELECT context_id,candidate_id,sequence,event_json" in sql:
            after = tuple(args[:3]) if len(args) > 1 else None
            rows = [(*key, copy.deepcopy(value[0]), value[2]) for key, value in sorted(self.events.items())
                    if value[1] is None and (after is None or key > after)][:args[-1]]
        elif "SELECT context_id,candidate_id,entry_phase,forecast_json" in sql:
            after = tuple(args[:3]) if len(args) > 1 else None
            rows = [(*key, copy.deepcopy(value), 2000) for key, value in sorted(self.stages.items())
                    if "_evidence" not in value and (after is None or key > after)][:args[-1]]
        elif "SELECT pg_column_size" in sql:
            rows = [(500 if "octet_length" in sql else 100,)]
        elif "repack_structure_event" in sql:
            self._write()
            key, original, header, body = tuple(args[:3]), args[3].obj, args[4].obj, args[5]
            if key in self.events and self.events[key][0] == original and self.events[key][1] is None:
                self.events[key] = [copy.deepcopy(header), body, 500]
                if self.on_write:
                    self.on_write()
                if key == self.fail_key:
                    raise RuntimeError("sensitive-database-details")
                rows = [(True,)]
            else:
                rows = [(False,)]
        elif "SELECT sequence,gatescope_security.structure_event_record" in sql:
            keys = sorted(key for key in self.events if key[:2] == tuple(args))
            rows = [(keys[0][2], self._envelope(self.events[keys[0]]))] if keys else []
        elif "SELECT gatescope_security.structure_event_record" in sql:
            key = tuple(args)
            if key in self.events:
                value = self._envelope(self.events[key])
                if self.corrupt_persisted and "_archive" in value:
                    value["_archive"]["sha256"] = "0"*64
                if self.corrupt_reference and "_archive" not in value:
                    value["first"]["forecast"]["target"]["price"] += 1
                rows = [(value,)]
        elif sql.lstrip().startswith("UPDATE market_structure_stages"):
            self._write()
            key = tuple(args[1:4])
            if self.stages.get(key) == args[4].obj:
                self.stages[key] = copy.deepcopy(args[0].obj)
                rowcount = 1
        elif "SELECT forecast_json" in sql:
            rows = [(copy.deepcopy(self.stages[tuple(args)]),)] if tuple(args) in self.stages else []
        elif "INSERT INTO gatescope_security.structure_storage_audit" in sql:
            self._write()
            self.audit.append(args)
            if self.fail_audit:
                raise RuntimeError("sensitive-audit-details")
        elif "INSERT INTO gatescope_security.structure_storage_progress" in sql:
            self._write()
            self.progress[args[0]] = copy.deepcopy(args[1].obj)
        else:
            raise AssertionError("Unexpected SQL in test: "+sql)
        return SimpleNamespace(fetchone=lambda: rows[0] if rows else None, fetchall=lambda: rows, rowcount=rowcount)


@pytest.fixture
def disks(tmp_path, monkeypatch):
    app, data, wal = (tmp_path/name for name in ("application", "database", "wal-volume"))
    for path in (app, data, wal):
        path.mkdir()
    (data/"pg_wal").symlink_to(wal, target_is_directory=True)
    free = {path: 4*1024**3 for path in (app, data, wal)}
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[Path(path)], total=70*1024**3))
    return app, data, wal, free


def test_discovers_actual_pg_directory_and_follows_independent_wal_symlink(disks):
    app, data, wal, _ = disks
    db = FakeDatabase(data)
    result = storage.run(db, root=app)
    assert result["status"] == "COMPLETE"
    paths = {item["scope"]: item["path"] for item in result["disk_before"]}
    assert paths == {"APPLICATION": str(app), "POSTGRES_DATA": str(data), "POSTGRES_WAL": str(wal)}
    assert result["physical_reclaimed_bytes"] is None
    assert result["minimum_free_bytes"] == 2*1024**3
    assert all(db.transactions)


@pytest.mark.parametrize("scope", [0, 1, 2])
@pytest.mark.parametrize("apply", [False, True])
def test_any_filesystem_below_floor_stops_before_reading_progress_or_evidence(disks, scope, apply):
    app, data, wal, free = disks
    free[(app, data, wal)[scope]] = storage.MIN_FREE_BYTES-1
    db = FakeDatabase(data, 2)
    result = storage.run(db, root=app, apply=apply)
    assert result["status"] == "PAUSED_LOW_DISK"
    assert result["scanned"] == result["packed"] == 0 and not db.progress and not db.audit
    assert not any("market_pattern_events" in sql or "cursor_json" in sql for sql, _ in db.calls)


def test_unknown_wal_filesystem_fails_closed_instead_of_assuming_app_disk(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data/"not-present")
    result = storage.run(db, root=app, apply=True)
    assert result["status"] == "PAUSED_STORAGE_UNVERIFIED" and result["scanned"] == 0
    assert not db.progress


def test_exact_floor_remains_non_overridable_and_is_measured_in_bytes(disks):
    app, data, _, free = disks
    free.update(dict.fromkeys(free, storage.MIN_FREE_BYTES))
    result = storage.run(FakeDatabase(data), root=app)
    assert result["status"] == "COMPLETE"
    assert all(item["free_bytes"] == 2*1024**3 for item in result["disk_before"])


@pytest.mark.parametrize("address", ["203.0.113.7", "10.0.0.5", "not-an-address", "10.0.0.5/32",
    "::ffff:10.0.0.5", "::ffff:203.0.113.7/128", "::/128", "2001:db8::1/128"])
def test_remote_server_paths_are_not_measured_on_wrong_host(disks, address):
    app, data, _, _ = disks
    db = FakeDatabase(data)
    db.address = address
    result = storage.run(db, root=app)
    assert result["status"] == "PAUSED_DATABASE_HOST_UNVERIFIED"
    assert not result["disk_before"]


@pytest.mark.parametrize("address", [None, "127.0.0.1", "127.0.0.1/32", "::1", "::1/128",
    "::ffff:127.0.0.1", "::ffff:127.0.0.1/128"])
def test_local_socket_and_loopback_supported(disks, address):
    app, data, _, _ = disks
    db = FakeDatabase(data)
    db.address = address
    assert storage.run(db, root=app)["status"] == "COMPLETE"


def test_pg_loopback_mask_reaches_disk_guard_instead_of_false_remote_refusal(disks):
    app, data, _, free = disks
    free.update(dict.fromkeys(free, int(1.63*1024**3)))
    db = FakeDatabase(data)
    db.address = "127.0.0.1/32"
    result = storage.run(db, root=app)
    assert result["status"] == "PAUSED_LOW_DISK" and result["scanned"] == result["packed"] == 0
    assert len(result["disk_before"]) == 3
    assert not db.progress and not db.audit
    assert any("host(inet_server_addr())" in sql for sql, _ in db.calls)


def test_dry_run_is_database_read_only_and_does_not_advance_stored_progress(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 5)
    start = ["context", "candidate-001", 1]
    db.progress["structure-storage-1:events"] = start.copy()
    result = storage.run(db, root=app, max_rows=3, batch_rows=2)
    assert result["scanned"] == result["packed"] == 3
    assert result["logical_estimated_saved_bytes"] == result["estimated_saved_bytes"] > 0
    assert result["last_committed_cursor"] == start and result["scan_cursor"][1] == "candidate-004"
    assert result["committed_batches"] == 0 and db.progress["structure-storage-1:events"] == start
    assert all(value[1] is None for value in db.events.values()) and not db.audit
    assert all(db.transactions)
    assert not any("pg_try_advisory" in sql or sql.lstrip().startswith(("UPDATE", "INSERT", "DELETE")) for sql, _ in db.calls)


def test_multiple_batches_resume_and_skip_no_benefit_without_stalling(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 5)
    db.events[("context", "candidate-002", 1)][0] = {"id": "tiny"}
    first = storage.run(db, root=app, apply=True, max_rows=3, batch_rows=2)
    assert (first["scanned"], first["packed"], first["committed_batches"]) == (3, 2, 2)
    assert first["last_committed_cursor"] == ["context", "candidate-003", 1]
    second = storage.run(db, root=app, apply=True, max_rows=10, batch_rows=2)
    assert second["status"] == "COMPLETE" and second["scanned"] == second["packed"] == 2
    assert second["last_committed_cursor"][1] == "candidate-005"
    third = storage.run(db, root=app, apply=True)
    assert third["status"] == "COMPLETE" and third["scanned"] == 0
    assert db.events[("context", "candidate-002", 1)][1] is None
    assert sum(batch[1] for batch in db.audit) == 5


def test_failed_second_batch_rolls_back_rows_audit_and_cursor_then_resumes(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 5)
    db.fail_key = ("context", "candidate-004", 1)
    result = storage.run(db, root=app, apply=True, max_rows=5, batch_rows=2)
    assert result["status"] == "FAILED_BATCH" and result["rolled_back_batch"]
    assert (result["scanned"], result["packed"], result["committed_batches"]) == (2, 2, 1)
    assert result["last_committed_cursor"] == ["context", "candidate-002", 1]
    assert db.events[("context", "candidate-003", 1)][1] is None
    assert db.events[("context", "candidate-004", 1)][1] is None
    assert len(db.audit) == 1 and db.rollbacks == 1
    assert "sensitive" not in str(result)
    db.fail_key = None
    resumed = storage.run(db, root=app, apply=True, max_rows=10, batch_rows=2)
    assert resumed["status"] == "COMPLETE" and resumed["scanned"] == 3


def test_all_no_benefit_rows_advance_cursor_without_mutating_evidence(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 5)
    for value in db.events.values():
        value[0] = {"id": "tiny"}
    original = copy.deepcopy(db.events)
    result = storage.run(db, root=app, apply=True, max_rows=10, batch_rows=2)
    assert result["status"] == "COMPLETE" and result["scanned"] == 5 and result["packed"] == 0
    assert result["logical_estimated_saved_bytes"] == 0 and result["physical_reclaimed_bytes"] is None
    assert result["last_committed_cursor"][1] == "candidate-005" and db.events == original


def test_low_disk_mid_batch_rolls_back_before_committing_progress(disks):
    app, data, wal, free = disks
    db = FakeDatabase(data, 2)
    db.on_write = lambda: free.update({wal: storage.MIN_FREE_BYTES-1})
    result = storage.run(db, root=app, apply=True, batch_rows=2)
    assert result["status"] == "PAUSED_LOW_DISK" and result["rolled_back_batch"]
    assert result["scanned"] == result["packed"] == 0 and db.rollbacks == 1
    assert not db.progress and not db.audit and all(value[1] is None for value in db.events.values())


@pytest.mark.parametrize("kind", ["persisted-evidence", "audit"])
def test_integrity_or_audit_failure_never_leaves_partial_mutation(disks, kind):
    app, data, _, _ = disks
    db = FakeDatabase(data, 1)
    db.corrupt_persisted = kind == "persisted-evidence"
    db.fail_audit = kind == "audit"
    result = storage.run(db, root=app, apply=True)
    assert result["status"] == "FAILED_BATCH" and result["scanned"] == 0
    assert not db.progress and not db.audit and db.events[("context", "candidate-001", 1)][1] is None


def test_busy_maintenance_lock_does_not_advance_cursor(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 1)
    db.locked = False
    result = storage.run(db, root=app, apply=True)
    assert result["status"] == "BUSY" and result["scanned"] == 0 and not db.progress


@pytest.mark.parametrize("apply", [False, True])
def test_stage_compaction_restores_exact_original_before_and_after_persistence(disks, apply):
    app, data, _, _ = disks
    db = FakeDatabase(data, 1)
    key = ("context", "candidate-001", "FORMING")
    original = record()["first"]["forecast"]
    db.stages[key] = copy.deepcopy(original)
    result = storage.run(db, root=app, apply=apply, mode="stages", max_rows=1)
    assert result["scanned"] == result["packed"] == 1
    if apply:
        summary = db.stages[key]
        assert summary["target"]["price"] == original["target"]["price"] and "_evidence" in summary
        assert storage.restore_forecast(db, "context", "candidate-001", summary) == original
        assert db.progress["structure-storage-1:stages"] == list(key)
    else:
        assert db.stages[key] == original and not db.progress
    exact_reads = [sql for sql, _ in db.calls if "SELECT gatescope_security.structure_event_record" in sql]
    assert len(exact_reads) >= (2 if apply else 1)


def test_mismatching_original_forecast_is_skipped_without_data_loss(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 1)
    key = ("context", "candidate-001", "FORMING")
    original = record()["first"]["forecast"]
    original["target"]["price"] += 10
    db.stages[key] = copy.deepcopy(original)
    result = storage.run(db, root=app, apply=True, mode="stages", max_rows=1)
    assert result["scanned"] == 1 and result["packed"] == 0
    assert db.stages[key] == original


def test_corrupt_forecast_reference_blocks_and_rolls_back_stage_batch(disks):
    app, data, _, _ = disks
    db = FakeDatabase(data, 1)
    key = ("context", "candidate-001", "FORMING")
    db.stages[key] = copy.deepcopy(record()["first"]["forecast"])
    db.corrupt_reference = True
    result = storage.run(db, root=app, apply=True, mode="stages", max_rows=1)
    assert result["status"] == "FAILED_BATCH" and result["scanned"] == 0
    assert "_evidence" not in db.stages[key] and not db.progress


@pytest.mark.parametrize("kwargs", [dict(max_rows=0), dict(max_rows=10001), dict(max_rows=True),
    dict(seconds=0), dict(seconds=51), dict(batch_rows=0), dict(batch_rows=33), dict(mode="delete")])
def test_invalid_budgets_rejected_before_database_access(disks, kwargs):
    app, data, _, _ = disks
    db = FakeDatabase(data)
    with pytest.raises(ValueError, match="INVALID_STORAGE_BUDGET"):
        storage.run(db, root=app, **kwargs)
    assert not db.calls
