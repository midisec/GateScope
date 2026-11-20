from copy import deepcopy

import psycopg

from gatescope.adapters.annual_structure_study import record_reference


def test_terminal_record_reference_reuses_prior_failures_not_its_later_outcome(migrated_database):
    with psycopg.connect(migrated_database) as connection:
        def add(context, candidate, start, end, phase, *, bucket="LOW", entry="FORMED", sampled=True, formed=True):
            connection.execute("""INSERT INTO market_structure_stages
                (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,phase,resolved_time,sampled,formed)
                VALUES(%s,%s,'double_top',%s,%s,%s,%s,%s,%s,%s)""",
                (context, candidate, entry, bucket, start, phase, end, sampled, formed))

        add("replay", "same", 100, 110, "CONFIRMED")
        add("recorded", "same", 100, 120, "INVALIDATED")
        add("replay", "overlap", 105, 115, "CONFIRMED")
        add("recorded", "expired", 130, 140, "EXPIRED")
        add("replay", "win", 150, 160, "CONFIRMED")
        add("replay", "same-close", 170, 200, "CONFIRMED")
        add("replay", "after-entry", 201, 210, "CONFIRMED")
        add("replay", "wrong-bucket", 20, 30, "CONFIRMED", bucket="HIGH")
        add("old-version-context", "old-model", 40, 50, "CONFIRMED")
        add("replay", "current", 60, 70, "CONFIRMED")
        record = {
            "id": "current", "pattern": "double_top", "phase": "CONFIRMED",
            "latest": {"confidence": .99},
            "stages": {"FORMED": {"time": 200, "score_bucket": "LOW", "prediction": {"sample_count": 0}}},
        }
        frozen = deepcopy(record)
        result = record_reference(connection, "replay", record, 500, recorded_context="recorded")
        assert result["sample_count"] == 3 and result["successes"] == 1
        assert result["recorded_count"] == 2 and result["replay_count"] == 1
        assert result["invalidated"] == result["expired"] == 1
        assert result["reference_as_of"] == 200 and result["reference_phase"] == "FORMED"
        assert {item["id"] for item in result["failure_examples"]} == {"same", "expired"}
        assert result["overlap_excluded"] == 1 and record == frozen
        # Later report refreshes cannot make the later success available at the
        # original decision. Terminal outcome changes do not change its cohort.
        record["phase"] = "INVALIDATED"
        later = record_reference(connection, "replay", record, 1000, recorded_context="recorded")
        assert later["sample_count"] == result["sample_count"]
        assert later["successes"] == result["successes"]


def test_forming_denominator_counts_formed_then_failed_and_not_censored(migrated_database):
    with psycopg.connect(migrated_database) as connection:
        for candidate, start, end, phase, formed in [
            ("formed-failed", 10, 20, "INVALIDATED", True),
            ("never-formed", 30, 40, "EXPIRED", False),
            ("confirmed", 50, 60, "CONFIRMED", True),
            ("gap", 70, 80, "CENSORED", False),
        ]:
            connection.execute("""INSERT INTO market_structure_stages
                (context_id,candidate_id,pattern,entry_phase,score_bucket,stage_time,phase,resolved_time,sampled,formed)
                VALUES('replay',%s,'double_top','FORMING','UNSCORED',%s,%s,%s,true,%s)""",
                (candidate, start, phase, end, formed))
        record = {"id": "new", "pattern": "double_top", "phase": "EXPIRED",
                  "stages": {"FORMING": {"time": 100, "score_bucket": "UNSCORED"}}}
        result = record_reference(connection, "replay", record, 500)
        assert result["sample_count"] == 3 and result["successes"] == 1
        assert result["formation"]["sample_count"] == 3 and result["formation"]["successes"] == 2
        assert result["invalidated"] == 1 and result["expired"] == 1
        assert result["recorded_count"] == 0 and result["replay_count"] == 3
