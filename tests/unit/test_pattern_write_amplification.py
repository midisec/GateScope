"""Storage-only optimizations retain the same new-close and immutable prediction semantics."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.quant.structure_detection import VERSION


INSTRUMENT = UUID(int=1)
NOW = datetime.now(timezone.utc)


class TrackingPool:
    def __init__(self, state, *, next_bar=True, version=VERSION):
        self.state = deepcopy(state)
        self.next_bar, self.version = next_bar, version
        self.statements = []
        self.error = "PATTERN_PREVIOUS_FAILURE"
        self.historical = ({"cell": [20, 12]}, {"wave": [[110, 2.0, "historical"]]}, 40, NOW)

    @contextmanager
    def public_transaction(self):
        yield self

    def execute(self, statement, params=()):
        sql = " ".join(statement.split())
        self.statements.append((sql, params))
        row = None
        if sql.startswith("SELECT pg_try_advisory"):
            row = (True,)
        elif sql.startswith("SELECT t.instrument_id"):
            row = (INSTRUMENT, "1m", deepcopy(self.state), self.version)
        elif sql.startswith("SELECT 1 FROM bars"):
            assert params[:3] == (INSTRUMENT, "1m", self.state["last_ts"])
            for clock in ("available_at<=%s", "close_time<=%s", "created_at<=%s", "is_final=true"):
                assert clock in sql
            assert "ORDER BY open_time DESC LIMIT 1" in sql
            row = (1,) if self.next_bar else None
        elif sql.startswith("SELECT checkpoint_json->'training'"):
            row = self.historical
        elif sql.startswith("UPDATE market_pattern_tracking SET checkpoint_json="):
            self.state = deepcopy(params[0].obj)
            if "safe_error=%s" in sql:
                self.error = params[1]
        elif sql.startswith("UPDATE market_pattern_tracking SET updated_at=now(),safe_error="):
            self.error = params[0]
        elif not sql.startswith(("INSERT INTO market_pattern_tracking", "UPDATE market_pattern_tracking SET updated_at=now()")):
            raise AssertionError(sql)
        return SimpleNamespace(fetchone=lambda: row)


def initial():
    return {"last_ts": 100, "last_index": 0, "records": {
        "old": {"phase": "EXPIRED", "last_index": 0, "first": {"prediction": 123.123456789}}
    }, "metadata": {"snapshot_at": "original", "input_digest": "unchanged"}}


def prepare(monkeypatch, pool, bars=None):
    monkeypatch.setattr(studies, "snapshot", lambda *args, **kwargs: (bars if bars is not None else [{"ts": 100}, {"ts": 160}], {"snapshot_at": "new"}))
    monkeypatch.setattr(studies, "write_journal", lambda *args, **kwargs: None)
    monkeypatch.setattr(annual, "project_journal", lambda *args: None)
    return studies.PatternStudyRepository(pool)


def test_no_new_closed_bar_only_touches_scheduler_not_snapshot_seed_or_error(monkeypatch):
    pool = TrackingPool(initial(), next_bar=False)
    repo = prepare(monkeypatch, pool)
    monkeypatch.setattr(studies, "snapshot", lambda *args, **kwargs: pytest.fail("idle poll must not read full OHLC snapshot"))
    monkeypatch.setattr(studies, "observe", lambda *args, **kwargs: pytest.fail("no new close"))
    assert repo.track_slice()
    assert pool.state == initial() and pool.error == "PATTERN_PREVIOUS_FAILURE"
    assert not any("SELECT checkpoint_json->'training'" in sql or "SET checkpoint_json=" in sql for sql, _ in pool.statements)
    assert pool.statements[-1][0].startswith("UPDATE market_pattern_tracking SET updated_at=now() WHERE")


@pytest.mark.parametrize("options", [{"seconds": 0}, {"max_steps": 0}])
def test_budget_yield_does_not_seed_or_rewrite_unprocessed_snapshot(monkeypatch, options):
    pool = TrackingPool(initial())
    repo = prepare(monkeypatch, pool)
    monkeypatch.setattr(studies, "observe", lambda *args, **kwargs: pytest.fail("budget yielded"))
    assert repo.track_slice(**options)
    assert pool.state == initial() and pool.error == "PATTERN_PREVIOUS_FAILURE"
    assert not any("SET checkpoint_json=" in sql for sql, _ in pool.statements)


def test_insufficient_initial_bars_stay_unseeded_and_do_not_replace_snapshot(monkeypatch):
    pool = TrackingPool({})
    repo = prepare(monkeypatch, pool, [{"ts": index * 60} for index in range(19)])
    assert repo.track_slice() and pool.state == {}
    assert not any("SELECT checkpoint_json->'training'" in sql or "SET checkpoint_json=" in sql for sql, _ in pool.statements)


def test_new_close_seeds_once_and_advances_without_replacing_frozen_predictions(monkeypatch):
    pool = TrackingPool(initial())
    repo = prepare(monkeypatch, pool)
    calls = []

    def observe(bars, index, state, **kwargs):
        calls.append(deepcopy(state))
        assert state["training"]["cell"] == [20, 12] and state["history_seeded"]
        assert state["forecast_training"]["wave"] == [[110, 2.0, "historical"]]
        state.update(last_ts=bars[index]["ts"], last_index=state["last_index"] + 1)
        return []

    monkeypatch.setattr(studies, "observe", observe)
    assert repo.track_slice(seconds=5)
    assert len(calls) == 1 and pool.state["last_ts"] == 160 and pool.error is None
    assert pool.state["records"] == initial()["records"]
    assert pool.state["metadata"] == {"snapshot_at": "new"}
    frozen = deepcopy(pool.state)
    pool.next_bar = False
    pool.historical = ({"future": [900, 800]}, {}, 220, NOW)
    assert repo.track_slice() and pool.state == frozen and len(calls) == 1


def test_failed_first_bar_preserves_checkpoint_and_seed_then_retries(monkeypatch):
    pool = TrackingPool(initial())
    repo = prepare(monkeypatch, pool)

    def fail(bars, index, state, **kwargs):
        assert state["history_seeded"]
        state["records"].clear()
        raise ValueError("PATTERN_HISTORY_GAP")

    monkeypatch.setattr(studies, "observe", fail)
    assert repo.track_slice(seconds=5)
    assert pool.state == initial() and pool.error == "PATTERN_HISTORY_GAP"
    assert not any("SET checkpoint_json=" in sql for sql, _ in pool.statements)

    def succeed(bars, index, state, **kwargs):
        assert state["history_seeded"] and state["records"] == initial()["records"]
        state.update(last_ts=bars[index]["ts"], last_index=state["last_index"] + 1)
        return []

    monkeypatch.setattr(studies, "observe", succeed)
    assert repo.track_slice(seconds=5)
    assert pool.state["last_ts"] == 160 and pool.error is None


def test_partial_progress_retains_successful_prefix_and_rolls_back_failed_bar_learning(monkeypatch):
    pool = TrackingPool(initial())
    pool.historical = ({"cell": [20, 12]}, {"wave": [[110, 2.0, "past"], [250, 3.0, "later"]]}, 40, NOW)
    repo = prepare(monkeypatch, pool, [{"ts": 100}, {"ts": 160}, {"ts": 220}])

    def observe(bars, index, state, **kwargs):
        if bars[index]["ts"] == 220:
            assert len(state["forecast_training"]["wave"]) == 2
            state["records"].clear()
            raise ValueError("PATTERN_CALCULATION_FAILED")
        state.update(last_ts=160, last_index=1)
        return []

    monkeypatch.setattr(studies, "observe", observe)
    assert repo.track_slice(seconds=5)
    assert pool.state["last_ts"] == 160 and pool.error == "PATTERN_CALCULATION_FAILED"
    assert pool.state["forecast_training"]["wave"] == [[110, 2.0, "past"]]
    assert pool.state["records"] == initial()["records"]


def test_model_change_still_censors_existing_candidates_without_new_bar(monkeypatch):
    state = {**initial(), "step_seconds": 60, "active": ["old"]}
    pool = TrackingPool(state, next_bar=False, version="previous-model")
    repo = prepare(monkeypatch, pool)
    changes = []

    def append(record, phase, *args):
        changes.append(phase)
        record["phase"] = phase

    monkeypatch.setattr(studies, "append_transition", append)
    monkeypatch.setattr(studies, "snapshot", lambda *args, **kwargs: pytest.fail("model retirement is independent of candles"))
    assert repo.track_slice()
    assert changes == ["CENSORED"] and pool.state["active"] == []
    assert pool.state["records"]["old"]["phase"] == "CENSORED"
    assert not any("SELECT 1 FROM bars" in sql for sql, _ in pool.statements)


class ProjectionConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params):
        self.statements.append((" ".join(sql.split()), params))

    @contextmanager
    def cursor(self):
        yield self

    def executemany(self, sql, params):
        self.execute(sql, list(params))


def test_projection_noop_guards_compare_only_mutable_outcome_columns():
    conn = ProjectionConnection()
    record = {"id": "candidate", "pattern": "double_top", "phase": "FORMING", "first": {},
              "stages": {"FORMING": {"score_bucket": "HIGH", "time": 100, "sampled": True,
                                      "prediction": {"probability": .8}}}}
    annual.project_journal(conn, "context", [{"record": record, "sequence": 1}])
    sql = conn.statements[-1][0]
    assert "IS DISTINCT FROM (EXCLUDED.phase,EXCLUDED.resolved_time,EXCLUDED.formed,EXCLUDED.outcome_json)" in sql
    mutable_sql = sql.split("DO UPDATE SET", 1)[1]
    for frozen in ("prediction", "forecast_json", "sampled", "stage_time", "environment_key", "score_bucket"):
        assert frozen not in mutable_sql
    annual.project_returns(conn, "context", [{"evidence_id": "event", "pattern": "double_top", "phase": "FORMING",
        "horizon": 5, "observed_time": 100, "status": "PENDING", "side": -1}])
    sql = conn.statements[-1][0]
    assert "IS DISTINCT FROM (EXCLUDED.status,EXCLUDED.entry_time,EXCLUDED.exit_time, EXCLUDED.net_return,EXCLUDED.mfe,EXCLUDED.mae)" in sql
    mutable_sql = sql.split("DO UPDATE SET", 1)[1]
    for frozen in ("pattern", "phase", "observed_time", "side", "horizon"):
        assert frozen not in mutable_sql
