from pathlib import Path

import pytest

from gatescope.application.storage_pressure import GIB, StoragePressureGuard, safe_storage_status


def make_guard(free=6 * GIB, *, env=None, probe=None, clock=lambda: 0, database_paths=None,
               database_url="postgresql://localhost/test"):
    return StoragePressureGuard(Path("/app"), database_url,
        environ={"GATESCOPE_HISTORY_STORAGE_PATHS": "/db:/wal", **(env or {})},
        disk_probe=probe or (lambda _: free), clock=clock, database_paths=database_paths)


def test_low_disk_and_strict_recovery_hysteresis():
    available = [3 * GIB]
    guard = make_guard(probe=lambda _: available[0])
    assert guard.check().allowed  # Pause below 3 GiB, not at the boundary.
    available[0] -= 1
    assert guard.check(force=True).state == "PAUSED_LOW_DISK"
    for size in (0, 3 * GIB, 4 * GIB, 5 * GIB):
        available[0] = size
        assert not guard.check(force=True).allowed
    available[0] = 5 * GIB + 1
    assert guard.check(force=True).allowed


def test_checks_application_data_and_wal_and_uses_lowest_capacity():
    calls = []
    values = {"/app": 20 * GIB, "/db": 9 * GIB, "/wal": 200 * 1024 ** 2}
    def probe(path):
        calls.append(str(path))
        return values[str(path)]
    result = make_guard(probe=probe).check()
    assert calls == ["/app", "/db", "/wal"]
    assert result.state == "PAUSED_LOW_DISK" and result.min_free_bytes == values["/wal"]
    assert result.checked_paths == 3 and result.database_scope == "LOCAL_DATABASE_EXPLICIT_PATHS"


def test_probe_throttle_avoids_busy_loop_and_rechecks_after_interval():
    tick, calls = [0], []
    guard = make_guard(probe=lambda path: calls.append(path) or 0, clock=lambda: tick[0])
    first = guard.check()
    for _ in range(100):
        assert guard.check() is first
    assert len(calls) == 3
    tick[0] = 15
    guard.check()
    assert len(calls) == 6


@pytest.mark.parametrize("error", [PermissionError("private-db-path"), FileNotFoundError("private-db-path")])
def test_path_error_fails_closed_and_recovers_without_echoing_path(error):
    failing = [True]
    def probe(path):
        if str(path) == "/wal" and failing[0]:
            raise error
        return 6 * GIB
    guard = make_guard(probe=probe)
    result = guard.check()
    assert result.state == "PAUSED_STORAGE_UNAVAILABLE"
    assert "private-db-path" not in repr(result.heartbeat())
    failing[0] = False
    assert guard.check(force=True).allowed


@pytest.mark.parametrize("setting", ["false", "0"])
def test_explicit_disable_never_probes_or_claims(setting):
    def forbidden(_):
        raise AssertionError("disabled policy must not inspect the filesystem")
    assert make_guard(env={"GATESCOPE_HISTORY_TASKS_ENABLED": setting}, probe=forbidden).check().state == "PAUSED_DISABLED"


@pytest.mark.parametrize("env", [
    {"GATESCOPE_HISTORY_STORAGE_PATHS": "relative-path"},
    {"GATESCOPE_HISTORY_STORAGE_PATHS": "/db:"},
    {"GATESCOPE_HISTORY_STORAGE_PATHS": ""},
    {"GATESCOPE_HISTORY_PAUSE_FREE_GIB": "nan"},
    {"GATESCOPE_HISTORY_RESUME_FREE_GIB": "2"},
    {"GATESCOPE_HISTORY_CHECK_SECONDS": "0"},
    {"GATESCOPE_HISTORY_TASKS_ENABLED": "typo"},
])
def test_bad_configuration_pauses_history_instead_of_crashing_worker(env):
    assert make_guard(env=env).check().state == "PAUSED_STORAGE_UNAVAILABLE"


def test_automatic_local_database_discovery_is_fail_closed_and_checks_external_wal():
    calls = []
    guard = StoragePressureGuard(Path("/app"), "postgresql://127.0.0.1/test", environ={},
        database_paths=lambda: (Path("/data"), Path("/other-mount/wal")),
        disk_probe=lambda path: calls.append(path) or 9 * GIB)
    assert guard.check().database_scope == "LOCAL_DATABASE_AUTO"
    assert calls == [Path("/app"), Path("/data"), Path("/other-mount/wal")]
    unavailable = StoragePressureGuard(Path("/app"), "postgresql://localhost/test", environ={},
        database_paths=lambda: (), disk_probe=lambda _: 9 * GIB)
    assert unavailable.check().state == "PAUSED_STORAGE_UNAVAILABLE"


def test_remote_database_requires_explicit_external_monitoring_not_local_path_pretense():
    guard = make_guard(database_url="postgresql://remote.example/test")
    assert guard.check().state == "PAUSED_STORAGE_UNAVAILABLE"
    assert guard.check().database_scope == "UNVERIFIED"
    acknowledged = make_guard(database_url="postgresql://remote.example/test", env={
        "GATESCOPE_HISTORY_REMOTE_DB_CAPACITY": "EXTERNALLY_MONITORED"})
    assert acknowledged.check().allowed
    assert acknowledged.check().database_scope == "REMOTE_DATABASE_EXTERNALLY_MONITORED"
    assert not make_guard(0, database_url="postgresql://remote.example/test", env={
        "GATESCOPE_HISTORY_REMOTE_DB_CAPACITY": "EXTERNALLY_MONITORED"}).check().allowed


@pytest.mark.parametrize("env", [{"PGHOST": "remote.example"}, {"PGHOSTADDR": "192.0.2.1"}, {"PGSERVICE": "remote"}])
def test_implicit_libpq_remote_configuration_does_not_masquerade_as_local(env):
    assert make_guard(database_url="postgresql:///test", env=env).check().state == "PAUSED_STORAGE_UNAVAILABLE"


def test_heartbeat_status_drops_secrets_and_only_accepts_known_states():
    safe = safe_storage_status({**make_guard(0).check().heartbeat(), "database_url": "private-marker", "paths": ["private-marker"]})
    assert safe["state"] == "PAUSED_LOW_DISK" and "private-marker" not in repr(safe)
    assert safe_storage_status({"state": "private-marker"}) == {}
    assert safe_storage_status(None) == {}
