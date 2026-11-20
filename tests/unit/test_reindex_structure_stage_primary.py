from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import reindex_structure_stage_primary as maintenance


def primary(**changes):
    row = dict(oid=10, name=maintenance.INDEX, table_oid=20, definition=maintenance.DEFINITION,
               bytes=1206689792, kind="i", valid=True, ready=True, live=True, primary=True, unique=True,
               replica_identity=False, clustered=False, key_count=3, attribute_count=3,
               keys=[1, 2, 4], opclasses=[100, 100, 100], collations=[100, 100, 100], options=[0, 0, 0],
               predicate=None, expressions=None, other_dependency=False,
               constraints=[{"oid": 30, "name": maintenance.INDEX, "type": "p", "table_oid": 20}])
    row.update(changes)
    return row


def artifact(*, oid=11, suffix="_ccnew", **changes):
    name = maintenance.INDEX + suffix
    row = primary(oid=oid, name=name, definition=maintenance.DEFINITION.replace(maintenance.INDEX, name, 1),
                  valid=False, ready=False, primary=False, constraints=[])
    row.update(changes)
    return row


def candidates(rows, *, original=None, observed=None):
    return maintenance.cleanup_candidates(rows, original=original or primary(), original_oids={10},
                                          owned_new_oids={11} if observed is None else observed)


def test_actual_density_indicates_estimate_not_promised_filesystem_gain():
    result = maintenance.density_decision({"avg_leaf_density": 57.62, "index_size": 1206689792}, 1206689792)
    assert result["eligible"]
    assert 400 * 1024**2 < result["estimated_index_gain_bytes"] < 500 * 1024**2
    assert "NOT_GUARANTEED" in result["estimate_basis"]


@pytest.mark.parametrize("density", [70, 85, 100])
def test_density_at_or_above_seventy_is_not_eligible(density):
    assert not maintenance.density_decision({"avg_leaf_density": density, "index_size": 2**30}, 2**30)["eligible"]


@pytest.mark.parametrize("density", [True, -1, 101, float("nan"), float("inf"), "57.62", None])
def test_invalid_density_refuses(density):
    with pytest.raises(maintenance.StoragePause, match="DENSITY_UNVERIFIED"):
        maintenance.density_decision({"avg_leaf_density": density, "index_size": 2**30}, 2**30)


def test_tiny_index_does_not_justify_maintenance():
    assert not maintenance.density_decision({"avg_leaf_density": 20, "index_size": 8192}, 8192)["eligible"]


def test_size_change_between_snapshot_and_diagnostic_refuses():
    with pytest.raises(maintenance.StoragePause):
        maintenance.density_decision({"avg_leaf_density": 20, "index_size": 2**30}, 2**29)


@pytest.mark.parametrize("change", [{"valid": False}, {"ready": False}, {"primary": False},
                                    {"unique": False}, {"replica_identity": True}, {"predicate": "x"},
                                    {"constraints": []}, {"other_dependency": True}])
def test_missing_or_changed_constraint_cannot_be_used_for_cleanup(change):
    with pytest.raises(maintenance.StoragePause):
        maintenance.valid_primary([primary(**change)])


def test_only_progress_observed_new_oid_can_be_removed():
    original, ours, unrelated = primary(), artifact(), artifact(oid=12, suffix="_ccnew2")
    assert candidates([original, ours, unrelated]) == [ours]
    assert candidates([original, ours], observed=set()) == []


@pytest.mark.parametrize("change", [{"valid": True}, {"primary": True}, {"unique": False},
                                    {"keys": [1, 4, 2]}, {"table_oid": 99}, {"other_dependency": True}])
def test_lookalike_or_still_valid_new_index_never_dropped(change):
    with pytest.raises(maintenance.StoragePause):
        candidates([primary(), artifact(**change)])


def test_preexisting_new_oid_not_owned_by_this_invocation():
    new = artifact()
    assert maintenance.cleanup_candidates([primary(), new], original=primary(), original_oids={10, 11},
                                          owned_new_oids={11}) == []


def test_old_original_oid_only_removed_after_valid_constraint_swap():
    original = primary()
    replacement = primary(oid=11, bytes=750_000_000)
    old = artifact(oid=10, suffix="_ccold", ready=False)
    assert candidates([replacement, old], original=original) == [old]
    with pytest.raises(maintenance.StoragePause):
        candidates([primary(oid=11, valid=False), old], original=original)
    wrong_constraint = deepcopy(replacement)
    wrong_constraint["constraints"][0]["oid"] = 31
    with pytest.raises(maintenance.StoragePause, match="CONSTRAINT_CHANGED"):
        candidates([wrong_constraint, old], original=original)


class Done:
    def __init__(self, values):
        self.values = iter(values)

    def wait(self, timeout):
        assert timeout == .25
        return next(self.values)


@pytest.mark.parametrize("status", ["PAUSED_LOW_DISK", "PAUSED_STORAGE_UNVERIFIED"])
def test_monitor_cancels_only_its_build_on_any_disk_failure(status):
    calls = []
    build = SimpleNamespace(cancel=lambda: calls.append("cancel"))
    result = maintenance.monitor_build(build, Done([False]), {}, deadline=10,
                                      disk_check=lambda paths: (status, []), now=lambda: 1)
    assert result == (status, []) and calls == ["cancel"]


def test_monitor_deadline_is_bounded_and_stops_before_cleanup():
    calls = []
    result = maintenance.monitor_build(SimpleNamespace(cancel=lambda: calls.append("cancel")), Done([False]), {},
                                      deadline=10, disk_check=lambda paths: ("READY", []), now=lambda: 10)
    assert result[0] == "PAUSED_TIME_BUDGET" and calls == ["cancel"]


def test_observation_failure_cancels_instead_of_losing_artifact_ownership():
    calls = []
    def fail():
        raise RuntimeError("unavailable")
    result = maintenance.monitor_build(SimpleNamespace(cancel=lambda: calls.append("cancel")), Done([False]), {},
                                      deadline=10, disk_check=lambda paths: ("READY", []), now=lambda: 1, observe=fail)
    assert result[0] == "PAUSED_PROGRESS_UNVERIFIED" and calls == ["cancel"]


def test_completed_build_is_not_cancelled():
    build = SimpleNamespace(cancel=lambda: pytest.fail("must not cancel"))
    assert maintenance.monitor_build(build, Done([True]), {}, deadline=10,
                                     disk_check=lambda paths: ("READY", []))[0] == "FINISHED"


def test_only_single_index_and_no_destructive_table_commands():
    source = Path(maintenance.__file__).read_text()
    assert "REINDEX INDEX CONCURRENTLY public.market_structure_stages_pkey" in source
    assert "SET temp_file_limit='1GB'" in source
    assert "TransactionStatus.IDLE" in source
    assert "DELETE FROM" not in source and "TRUNCATE " not in source
    assert "CREATE EXTENSION" not in source


def test_external_tablespaces_and_temp_default_are_measured(tmp_path):
    external = tmp_path / "external"
    conn = SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchall=lambda: [
        ("DATABASE_TABLESPACE", str(external)), ("INDEX_TABLESPACE", ""), ("HEAP_TABLESPACE", str(external))]))
    result = maintenance.storage_tablespaces(conn, {"POSTGRES_DATA": tmp_path / "data"})
    assert result["DATABASE_TABLESPACE"] == external
    assert result["HEAP_TABLESPACE"] == external
    assert result["INDEX_TABLESPACE"] == tmp_path / "data"
    assert "SET temp_tablespaces=''" in Path(maintenance.__file__).read_text()


@pytest.mark.parametrize("rows", [[], [("DATABASE_TABLESPACE", "relative")], [
    ("DATABASE_TABLESPACE", "relative"), ("INDEX_TABLESPACE", ""), ("HEAP_TABLESPACE", "")]])
def test_unverified_tablespace_never_silently_checks_only_data_directory(rows, tmp_path):
    conn = SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchall=lambda: rows))
    with pytest.raises(maintenance.StoragePause, match="TABLESPACE_UNVERIFIED"):
        maintenance.storage_tablespaces(conn, {"POSTGRES_DATA": tmp_path})


def test_connection_failure_cannot_leak_dsn_or_claim_success(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET postgresql://private")
    monkeypatch.setattr(maintenance.psycopg, "connect", fail)
    result = maintenance.run("not-a-real-dsn", apply=True)
    assert result["status"] == "FAILED_CONNECTION_OR_CONFIGURATION"
    assert not result["reindex_started"] and not result["build_query_ended"]
    assert result["physical_index_reclaimed_bytes"] is None
    assert "SECRET" not in str(result) and "postgresql://" not in str(result)
