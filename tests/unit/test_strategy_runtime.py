from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from gatescope.adapters.postgres_strategy import StructureSignalSnapshot
from gatescope.application.alerts import AlertRuleSnapshot, PatternObservation
from gatescope.application.strategy_runtime import FormalStrategyRunner
from gatescope.domain.market import Bar
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import observe
from tests.unit.test_structure_rules import pivot_case


class Repository:
    def __init__(self, rule, snapshot):
        self.rules = [rule]
        self.snapshot = snapshot
        self.reads = 0
        self.saved = []

    def due_tenants(self):
        return [uuid4()]

    def active_rules(self, *args):
        return [SimpleNamespace(snapshot=r) for r in self.rules]

    def structure_snapshot(self, *args, **kwargs):
        self.reads += 1
        return self.snapshot

    def last_alert_at(self, *args):
        return None

    def persist_pattern_events(self, tenant, subject, instrument, timeframe, events, bars):
        self.saved.extend(events)
        return [
            PatternObservation(
                e["evidence_id"],
                e["id"],
                e["pattern"],
                e["confidence"],
                e["confirmed"],
                datetime.fromtimestamp(e["end_time"], timezone.utc),
                datetime.fromtimestamp(e["observed_time"], timezone.utc),
                "a" * 64,
                direction=e["signal_direction"],
                observed_at=datetime.fromtimestamp(e["observed_time"], timezone.utc),
                detector_version=VERSION,
                evidence_id=e["evidence_id"],
                phase=e["phase"],
            )
            for e in events
        ]


class Writer:
    def __init__(self):
        self.decisions = []

    def record(self, tenant, subject, decision):
        if any(old.event_key == decision.event_key for old in self.decisions):
            return False
        self.decisions.append(decision)
        return True


def rule():
    return AlertRuleSnapshot(uuid4(), uuid4(), 1, uuid4(), "1h", ("double_bottom",), "ANY", 0.7, True, 3, 0)


def snapshot(rows, state):
    bars = []
    for row in rows:
        opened = datetime.fromtimestamp(row["ts"], timezone.utc)
        bars.append(
            Bar(
                "GATE",
                "FUTURES_USDT",
                "btc",
                "1h",
                opened,
                opened + timedelta(hours=1),
                *[Decimal(str(row[key])) for key in ("open", "high", "low", "close", "volume")],
                True,
                1,
                opened + timedelta(hours=1),
            )
        )
    events = [r["latest"] for r in state["records"].values() if r["pattern"] == "double_bottom"]
    return StructureSignalSnapshot(str(rows[-1]["ts"]), bars, events)


def test_formal_signal_consumes_same_two_close_lifecycle_as_chart():
    rows, _ = pivot_case("double_bottom")
    offset = rows[0]["ts"] % 3600
    for row in rows:
        row["ts"] -= offset
    state = {"step_seconds": 3600}
    for i in range(19, len(rows)):
        observe(rows, i, state)
    active = rule()
    writer = Writer()
    repo = Repository(active, None)
    clock = [datetime.fromtimestamp(rows[-1]["ts"] + 3600, timezone.utc)]
    runner = FormalStrategyRunner(repo, writer, subject_id=uuid4(), clock=lambda: clock[0])
    for price, expected in ((142.0, 0), (143.0, 1)):
        rows.append(
            {
                "ts": rows[-1]["ts"] + 3600,
                "open": price,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price,
                "volume": 10.0,
            }
        )
        observe(rows, len(rows) - 1, state)
        repo.snapshot = snapshot(rows, state)
        clock[0] = repo.snapshot.bars[-1].close_time + timedelta(seconds=5)
        result = runner.run_once()
        assert result.failed == 0 and result.emitted == expected
    assert len(writer.decisions) == 1
    assert repo.saved[-1]["phase"] == "CONFIRMED"
    assert runner.run_once().scanned == 0
    restarted = FormalStrategyRunner(repo, writer, subject_id=uuid4(), clock=lambda: clock[0])
    assert restarted.run_once().emitted == 0


def test_cache_is_read_once_per_pair_and_old_snapshot_cannot_trigger():
    rows, _ = pivot_case("double_bottom")
    offset = rows[0]["ts"] % 3600
    for row in rows:
        row["ts"] -= offset
    state = {"step_seconds": 3600}
    for i in range(19, len(rows)):
        observe(rows, i, state)
    data = snapshot(rows, state)
    active = replace(rule(), confirmed_only=False)
    repo = Repository(active, data)
    repo.rules.append(replace(active, rule_id=uuid4(), version_id=uuid4()))
    writer = Writer()
    runner = FormalStrategyRunner(
        repo, writer, subject_id=uuid4(), clock=lambda: data.bars[-1].close_time + timedelta(days=1)
    )
    result = runner.run_once()
    assert repo.reads == 1 and result.emitted == 0
    repo.snapshot = None
    assert runner.run_once().emitted == 0


def test_any_emits_all_distinct_candidates_in_same_snapshot_without_repeats():
    rows, _ = pivot_case("double_bottom")
    offset = rows[0]["ts"] % 3600
    for row in rows:
        row["ts"] -= offset
    state = {"step_seconds": 3600}
    for i in range(19, len(rows)):
        observe(rows, i, state)
    data = snapshot(rows, state)
    assert data.events
    original = data.events[0]
    observed = int(data.bars[-1].close_time.timestamp())
    events = [{**original, "id": f"candidate-{n}", "evidence_id": str(n) * 64,
               "observed_time": observed, "confirmed": True, "phase": "CONFIRMED",
               "signal_direction": "bullish", "confidence": .8} for n in (1, 2, 3)]
    data = replace(data, events=events)
    repo, writer = Repository(rule(), data), Writer()
    def clock():
        return data.bars[-1].close_time + timedelta(seconds=5)
    runner = FormalStrategyRunner(repo, writer, subject_id=uuid4(), clock=clock)
    assert runner.run_once().emitted == 3
    assert runner.run_once().emitted == 0
    assert FormalStrategyRunner(repo, writer, subject_id=uuid4(), clock=clock).run_once().emitted == 0
    assert len(writer.decisions) == 3
