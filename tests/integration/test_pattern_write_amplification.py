"""Actual workload roles: skip physical no-op updates without losing evidence."""
from copy import deepcopy
from datetime import datetime, timezone

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.adapters.postgres import PostgresPool
from gatescope.quant.structure_detection import VERSION
from tests.integration.test_pattern_study_recovery import replay_fixture
from tests.integration.test_postgres_rls import INSTRUMENT


def test_stage_and_return_identical_upserts_preserve_row_versions_and_frozen_inputs(migrated_database):
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"), max_size=1)
    pool.open()
    stage = {"score_bucket": "HIGH", "time": 100, "sampled": True, "prediction": {"probability": .8},
             "environment_key": "se1:UP:NORMAL:NORMAL"}
    record = {"id": "candidate", "pattern": "double_bottom", "phase": "FORMING", "stages": {"FORMING": stage},
              "first": {"forecast": {"target": {"price": 99.123456789}, "issued_at": 100}}}
    sample = {"evidence_id": "evidence", "pattern": "double_bottom", "phase": "FORMING", "horizon": 5,
              "observed_time": 100, "status": "PENDING", "side": -1}

    def project():
        with pool.public_transaction() as conn:
            annual.project_journal(conn, "write-budget", [{"record": record, "sequence": 1}])
            annual.project_returns(conn, "write-budget", [sample])

    def saved():
        with pool.public_transaction() as conn:
            return (
                conn.execute("SELECT xmin::text,to_jsonb(s) FROM market_structure_stages s WHERE context_id='write-budget'").fetchone(),
                conn.execute("SELECT xmin::text,to_jsonb(r) FROM market_structure_returns r WHERE context_id='write-budget'").fetchone(),
            )

    try:
        project()
        original = saved()
        # Repeated unchanged events must not manufacture new heap/index versions.
        for _ in range(3):
            project()
        assert saved() == original
        # Unchangeable entry fields are intentionally excluded from conflict updates.
        stage.update(score_bucket="LOW", time=999, sampled=False, prediction={"probability": .2},
                     environment_key="se1:DOWN:HIGH:LOW")
        record["first"]["forecast"]["target"]["price"] = 12345
        sample.update(pattern="double_top", phase="FORMED", observed_time=999, side=1)
        project()
        assert saved() == original

        record.update(phase="INVALIDATED", resolved_time=200, forecast_outcome={"status": "NOT_REACHED", "target_price": 99.123456789})
        sample.update(status="COMPLETE", entry_time=100, exit_time=200, net_return=-.02, mfe=0., mae=-.03)
        project()
        changed = saved()
        assert changed[0][0] != original[0][0] and changed[1][0] != original[1][0]
        assert changed[0][1]["phase"] == "INVALIDATED" and changed[0][1]["outcome_json"]["status"] == "NOT_REACHED"
        assert changed[1][1]["net_return"] == -.02 and changed[1][1]["mfe"] == 0
        for field in ("prediction", "forecast_json", "sampled", "stage_time", "score_bucket", "environment_key"):
            assert changed[0][1][field] == original[0][1][field]
        for field in ("pattern", "phase", "observed_time", "side"):
            assert changed[1][1][field] == original[1][1][field]
        project()
        assert saved() == changed
        # Nullable outcomes and exact zero losses still persist real changes.
        record["forecast_outcome"] = None
        sample.update(net_return=0., mae=0.)
        project()
        final = saved()
        assert final[0][1]["outcome_json"] is None and final[1][1]["net_return"] == 0
        assert final[0][0] != changed[0][0] and final[1][0] != changed[1][0]
        project()
        assert saved() == final
    finally:
        pool.close()


def test_idle_tracking_does_not_touch_checkpoint_column_or_import_future_learning(migrated_database, monkeypatch):
    with replay_fixture(migrated_database, monkeypatch) as (repo, worker, rows):
        now = datetime.now(timezone.utc)
        initial = {"last_ts": rows[-1][0], "last_index": 79, "step_seconds": 3600,
                   "records": {}, "active": [], "metadata": {"snapshot_at": "frozen-at-original-close"}}
        with worker.public_transaction() as conn:
            conn.execute("""INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json,safe_error)
                            VALUES(%s,'1h',%s,%s,'PATTERN_PREVIOUS_FAILURE')""", (INSTRUMENT, VERSION, Jsonb(initial)))
            conn.execute("UPDATE market_pattern_studies SET checkpoint_json=%s", (Jsonb({
                "last_ts": rows[-1][0], "training": {"new-replay": [100, 90]},
                "forecast_training": {"wave": [[rows[-1][0], 2., "new-history"]]},
            }),))
        # Test-only column trigger proves the large JSON column isn't even assigned,
        # stronger than comparing equal JSON or relying on asynchronous stats counters.
        with psycopg.connect(migrated_database) as conn:
            conn.execute("""CREATE FUNCTION public.test_forbid_tracking_checkpoint_write() RETURNS trigger
                            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'unexpected checkpoint write'; END; $$""")
            conn.execute("""CREATE TRIGGER test_forbid_tracking_checkpoint_write BEFORE UPDATE OF checkpoint_json
                            ON market_pattern_tracking FOR EACH ROW EXECUTE FUNCTION public.test_forbid_tracking_checkpoint_write()""")
        try:
            for _ in range(2):
                with worker.public_transaction() as conn:
                    conn.execute("UPDATE market_pattern_tracking SET updated_at='epoch'")
                assert repo.track_slice(seconds=5)
            with worker.public_transaction() as conn:
                saved = conn.execute("SELECT checkpoint_json,safe_error,updated_at FROM market_pattern_tracking").fetchone()
            assert saved[0] == initial and saved[1] == "PATTERN_PREVIOUS_FAILURE" and saved[2] >= now
        finally:
            with psycopg.connect(migrated_database) as conn:
                conn.execute("DROP TRIGGER test_forbid_tracking_checkpoint_write ON market_pattern_tracking")
                conn.execute("DROP FUNCTION public.test_forbid_tracking_checkpoint_write()")


def test_failed_new_close_keeps_original_checkpoint_and_retry_can_progress(migrated_database, monkeypatch):
    with replay_fixture(migrated_database, monkeypatch) as (repo, worker, rows):
        initial = {"last_ts": rows[-2][0], "last_index": 78, "step_seconds": 3600,
                   "records": {}, "active": [], "metadata": {"snapshot_at": "original"}}
        with worker.public_transaction() as conn:
            conn.execute("INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json) VALUES(%s,'1h',%s,%s)",
                         (INSTRUMENT, VERSION, Jsonb(initial)))
            conn.execute("UPDATE market_pattern_studies SET checkpoint_json=%s", (Jsonb({
                "last_ts": rows[-3][0], "training": {"replay": [20, 12]},
                "forecast_training": {"wave": [[rows[-3][0], 2., "past"]]},
            }),))

        def fail(bars, index, state, **kwargs):
            assert state["history_seeded"] and state["training"] == {"replay": [20, 12]}
            state["records"]["must-not-persist"] = {"phase": "FORMED"}
            raise ValueError("PATTERN_CALCULATION_FAILED")

        monkeypatch.setattr(studies, "observe", fail)
        assert repo.track_slice(seconds=5)
        with worker.public_transaction() as conn:
            saved = conn.execute("SELECT checkpoint_json,safe_error FROM market_pattern_tracking").fetchone()
            assert saved == (initial, "PATTERN_CALCULATION_FAILED")
            conn.execute("UPDATE market_pattern_tracking SET updated_at='epoch'")
        seen = []

        def succeed(bars, index, state, **kwargs):
            seen.append(deepcopy(state))
            state.update(last_ts=bars[index]["ts"], last_index=79)
            return []

        monkeypatch.setattr(studies, "observe", succeed)
        assert repo.track_slice(seconds=5)
        with worker.public_transaction() as conn:
            saved = conn.execute("SELECT checkpoint_json,safe_error FROM market_pattern_tracking").fetchone()
        assert len(seen) == 1 and seen[0]["history_seeded"]
        assert saved[0]["last_ts"] == rows[-1][0] and saved[1] is None
        assert saved[0]["records"] == initial["records"]
