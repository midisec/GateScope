from concurrent.futures import ThreadPoolExecutor
from threading import Event

from gatescope.application.market_sync import MarketSyncRuntime
from tests.unit.test_market_sync import Repository, Client, NOW
from tests.unit.test_structure_lifecycle import candidate
from tests.unit.test_pattern_study import candles
from gatescope.quant.structure_lifecycle import observe


def test_live_lane_does_not_wait_for_history_and_warms_without_a_browser():
    repo = Repository()
    client = Client()
    entered = Event()
    release = Event()
    warmed = []

    class SlowHistory:
        def fetch_candles(self, *args):
            entered.set()
            assert release.wait(3)
            return [], False

    class Warmer:
        def warm(self, s):
            warmed.append(s.symbol)

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(MarketSyncRuntime(repo, SlowHistory(), now=lambda: NOW).run_cycle, live=False)
        try:
            assert entered.wait(1)
            result = MarketSyncRuntime(repo, client, warmer=Warmer(), now=lambda: NOW).run_cycle(
                history=False
            )
            assert result["updated_subscriptions"] == 2 and len(client.calls) == 2
            assert len(warmed) == 2 and not task.done()
        finally:
            release.set()
        task.result()


def test_failed_snapshot_build_does_not_mark_successful_market_poll_failed():
    class Broken:
        def warm(self, s):
            raise RuntimeError("storage temporarily unavailable")

    result = MarketSyncRuntime(Repository(), Client(), warmer=Broken(), now=lambda: NOW).run_cycle(
        history=False
    )
    assert result["updated_subscriptions"] == 2 and result["snapshot_error"] == "CHART_SNAPSHOT_WARM_FAILED"


def test_durable_terminal_guard_stops_resurrection_after_projection_eviction():
    bars = candles(35)
    state = {"step_seconds": 3600}
    calls = []

    def guard(ids):
        calls.extend(ids)
        return {"a"}

    journal = observe(bars, 25, state, detector=lambda p: [candidate(p)], terminal_guard=guard)
    assert calls == ["a"] and not journal and not state["records"]
