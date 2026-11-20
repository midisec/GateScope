from copy import deepcopy

import pytest

from gatescope.adapters import annual_structure_study as annual


class Cursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value


class Connection:
    def __init__(self, ready=True, counts=None):
        self.ready = ready
        self.counts = counts or (3, 1, 2, 1, 1, 2, 0, [])
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return Cursor((1,) if self.ready else None) if sql.startswith("SELECT 1 ") else Cursor(self.counts)


def record(phase="CONFIRMED"):
    return {
        "id": "current", "pattern": "double_top", "phase": phase,
        "latest": {"phase": phase, "confidence": .99},
        "stages": {
            "FORMING": {"time": 900, "score_bucket": "UNSCORED", "prediction": {"sample_count": 0}},
            "FORMED": {"time": 1000, "score_bucket": "LOW", "prediction": {"sample_count": 0}},
        },
    }


@pytest.mark.parametrize("phase", ["FORMED", "CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED"])
def test_record_uses_original_formed_entry_and_score_not_current_terminal_state(phase):
    connection, source = Connection(), record(phase)
    before = deepcopy(source)
    result = annual.record_reference(connection, "replay", source, 2000, recorded_context="recorded")
    assert result["reference_phase"] == "FORMED" and result["reference_as_of"] == 1000
    assert result["reference_score_bucket"] == "LOW" and result["sample_count"] == 3
    assert result["successes"] == 1 and result["invalidated"] == result["expired"] == 1
    assert result["reference_state"] == "READY" and result["reference_is_live_prediction"] is False
    assert result["formation"] is None and source == before
    params = connection.calls[-1][1]
    assert params[:7] == (["replay", "recorded"], "double_top", "FORMED", "LOW", 1000, 1000 - 365 * 86400, "current")


def test_forming_only_record_keeps_formation_denominator_and_original_unscored_bucket():
    source = record("EXPIRED")
    source["stages"].pop("FORMED")
    result = annual.record_reference(Connection(), "replay", source, 2000, recorded_context="recorded")
    assert result["reference_phase"] == "FORMING" and result["reference_as_of"] == 900
    assert result["formation"]["sample_count"] == 3 and result["formation"]["successes"] == 2
    assert result["reference_score_bucket"] == "UNSCORED"


@pytest.mark.parametrize("change", [
    lambda r: r.update(stages={}),
    lambda r: r["stages"]["FORMED"].pop("time"),
    lambda r: r["stages"]["FORMED"].update(time=True),
    lambda r: r["stages"]["FORMED"].update(time=3000),
    lambda r: r["stages"]["FORMED"].update(score_bucket="ALL"),
    lambda r: r["stages"]["FORMED"].update(score_bucket=None),
])
def test_missing_or_invalid_original_stage_is_na_without_querying_or_guessing(change):
    connection, source = Connection(), record()
    change(source)
    result = annual.record_reference(connection, "replay", source, 2000, recorded_context="recorded")
    assert result["reference_state"] == "STAGE_UNAVAILABLE"
    assert result["sample_count"] is None and result["probability"] is None
    assert not connection.calls


def test_projection_not_ready_is_na_but_a_real_empty_cohort_is_zero():
    pending = annual.record_reference(Connection(ready=False), "replay", record(), 2000)
    assert pending["reference_state"] == "HISTORY_NOT_READY" and pending["sample_count"] is None
    empty = annual.record_reference(Connection(counts=(0, 0, 0, 0, 0, 0, 0, [])), "replay", record(), 2000)
    assert empty["reference_state"] == "NO_MATCHING_SAMPLES" and empty["sample_count"] == 0
    assert empty["probability"] is None


def test_missing_context_has_no_query_and_no_fake_zero():
    connection = Connection()
    result = annual.record_reference(connection, None, record(), 2000)
    assert result["reference_state"] == "HISTORY_NOT_READY" and not connection.calls
    assert result["recorded_count"] is None and result["replay_count"] is None


def test_existing_engine_reference_does_not_silently_change_terminal_behavior():
    connection = Connection()
    result = annual.reference(connection, "replay", {"phase": "CONFIRMED"}, 2000, recorded_context="recorded")
    assert result["sample_count"] == 0 and not connection.calls


def test_display_catalog_identifies_eight_forming_rules_without_changing_detector_catalog():
    original = deepcopy(annual.catalog())
    rendered = annual.reference_catalog()
    assert len(rendered) == len(original) == 28
    assert {row["id"] for row in rendered if row["supports_forming"]} == {
        "double_top", "double_bottom", "elliott_impulse_bull", "elliott_impulse_bear",
        "elliott_correction_bull", "elliott_correction_bear", "elliott_cycle_bull", "elliott_cycle_bear",
    }
    assert annual.catalog() == original
    assert all("supports_forming" not in row for row in original)
