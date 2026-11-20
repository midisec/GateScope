from threading import Event
from unittest.mock import Mock

from apps.market_worker.main import _formal_main, _run_coinglass_loop


class Stop:
    def __init__(self, turns=1):
        self.turns = turns
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.turns

    def wait(self, seconds):
        self.waits.append(seconds)


def test_background_queue_fairness_and_idle_wait():
    order = []
    tests, charts = Mock(), Mock()
    tests.run_one.side_effect = lambda: order.append("test") or True
    charts.run_one.side_effect = lambda: order.append("chart") or True
    stopped = Stop(turns=2)
    updates = []
    _run_coinglass_loop(charts, tests, stopped, on_status=updates.append)
    assert order == ["test", "chart", "test", "chart"]
    assert stopped.waits == [0.05, 0.05]
    assert updates[-1]["provider_tests_processed"] == 2
    assert updates[-1]["coinglass_chart_jobs_processed"] == 2

    tests.run_one.side_effect = charts.run_one.side_effect = None
    tests.run_one.return_value = charts.run_one.return_value = False
    stopped = Stop()
    _run_coinglass_loop(charts, tests, stopped)
    assert stopped.waits == [2.0]


def test_background_failure_does_not_starve_other_queue_and_once_is_bounded():
    charts, tests = Mock(), Mock()
    tests.run_one.side_effect = RuntimeError("not exposed")
    charts.run_one.return_value = True
    stopped = Stop()
    updates = []
    _run_coinglass_loop(charts, tests, stopped, once=True, on_status=updates.append)
    assert stopped.waits == []
    charts.run_one.assert_called_once()
    assert updates[-1]["coinglass_loop_error"] == "COINGLASS_BACKGROUND_FAILED"
    assert "not exposed" not in str(updates)


def test_formal_worker_starts_provider_before_a_blocking_gate_live_cycle(monkeypatch):
    monkeypatch.setenv("GATESCOPE_WORKER_ONCE", "1")
    monkeypatch.setenv("GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY", "fixture-only")
    monkeypatch.setenv("GATESCOPE_COINGLASS_KEY_VERSION", "fixture")
    provider_started = Event()
    pool, repository = Mock(), Mock()
    charts, tests = Mock(), Mock()
    tests.run_one.return_value = False

    def chart_job():
        provider_started.set()
        return True

    charts.run_one.side_effect = chart_job
    live, history = Mock(), Mock()

    def live_cycle(**kwargs):
        # The old scheduler could not start its provider future until this
        # blocking Gate cycle returned, so this condition reproduces the bug.
        assert provider_started.wait(2), "provider dispatch still depends on Gate completion"
        return {}

    live.run_cycle.side_effect = live_cycle
    history.run_cycle.return_value = {}
    gate = Mock()
    gate.fetch_catalog.return_value = []
    monkeypatch.setattr("apps.market_worker.main.GateClient", Mock(return_value=gate))
    monkeypatch.setattr("apps.market_worker.main.install_stop_handlers", Mock())
    monkeypatch.setattr("gatescope.adapters.postgres.PostgresPool", Mock(return_value=pool))
    monkeypatch.setattr(
        "gatescope.adapters.postgres_market_ingest.PostgresMarketIngestRepository",
        Mock(return_value=repository),
    )
    monkeypatch.setattr("gatescope.adapters.postgres_coinglass.PostgresCoinglassRepository", Mock())
    monkeypatch.setattr(
        "gatescope.adapters.postgres_coinglass_context.PostgresCoinglassContextRepository", Mock()
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.CoinglassChartRuntime", Mock(return_value=charts)
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_runtime.CoinglassTestRuntime", Mock(return_value=tests)
    )
    monkeypatch.setattr(
        "gatescope.application.market_sync.MarketSyncRuntime", Mock(side_effect=[live, history])
    )
    monkeypatch.setattr("gatescope.adapters.gate_history.GateHistoryArchive", Mock())
    monkeypatch.setattr("gatescope.adapters.postgres_chart_cache.PostgresChartCache", Mock())
    monkeypatch.setattr("gatescope.credential_crypto.RsaOaepCredentialDecryptor.from_base64_der", Mock())
    _formal_main("postgresql://localhost/unused-no-network")
    assert provider_started.is_set()
    charts.run_one.assert_called_once()
    tests.run_one.assert_called_once()
    charts.close.assert_called_once()
    tests.close.assert_called_once()
    pool.close.assert_called_once()
