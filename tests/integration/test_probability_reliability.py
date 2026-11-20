import psycopg
import pytest

from gatescope.adapters.annual_structure_study import project_journal, report


def test_reliability_single_aggregate_preserves_outcomes_and_excludes_future(migrated_database):
    through = 1_790_000_000
    environment = "se1:UP:NORMAL:NORMAL"
    with psycopg.connect(migrated_database) as connection:
        for index in range(61):
            recent = index < 40 or index == 60
            phase = "INVALIDATED" if 20 <= index < 40 else "CONFIRMED"
            connection.execute("""INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,score_bucket,
              stage_time,phase,resolved_time,sampled,formed,prediction,environment_key)
              VALUES('reliability-test',%s,'double_bottom','FORMING','HIGH',%s,%s,%s,true,true,.8,%s)""",
              (f"case-{index}", through-(10 if recent else 40)*86400, phase,
               through+60 if index == 60 else through-86400,
               environment if recent else None))
        result = report(connection, "reliability-test", {"last_ts": through-60, "step_seconds": 60},
                        {"requested_from": through-365*86400}, 10_000, through-365*86400,
                        environment_key=environment)
    row = next(item for item in result["completion_rows"] if
               (item["pattern"], item["phase"], item["score_bucket"]) == ("double_bottom", "FORMING", "ALL"))
    assert row["sample_count"] == 60 and row["calibration_count"] == 60
    reliability = row["reliability"]
    assert reliability["recent"]["sample_count"] == 40 and reliability["annual"]["sample_count"] == 60
    assert reliability["recent"]["sampled_candidate_count"] == 41
    assert reliability["recent"]["unresolved_count"] == 1 and reliability["recent"]["censored_count"] == 0
    assert abs(reliability["recent"]["brier"]-.34) < 1e-12
    assert reliability["environment_reference"]["matched"]["sample_count"] == 40
    assert reliability["environment_reference"]["legacy_unlabelled_count"] == 20
    assert reliability["current_regime_status"] == "READY"
    assert all("reliability" not in item for item in result["completion_rows"] if item["score_bucket"] != "ALL")


def test_environment_projection_is_onset_only_and_never_backfills_or_relabels(migrated_database):
    stage = {"score_bucket": "HIGH", "time": 100, "sampled": True, "prediction": {"probability": .8}}
    record = {"id": "candidate", "pattern": "double_bottom", "phase": "FORMING", "stages": {"FORMING": stage}, "first": {}}
    with psycopg.connect(migrated_database) as connection:
        assert connection.execute("""SELECT convalidated FROM pg_constraint
            WHERE conrelid='market_structure_stages'::regclass
              AND conname='market_structure_stage_environment_key_valid'""").fetchone()==(False,)
        project_journal(connection, "onset-only", [{"record": record, "sequence": 1}])
        stage["environment_key"] = "se1:UP:NORMAL:NORMAL"
        record.update(phase="INVALIDATED", resolved_time=200)
        project_journal(connection, "onset-only", [{"record": record, "sequence": 2}])
        assert connection.execute("SELECT environment_key,phase FROM market_structure_stages WHERE context_id='onset-only'").fetchone() == (None, "INVALIDATED")
        record["id"] = "new-candidate"
        project_journal(connection, "onset-only", [{"record": record, "sequence": 1}])
        stage["environment_key"] = "se1:DOWN:HIGH:LOW"
        project_journal(connection, "onset-only", [{"record": record, "sequence": 2}])
        assert connection.execute("SELECT environment_key FROM market_structure_stages WHERE candidate_id='new-candidate'").fetchone()[0] == "se1:UP:NORMAL:NORMAL"
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute("UPDATE market_structure_stages SET environment_key='invented' WHERE candidate_id='new-candidate'")
