import copy
import json
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.domain.current_patterns import (
    InvalidCurrentPatternFilter,
    filter_current_records,
    summarize_current_records,
    validate_confidence_filter,
)


INSTRUMENT = UUID(int=1)


def candidate(index, score=None, *, pattern="double_top", phase="FORMING", direction="neutral", wave=False):
    return {
        "id": str(index), "pattern": pattern, "phase": phase, "observed_time": index,
        "first_observed_time": index, "stages": {},
        "latest": {"id": str(index), "pattern": pattern, "name": f"形态 {pattern}",
            "group": "波浪" if wave else "反转", "experimental": wave,
            "confidence": score, "signal_direction": direction, "direction": "bullish"},
    }


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Pool:
    def __init__(self, records):
        self.state = {"last_ts": 1000, "records": {r["id"]: r for r in records}}
        self.transactions = 0
        self.statements = []

    @contextmanager
    def public_transaction(self):
        self.transactions += 1
        yield self

    def execute(self, query, args):
        self.statements.append(query)
        if "SELECT requested_at" in query:
            return Result((datetime(2026, 9, 13, tzinfo=timezone.utc), "READY", 8760, 8760, 1000))
        if "checkpoint_json" in query:
            return Result((self.state, None))
        return Result(None)


def test_filter_and_summary_cover_more_than_96_before_pagination_with_page_only_enrichment(monkeypatch):
    records = [candidate(i, .9 if i < 120 else .1 if i < 200 else None,
        pattern="double_top" if i % 2 == 0 else "custom_v2", wave=bool(i % 2),
        direction=("bullish", "bearish", "neutral")[i % 3]) for i in range(220)]
    pool = Pool(records)
    original = copy.deepcopy(pool.state)
    enriched = []

    def reference(conn, context, record, *args, **kwargs):
        enriched.append(record["id"])
        return {"sample_count": 0, "rate": None}

    monkeypatch.setattr(annual, "record_reference", reference)
    repo = PatternStudyRepository(pool)
    first = repo.current(INSTRUMENT, "1h", confidence_min=.8)
    second = repo.current(INSTRUMENT, "1h", confidence_min=.8, offset=96)
    assert first["total"] == second["total"] == 120
    assert first["tracked_total"] == 220 and first["phase_counts"]["FORMING"] == 220
    assert len(first["items"]) == 96 and len(second["items"]) == 24
    assert first["has_more"] and not second["has_more"]
    assert first["summary"] == second["summary"]
    summary = first["summary"]
    assert (summary["total"], summary["scored_count"], summary["unscored_count"]) == (120, 120, 0)
    assert summary["directions"] == {"bullish": 40, "bearish": 40, "neutral": 40}
    assert [(p["pattern"], p["count"], p["share"]) for p in summary["patterns"]] == [
        ("custom_v2", 60, .5), ("double_top", 60, .5)]
    assert {p["family"] for p in summary["patterns"]} == {"wave", "chart"}
    assert all(p["score_min"] == p["score_max"] == .9 for p in summary["patterns"])
    assert len(enriched) == len(set(enriched)) == 120
    assert len(pool.statements) == 6  # Only existing tracking/checkpoint/report queries.
    assert pool.state == original


@pytest.mark.parametrize("options,expected", [
    ({}, list(range(11))),
    ({"confidence_min": 0}, [1, 2, 3]),
    ({"confidence_max": 1}, [1, 2, 3]),
    ({"confidence_min": 0, "confidence_max": 0}, [1]),
    ({"confidence_min": .5, "confidence_max": .5}, [2]),
    ({"confidence_min": 1, "confidence_max": 1}, [3]),
    ({"unscored_only": True}, [0, 4, 5, 6, 7, 8, 9, 10]),
])
def test_missing_zero_endpoints_and_nonfinite_scores(options, expected):
    scores = [None, 0, .5, 1, math.nan, math.inf, -math.inf, -1, 2, True, "0.5"]
    records = [candidate(i, value) for i, value in enumerate(scores)]
    original = json.dumps(records)
    filtered = filter_current_records(records, **options)
    assert [int(r["id"]) for r in filtered] == expected
    assert json.dumps(records) == original
    summary = summarize_current_records(filtered)
    assert summary["total"] == summary["scored_count"] + summary["unscored_count"]
    json.dumps(summary, allow_nan=False)


def test_filters_intersect_existing_phase_family_query_and_exact_unknown_pattern():
    records = [
        candidate(0, .8, pattern="custom_v2", wave=True, phase="CONFIRMED"),
        candidate(1, .8, pattern="custom_v2", wave=False, phase="CONFIRMED"),
        candidate(2, .8, pattern="custom_v2", wave=True, phase="FORMING"),
        candidate(3, .8, pattern="custom_v20", wave=True, phase="CONFIRMED"),
        candidate(4, .4, pattern="custom_v2", wave=True, phase="CONFIRMED"),
    ]
    result = filter_current_records(records, phase="CONFIRMED", family="wave", query=" CUSTOM_V2 ",
        pattern="custom_v2", confidence_min=.8, confidence_max=.8)
    assert [r["id"] for r in result] == ["0"]


def test_summary_uses_signal_direction_and_record_pattern_before_latest_fallback():
    records = [candidate(0, 0, direction="bearish"), candidate(1, 1, pattern="ignored"),
        candidate(2, None, pattern="z_future", direction="unexpected"), candidate(3, .5, pattern="a_future", direction="bullish")]
    records[0]["latest"]["pattern"] = "not_authoritative"
    records[1].pop("pattern")
    records[1]["latest"]["pattern"] = "double_top"
    records[2]["latest"].pop("name")
    records[2]["latest"].pop("group")
    summary = summarize_current_records(records)
    assert summary["directions"] == {"bullish": 1, "bearish": 1, "neutral": 2}
    assert [p["pattern"] for p in summary["patterns"]] == ["double_top", "a_future", "z_future"]
    assert summary["patterns"][0]["score_min"] == 0
    assert summary["patterns"][0]["score_max"] == 1
    assert summary["patterns"][-1]["name"] == "z_future"
    assert summary["patterns"][-1]["group"] == "未分类"
    assert summary["patterns"][-1]["score_min"] is summary["patterns"][-1]["score_max"] is None
    assert sum(item["share"] for item in summary["patterns"]) == 1


def test_empty_summary_has_zero_counts_and_no_invented_probability():
    summary = summarize_current_records([])
    assert summary == {"total": 0, "scored_count": 0, "unscored_count": 0,
        "directions": {"bullish": 0, "bearish": 0, "neutral": 0},
        "phase_counts": {phase: 0 for phase in ("FORMING", "FORMED", "CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED")},
        "patterns": []}


def test_cache_key_separates_each_new_filter_and_nonfinite_response_is_safe(monkeypatch):
    monkeypatch.setattr(annual, "record_reference", lambda *a, **kw: {})
    pool = Pool([candidate(0, None), candidate(1, 0), candidate(2, 1, pattern="custom_v2"), candidate(3, math.nan)])
    before = json.dumps(pool.state)
    repo = PatternStudyRepository(pool)
    options = [{}, {"confidence_min": 0}, {"confidence_min": 1}, {"confidence_max": 0},
        {"confidence_max": 1}, {"unscored_only": True}, {"pattern": "custom_v2"},
        {"pattern": "missing"}, {"confidence_min": 0, "confidence_max": 1},
        {"confidence_min": 1, "offset": 96}]
    expected = [4, 2, 1, 1, 2, 2, 1, 0, 2, 1]
    results = [repo.current(INSTRUMENT, "1h", **filters) for filters in options]
    assert [r["total"] for r in results] == expected
    assert pool.transactions == len(options)
    for filters, previous in zip(options, results, strict=True):
        assert repo.current(INSTRUMENT, "1h", **filters) is previous
        json.dumps(previous, allow_nan=False)
    assert pool.transactions == len(options)
    assert results[-1]["summary"]["total"] == 1 and results[-1]["items"] == []
    assert results[7]["summary"]["total"] == 0 and results[7]["summary"]["patterns"] == []
    assert all(r["tracked_total"] == 4 and r["phase_counts"]["FORMING"] == 4 for r in results)
    assert json.dumps(pool.state) == before


@pytest.mark.parametrize("minimum,maximum,only", [
    (math.nan, None, False), (None, math.inf, False), (-math.inf, None, False),
    (-.1, None, False), (None, 1.1, False), (.8, .7, False),
    (0, None, True), (None, 1, True), (True, None, False), ("0", None, False),
])
def test_invalid_filters_fail_before_any_database_call(minimum, maximum, only):
    with pytest.raises(InvalidCurrentPatternFilter):
        validate_confidence_filter(minimum, maximum, only)
    pool = Pool([])
    with pytest.raises(InvalidCurrentPatternFilter):
        PatternStudyRepository(pool).current(INSTRUMENT, "1h", confidence_min=minimum,
            confidence_max=maximum, unscored_only=only)
    assert pool.transactions == 0
