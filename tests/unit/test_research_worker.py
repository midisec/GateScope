from contextlib import contextmanager
from pathlib import Path

import pytest

from apps.research_worker import main as worker
from gatescope.application.storage_pressure import GIB, StoragePressureGuard


class Pool:
    instances=[]
    def __init__(self,url,**kwargs):
        self.calls=[]
        self.closed=False
        self.__class__.instances.append(self)
    def open(self):
        pass
    def close(self):
        self.closed=True
    @contextmanager
    def public_transaction(self):
        yield self
    def execute(self,query,params):
        self.calls.append((query,params))


def test_isolated_worker_runs_both_queues_without_strategy_heartbeat(monkeypatch):
    calls=[]
    class Legacy:
        def __init__(self,pool):
            pass
        def run_one(self,progress):
            calls.append("legacy")
            progress(10,500)
            return True
    class Structures:
        def __init__(self,pool):
            pass
        def run_slice(self,**kwargs):
            assert kwargs=={"max_bars":32,"budget_seconds":1.5}
            calls.append("structure")
            return True
    class Portfolios:
        def __init__(self,pool):
            pass
        def run_one(self,progress):
            calls.append("portfolio")
            progress(20,1000)
            return True
    monkeypatch.setattr(worker,"PostgresPool",Pool)
    monkeypatch.setattr(worker,"BacktestRepository",Legacy)
    monkeypatch.setattr(worker,"StructureResearchRepository",Structures)
    monkeypatch.setattr(worker,"PortfolioResearchRepository",Portfolios)
    monkeypatch.setattr(worker,"install_stop_handlers",lambda _:None)
    monkeypatch.setattr(worker,"StoragePressureGuard",lambda *args,**kwargs:StoragePressureGuard(
        Path("/app"),"postgresql://localhost/test",environ={"GATESCOPE_HISTORY_STORAGE_PATHS":"/db"},
        disk_probe=lambda _:6*GIB))
    monkeypatch.setenv("GATESCOPE_DATABASE_URL","postgresql://test.invalid/research")
    monkeypatch.setenv("GATESCOPE_WORKER_ID","formal-strategy-primary")
    monkeypatch.delenv("GATESCOPE_RESEARCH_WORKER_ID",raising=False)
    monkeypatch.setenv("GATESCOPE_WORKER_ONCE","1")
    worker.main()
    pool=Pool.instances[-1]
    assert calls==["legacy","structure","portfolio"] and pool.closed
    assert all("'research-worker'" in query and "'strategy-worker'" not in query for query,_ in pool.calls)
    assert all(params[0]=="formal-research-primary" for _,params in pool.calls)
    assert pool.calls[-1][1][1] is False
    assert any(params[2].obj["processed_bars"]==10 for _,params in pool.calls)


def test_heartbeat_excludes_arbitrary_private_fields():
    pool=Pool("unused")
    worker.heartbeat(pool,"research",running=True,status={"mode":"IDLE","password":"private-marker","failed":True})
    payload=pool.calls[-1][1][2].obj
    assert payload["safe_error_code"]=="RESEARCH_CYCLE_FAILED"
    assert "private-marker" not in repr(payload)


def test_worker_requires_postgres_without_echoing_configuration(monkeypatch):
    monkeypatch.setenv("GATESCOPE_DATABASE_URL","private-marker")
    with pytest.raises(RuntimeError) as exc:
        worker.main()
    assert "private-marker" not in str(exc.value)


def test_process_isolation_resource_limits_and_container_entrypoint():
    root=Path(__file__).resolve().parents[2]
    service=(root/"deploy/systemd/gatescope-research.service").read_text()
    for required in ("CPUQuota=100%","MemoryHigh=512M","MemoryMax=768M","Nice=10","TimeoutStopSec=30","formal-strategy.env","apps.research_worker.main"):
        assert required in service
    assert 'CMD ["python", "-m", "apps.research_worker.main"]' in (root/"deploy/images/research-worker.Dockerfile").read_text()
    strategy=(root/"apps/strategy_worker/main.py").read_text()
    assert "backtests.run_one" not in strategy and "structure_research.run_slice" not in strategy
    assert "gatescope-research.service" in (root/"scripts/gatescope_healthcheck.sh").read_text()
