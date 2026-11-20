from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from apps.research_worker import main as research
from apps.strategy_worker import main as strategy
from gatescope.application.storage_pressure import GIB, StoragePressureGuard


def guard(free=0):
    return StoragePressureGuard(Path("/app"), "postgresql://localhost/test",
        environ={"GATESCOPE_HISTORY_STORAGE_PATHS": "/db"}, disk_probe=lambda _: free)


class Pool:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.closed = False
    def open(self):
        pass
    def close(self):
        self.closed = True
    @contextmanager
    def public_transaction(self):
        yield self
    def execute(self, query, params):
        self.calls.append((query, params))


class Stop:
    def __init__(self):
        self.stopped = False
        self.waits = []
    def is_set(self):
        return self.stopped
    def set(self):
        self.stopped = True
    def wait(self, seconds):
        self.waits.append(seconds)
        self.set()


def test_strategy_low_disk_preserves_rules_and_live_tracking_without_history(monkeypatch):
    from gatescope.adapters import postgres, postgres_pattern_studies, postgres_strategy
    from gatescope.application import alerts, strategy_runtime
    calls, heartbeats = [], []
    pool, stop = Pool(), Stop()
    runner = SimpleNamespace(run_once=lambda: calls.append("rules") or SimpleNamespace(
        tenants=1, rules=1, scanned=1, emitted=0, failed=0))
    repo = SimpleNamespace(heartbeat=lambda *args, **kwargs: heartbeats.append(kwargs))
    patterns = SimpleNamespace(track_slice=lambda: calls.append("track") or False,
        schedule=lambda: calls.append("schedule"), run_slice=lambda: calls.append("replay") or True)
    monkeypatch.setattr(postgres, "PostgresPool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(postgres_strategy, "PostgresStrategyRepository", lambda _: repo)
    monkeypatch.setattr(postgres_pattern_studies, "PatternStudyRepository", lambda _: patterns)
    monkeypatch.setattr(alerts, "PostgresAlertWriter", lambda _: None)
    monkeypatch.setattr(strategy_runtime, "FormalStrategyRunner", lambda *args, **kwargs: runner)
    monkeypatch.setattr(strategy, "StoragePressureGuard", lambda *args, **kwargs: guard())
    monkeypatch.setattr(strategy.threading, "Event", lambda: stop)
    monkeypatch.setattr(strategy, "install_stop_handlers", lambda _: None)
    monkeypatch.delenv("GATESCOPE_WORKER_ONCE", raising=False)
    strategy._formal_main("postgresql://localhost/test")
    assert calls == ["rules", "track"]
    assert stop.waits == [5] and pool.closed
    assert heartbeats[0]["running"] is True
    assert heartbeats[0]["status"]["history_state"] == "PAUSED_LOW_DISK"
    assert heartbeats[0]["status"]["failed_rules"] == 0


def test_strategy_resumes_same_replay_interface_after_capacity_recovers():
    calls, available = [], [0]
    storage = StoragePressureGuard(Path("/app"), "postgresql://localhost/test",
        environ={"GATESCOPE_HISTORY_STORAGE_PATHS": "/db"}, disk_probe=lambda _: available[0])
    patterns = SimpleNamespace(track_slice=lambda: calls.append("track") or True,
        schedule=lambda: calls.append("schedule"), run_slice=lambda: calls.append("replay") or True)
    assert strategy.run_pattern_cycle(patterns, storage)[:2] == (True, False)
    available[0] = 6 * GIB
    storage.check(force=True)
    assert strategy.run_pattern_cycle(patterns, storage)[:2] == (True, True)
    assert calls == ["track", "track", "schedule", "replay"]


def test_research_low_disk_keeps_running_heartbeat_without_claim_or_busy_loop(monkeypatch):
    pool, stop, claims = Pool(), Stop(), []
    repo = SimpleNamespace(run_one=lambda **kwargs: claims.append("run_one"),
        run_slice=lambda **kwargs: claims.append("run_slice"))
    monkeypatch.setattr(research, "PostgresPool", lambda *args, **kwargs: pool)
    for name in ("BacktestRepository", "StructureResearchRepository", "PortfolioResearchRepository"):
        monkeypatch.setattr(research, name, lambda _: repo)
    monkeypatch.setattr(research, "StoragePressureGuard", lambda *args, **kwargs: guard())
    monkeypatch.setattr(research.threading, "Event", lambda: stop)
    monkeypatch.setattr(research, "install_stop_handlers", lambda _: None)
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.delenv("GATESCOPE_WORKER_ONCE", raising=False)
    research.main()
    assert claims == [] and stop.waits == [15] and pool.closed
    active = [params for _, params in pool.calls if params[1]]
    assert active and all(params[2].obj["mode"] == "PAUSED_LOW_DISK" for params in active)
    assert all(params[2].obj["history_state"] == "PAUSED_LOW_DISK" for params in active)
    assert all(not params[2].obj["degraded"] for params in active)
    assert not any("UPDATE research_" in query for query, _ in pool.calls)


def test_research_rechecks_before_claiming_next_queue_after_long_job(monkeypatch):
    pool, calls = Pool(), []
    available, tick = [6 * GIB], [0]
    storage = StoragePressureGuard(Path("/app"), "postgresql://localhost/test",
        environ={"GATESCOPE_HISTORY_STORAGE_PATHS": "/db"}, disk_probe=lambda _: available[0], clock=lambda: tick[0])
    def legacy(**kwargs):
        calls.append("legacy")
        available[0], tick[0] = 0, 16
        return True
    monkeypatch.setattr(research, "PostgresPool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(research, "BacktestRepository", lambda _: SimpleNamespace(run_one=legacy))
    monkeypatch.setattr(research, "StructureResearchRepository", lambda _: SimpleNamespace(
        run_slice=lambda **kwargs: calls.append("structure")))
    monkeypatch.setattr(research, "PortfolioResearchRepository", lambda _: SimpleNamespace(
        run_one=lambda **kwargs: calls.append("portfolio")))
    monkeypatch.setattr(research, "StoragePressureGuard", lambda *args, **kwargs: storage)
    monkeypatch.setattr(research, "install_stop_handlers", lambda _: None)
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://localhost/test")
    monkeypatch.setenv("GATESCOPE_WORKER_ONCE", "1")
    research.main()
    assert calls == ["legacy"]
    assert pool.calls[-2][1][2].obj["mode"] == "PAUSED_LOW_DISK"


def test_strategy_heartbeat_preserves_safe_pause_reason_without_secret_fields():
    from gatescope.adapters.postgres_strategy import PostgresStrategyRepository
    import json
    pool = Pool()
    PostgresStrategyRepository(pool).heartbeat("worker", running=True,
        status={"storage_pressure": {**guard().check().heartbeat(), "database_url": "private-marker"}})
    result = json.loads(pool.calls[-1][1][2])
    assert result["history_state"] == "PAUSED_LOW_DISK"
    assert result["safe_error_code"] is None
    assert "private-marker" not in repr(result)
