from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from psycopg.errors import QueryCanceled

from gatescope.adapters.postgres_mcp_structures import McpStructureRepository, _shape_summary
from gatescope.quant.structure_detection import VERSION

INSTRUMENT = uuid4()
CONTEXT = "c" * 64
CANDIDATE = "a" * 64
NOW = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
STAMP = datetime.fromtimestamp(NOW, timezone.utc)


@pytest.fixture(autouse=True)
def freeze_repository_read_clock(monkeypatch):
    """Collection may precede this module by minutes in the full test suite."""
    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class ReadClock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return STAMP.astimezone(tz) if tz is not None else STAMP.replace(tzinfo=None)

    monkeypatch.setattr("gatescope.adapters.postgres_mcp_structures.datetime", ReadClock)


class Connection:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if sql.startswith("SET "):
            return Mock()
        assert sql.lstrip().startswith(("SELECT ", "WITH "))
        value = self.replies.popleft()
        if isinstance(value, Exception):
            raise value
        return Mock(fetchone=Mock(return_value=value), fetchall=Mock(return_value=value))

    @contextmanager
    def transaction(self):
        yield self


class Pool:
    def __init__(self, *replies):
        self.conn = Connection(replies)

    @contextmanager
    def public_transaction(self):
        yield self.conn


def record(identifier=CANDIDATE, *, phase="FORMING", wave=False, confidence=0.8):
    first = {
        "id": identifier,
        "evidence_id": "f" * 64,
        "phase": "FORMING",
        "pattern": "elliott_impulse_bull" if wave else "double_bottom",
        "experimental": wave,
        "confidence": confidence,
        "name": "Wave" if wave else "Double bottom",
        "group": "structure",
        "signal_direction": "neutral",
        "points": [{"time": NOW - 120, "price": 12, "basis": "LOW"}],
        "lines": [{"from": {"time": NOW - 120, "price": 12}, "to": {"time": NOW - 60, "price": 13}}],
        "forecast": {"id": "first", "kind": "NEXT_PIVOT", "target": {"price": 14}},
        "levels": {"invalid_price": 10},
    }
    return {
        "id": identifier,
        "pattern": first["pattern"],
        "phase": phase,
        "first": first,
        "latest": {**deepcopy(first), "phase": phase, "forecast": {"id": "later", "target": {"price": 20}}},
        "first_observed_time": NOW,
        "observed_time": NOW + 60,
        "first_snapshot_at": STAMP.isoformat(),
        "snapshot_at": STAMP.isoformat(),
        "source_mode": "RECORDED",
        "stages": {
            "FORMING": {
                "time": NOW,
                "score_bucket": "HIGH",
                "prediction": {"sample_count": 4, "probability": 0.3},
            }
        },
    }


def tracking_pool(records, selected=None):
    selected = records if selected is None else selected
    replies = [(NOW - 60, 900, STAMP, None), [(r["id"], r) for r in records]]
    if selected:
        replies.append([(r["id"], r) for r in selected])
    replies.append((STAMP, "READY", 10000, 10000, VERSION))
    return Pool(*replies)


def event_row(value, sequence=1):
    return (sequence, value["phase"], None, STAMP, STAMP, VERSION, value["source_mode"], value)


def test_current_reads_existing_records_and_preserves_original_prediction(monkeypatch):
    value = record()
    before = deepcopy(value)
    pool = tracking_pool([value])
    reference = Mock(
        return_value={"sample_count": 20, "probability": 0.7, "failure_examples": [{"id": "failure"}]}
    )
    monkeypatch.setattr("gatescope.adapters.postgres_mcp_structures.annual.record_reference", reference)
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m")
    item = result["items"][0]
    assert result["status"] == "READY" and result["total"] == 1
    assert item["initial_prediction"]["probability"] == 0.3
    assert item["historical_reference"]["probability"] == 0.7
    assert item["forecast"]["id"] == "later"
    assert item["points"] == value["latest"]["points"]
    assert value == before
    assert result["data_watermark"] == NOW
    sql = " ".join(call[0] for call in pool.conn.calls)
    assert "request_pattern" not in sql and "INSERT" not in sql and "UPDATE" not in sql
    assert "REPEATABLE READ, READ ONLY" in sql and "statement_timeout" in sql
    assert "SELECT e.key,jsonb_build_object" in sql
    json.dumps(result, allow_nan=False)


def test_current_filters_before_pagination_and_keeps_failed_records(monkeypatch):
    records = [
        record(),
        record("b" * 64, phase="INVALIDATED", wave=True),
        record("d" * 64, wave=True, confidence=None),
    ]
    pool = tracking_pool(records, [records[1]])
    monkeypatch.setattr(
        "gatescope.adapters.postgres_mcp_structures.annual.record_reference", Mock(return_value={})
    )
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m", family="wave", confidence_min=0.7)
    assert result["tracked_loaded"] == 3 and result["total"] == 1
    assert result["items"][0]["phase"] == "INVALIDATED"
    assert result["summary"]["total"] == 1


def test_current_absent_checkpoint_does_not_create_or_report_zero_samples():
    pool = Pool(None)
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m")
    assert result["status"] == "NOT_REQUESTED" and result["total"] is None
    assert len(pool.conn.calls) == 4


def test_current_history_timeout_is_unknown_not_a_zero_success_rate(monkeypatch):
    pool = tracking_pool([record()])
    monkeypatch.setattr(
        "gatescope.adapters.postgres_mcp_structures.annual.record_reference",
        Mock(side_effect=QueryCanceled()),
    )
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m")
    history = result["items"][0]["historical_reference"]
    assert history["sample_count"] is None and history["probability"] is None
    assert history["reference_state"] == "UNAVAILABLE"
    assert result["status"] == "PARTIAL"
    assert "HISTORICAL_REFERENCE_UNAVAILABLE" in result["quality_flags"]


def test_current_summary_limit_is_explicit_not_a_false_total(monkeypatch):
    records = [record(), record("b" * 64)]
    pool = tracking_pool(records, [records[0]])
    monkeypatch.setattr("gatescope.adapters.postgres_mcp_structures.MAX_RECORDS", 1)
    monkeypatch.setattr(
        "gatescope.adapters.postgres_mcp_structures.annual.record_reference", Mock(return_value={})
    )
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m")
    assert result["status"] == "PARTIAL" and result["total"] is None
    assert result["source_truncated"] and result["has_more"]


def test_study_projects_stored_summary_and_bounds_report_arrays():
    row = (
        "READY",
        1000,
        1000,
        STAMP,
        STAMP,
        None,
        VERSION,
        True,
        {"version": VERSION, "to_time": NOW},
        {"snapshot_at": STAMP.isoformat()},
        [{"pattern": "double_bottom"}] * 501,
        [],
        [],
    )
    pool = Pool(row)
    result = McpStructureRepository(pool).study(INSTRUMENT, "1m", pattern="double_bottom")
    assert len(result["report"]["rows"]) == 500
    assert result["report"]["rows_truncated"]
    sql, args = pool.conn.calls[-1]
    assert "LIMIT 501" in sql and "training_samples" not in sql
    assert args[:6] == ("double_bottom",) * 6
    assert "checkpoint_json" not in sql
    json.dumps(result, allow_nan=False)


def test_study_does_not_mislabel_old_model_report():
    row = ("RUNNING", 10, 1000, STAMP, STAMP, None, VERSION, True, {"version": "older"}, {}, [], [], [])
    result = McpStructureRepository(Pool(row)).study(INSTRUMENT, "1m")
    assert result["report"] is None and result["report_status"] == "MODEL_MISMATCH"


def test_events_pages_latest_candidate_states_not_all_transitions():
    row = (CANDIDATE, 7, "INVALIDATED", STAMP, STAMP, "double_bottom", "Double bottom", str(NOW))
    pool = Pool((VERSION,), [row, row])
    result = McpStructureRepository(pool).events(
        INSTRUMENT, "1m", context=CONTEXT, phase="INVALIDATED", limit=1
    )
    assert len(result["items"]) == 1 and result["has_more"] and result["next_offset"] == 1
    assert result["total"] is None
    assert result["items"][0]["phase"] == "INVALIDATED"
    sql = pool.conn.calls[-1][0]
    assert "DISTINCT ON(candidate_id)" in sql and "COUNT(" not in sql.upper()
    assert pool.conn.calls[-2][1] == (CONTEXT, INSTRUMENT, "1m", "RECORDED")


def test_events_wrong_scope_returns_not_found_without_disclosing_candidates():
    pool = Pool(None)
    result = McpStructureRepository(pool).events(INSTRUMENT, "1m", context=CONTEXT, source="REPLAY")
    assert result["status"] == "NOT_FOUND" and result["items"] == []


def test_events_missing_replay_does_not_schedule_it():
    pool = Pool(None)
    result = McpStructureRepository(pool).events(INSTRUMENT, "1m", source="REPLAY")
    assert result["status"] == "NOT_REQUESTED"


def test_detail_checks_binding_before_returning_any_evidence():
    pool = Pool(None)
    result = McpStructureRepository(pool).detail(INSTRUMENT, "1m", CONTEXT, CANDIDATE)
    assert result["status"] == "NOT_FOUND"
    assert pool.conn.calls[-1][1] == (CONTEXT, CANDIDATE, INSTRUMENT, "1m")
    assert "bars" not in pool.conn.calls[-1][0]


def test_detail_keeps_original_forecast_and_separate_latest_geometry_and_pagination():
    first = record()
    last = record(phase="INVALIDATED")
    rows = [event_row(first), event_row(last, 2)]
    pool = Pool(rows[0], rows[1], rows)
    result = McpStructureRepository(pool).detail(
        INSTRUMENT, "1m", CONTEXT, CANDIDATE, transition_limit=1, bar_limit=0
    )
    assert result["first_record"]["first"]["forecast"]["id"] == "first"
    assert result["latest_record"]["phase"] == "INVALIDATED"
    assert result["latest_geometry"]["forecast"]["id"] == "later"
    assert result["transitions_has_more"] and result["next_transition_offset"] == 1
    assert result["chart"]["status"] == "NOT_REQUESTED"
    assert result["chart"]["geometry"]["forecast"]["id"] == "first"
    assert len(result["transitions"]) == 1
    json.dumps(result, allow_nan=False)


def test_detail_original_bars_are_revision_bounded_and_do_not_shift_old_anchor():
    value = record()
    event = event_row(value)
    opened = datetime.fromtimestamp(NOW - 60, timezone.utc)
    bar = (
        opened,
        STAMP,
        Decimal("12.0000"),
        Decimal("14"),
        Decimal("11"),
        Decimal("13"),
        Decimal("100"),
        STAMP,
        STAMP,
        2,
        "hash",
        ["REST_POLLING"],
    )
    pool = Pool(event, event, [event], [bar])
    result = McpStructureRepository(pool).detail(INSTRUMENT, "1m", CONTEXT, CANDIDATE, bar_limit=1)
    chart = result["chart"]
    assert chart["status"] == "CLIPPED" and chart["missing_anchor_count"] == 1
    assert chart["geometry"]["points"][0]["time"] == NOW - 120
    assert chart["bars"][0]["open"] == "12.0000" and chart["bars"][0]["revision"] == 2
    sql, args = pool.conn.calls[-1]
    assert "available_at<=%s AND created_at<=%s" in sql
    assert args[3:6] == (STAMP, STAMP, STAMP)
    assert "open_time>=to_timestamp" in sql


def test_read_timeout_returns_unknown_not_empty_success():
    result = McpStructureRepository(Pool(QueryCanceled())).current(INSTRUMENT, "1m")
    assert result["status"] == "UNAVAILABLE" and result["total"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": True},
        {"offset": -1},
        {"phase": "BUY"},
        {"family": "any"},
        {"pattern": "a'; drop table bars"},
        {"confidence_min": float("nan")},
        {"confidence_min": 0.9, "confidence_max": 0.1},
    ],
)
def test_invalid_current_arguments_do_not_query(kwargs):
    pool = Pool()
    with pytest.raises(ValueError):
        McpStructureRepository(pool).current(INSTRUMENT, "1m", **kwargs)
    assert not pool.conn.calls


@pytest.mark.parametrize(
    "kwargs", [{"bar_limit": 501}, {"bar_limit": True}, {"transition_limit": 0}, {"transition_offset": -1}]
)
def test_invalid_detail_arguments_do_not_query(kwargs):
    pool = Pool()
    with pytest.raises(ValueError):
        McpStructureRepository(pool).detail(INSTRUMENT, "1m", CONTEXT, CANDIDATE, **kwargs)
    assert not pool.conn.calls


@pytest.mark.parametrize(
    "phase, confirmed",
    [
        ("FORMING", False),
        ("FORMED", False),
        ("CONFIRMED", True),
        ("INVALIDATED", False),
        ("EXPIRED", False),
        ("CENSORED", False),
        ("UNKNOWN", None),
    ],
)
def test_shape_summary_never_conflates_geometry_formation_with_breakout_confirmation(phase, confirmed):
    value = record(phase=phase)
    result = _shape_summary(value["latest"], "4h", value)
    assert result["name"] == "Double bottom" and result["timeframe"] == "4h"
    assert result["is_confirmed"] is confirmed
    assert result["invalidation_price"] == 10
    assert result["first_seen"] == NOW
    assert result["trigger_price"] is None and result["field_notes"]["trigger_price"]


def test_shape_summary_uses_only_explicit_unique_confirmation_and_preserves_original():
    value = record()
    value["latest"]["forecast"].update(
        issued_at=NOW, confirmation=[{"side": 1, "price": 13.5}], confirmation_closes=2
    )
    before = deepcopy(value)
    result = _shape_summary(value["latest"], "1m", value)
    assert result["trigger_price"] == 13.5
    assert result["forecast_issued_at"] == NOW
    assert result["key_levels"]["confirmation_closes"] == 2
    assert value == before


def test_shape_summary_does_not_pick_a_direction_or_flatten_time_varying_invalidation():
    value = record()
    value["latest"]["forecast"]["confirmation"] = [{"side": 1, "price": 13.5}, {"side": -1, "price": 11}]
    value["latest"]["levels"]["invalid_edge"] = {
        "from": {"time": NOW - 60, "price": 10},
        "to": {"time": NOW, "price": 11},
    }
    result = _shape_summary(value["latest"], "1m", value)
    assert result["trigger_price"] is None and result["invalidation_price"] is None
    assert len(result["key_levels"]["confirmation_conditions"]) == 2
    assert result["key_levels"]["structure_levels"]["invalid_price"] == 10


def test_current_stale_checkpoint_is_not_reported_as_live_ready(monkeypatch):
    value = record()
    pool = tracking_pool([value])
    pool.conn.replies[0] = (NOW - 600, 900, STAMP, None)
    monkeypatch.setattr(
        "gatescope.adapters.postgres_mcp_structures.annual.record_reference", Mock(return_value={})
    )
    result = McpStructureRepository(pool).current(INSTRUMENT, "1m")
    assert result["status"] == "STALE" and result["checkpoint_stale"]
    assert "STRUCTURE_CHECKPOINT_STALE" in result["quality_flags"]
    assert result["items"][0]["shape_summary"]["is_confirmed"] is False


def test_original_geometry_reports_revised_candle_without_moving_anchor():
    value = record()
    opened = datetime.fromtimestamp(NOW - 120, timezone.utc)
    bar = (
        opened,
        datetime.fromtimestamp(NOW - 60, timezone.utc),
        Decimal("12"),
        Decimal("14"),
        Decimal("11"),
        Decimal("13"),
        Decimal("100"),
        STAMP,
        STAMP,
        2,
        "hash",
        [],
    )
    row = event_row(value)
    result = McpStructureRepository(Pool(row, row, [row], [bar])).detail(INSTRUMENT, "1m", CONTEXT, CANDIDATE)
    assert result["chart"]["status"] == "REVISED_CANDLE"
    assert result["chart"]["geometry"]["points"][0]["price"] == 12
    assert result["chart"]["bars"][0]["low"] == "11"
    assert result["first_shape_summary"]["key_levels"]["target_zone"]["price"] == 14
    assert result["shape_summary"]["key_levels"]["target_zone"]["price"] == 20
