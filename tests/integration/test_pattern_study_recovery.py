"""Slice rollback, same-context retry, and late replay learning with real roles."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from gatescope.adapters import postgres_pattern_studies as studies
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import observe
from tests.integration.test_postgres_rls import seed_two_tenants, INSTRUMENT
from tests.unit.test_structure_lifecycle import candidate


@contextmanager
def replay_fixture(url, monkeypatch):
    seed_two_tenants(url)
    market = PostgresPool(make_conninfo(url, options="-c role=gatescope_market"), max_size=1)
    worker = PostgresPool(make_conninfo(url, options="-c role=gatescope_strategy"), max_size=1)
    market.open()
    worker.open()
    try:
        now = datetime.now(timezone.utc)
        last = int(now.timestamp()) // 3600 * 3600 - 3600
        rows = [(last - (79 - i) * 3600, 100.0, 105.0, 95.0, 101.0, 10.0) for i in range(80)]
        subscription = MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h")
        ingest = PostgresMarketIngestRepository(market)
        ingest.sync_states([subscription])
        ingest.upsert_final_bars(subscription, rows, observed_at=now)
        ingest.mark_history(
            subscription, ingest.history_plan(subscription, now), now, state="SOURCE_LIMIT", delay=900
        )
        monkeypatch.setattr(
            studies,
            "observe",
            lambda bars, index, state, **kwargs: observe(
                bars,
                index,
                state,
                detector=lambda prefix: [candidate(prefix)] if len(prefix) >= 20 else [],
                **kwargs,
            ),
        )
        repo = PatternStudyRepository(worker)
        repo.schedule()
        yield repo, worker, rows
    finally:
        market.close()
        worker.close()


def state(url):
    with psycopg.connect(url) as c:
        row = c.execute(
            "SELECT state,cursor,input_json,checkpoint_json,requested_at,safe_error FROM market_pattern_studies"
        ).fetchone()
        counts = tuple(
            c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("market_pattern_events", "market_structure_stages", "market_structure_returns")
        )
    return row, counts


def schedule_now(url, repo):
    with psycopg.connect(url) as c:
        c.execute("UPDATE market_pattern_studies SET updated_at=now()-interval '16 minutes'")
    repo._scheduled_at = 0
    repo.schedule()


def test_failed_slice_rolls_back_journal_and_preserves_checkpoint_then_resumes(
    migrated_database, monkeypatch
):
    with replay_fixture(migrated_database, monkeypatch) as (repo, worker, rows):
        assert repo.run_slice(max_steps=20)
        before, counts = state(migrated_database)
        assert before[0] == "RUNNING" and before[1] == 20 and counts[0] > 0
        writer = studies.write_journal

        def failed_write(conn, instrument, tf, context, journal):
            writer(conn, instrument, tf, context, journal)
            writer(
                conn,
                instrument,
                tf,
                context,
                [
                    {
                        "candidate_id": "must-rollback",
                        "sequence": 1,
                        "phase": "FORMING",
                        "from_phase": None,
                        "observed_time": rows[20][0] + 3600,
                        "record": {"fixture": True},
                    }
                ],
            )
            raise RuntimeError("fixture calculation failure")

        monkeypatch.setattr(studies, "write_journal", failed_write)
        assert repo.run_slice(max_steps=3)
        failed, after_counts = state(migrated_database)
        assert failed[0] == "FAILED" and failed[5] == "PATTERN_CALCULATION_FAILED"
        assert failed[1:5] == before[1:5] and after_counts == counts
        schedule_now(migrated_database, repo)
        resumed, _ = state(migrated_database)
        assert resumed[0] == "QUEUED" and resumed[1] == 20 and resumed[4] == before[4]
        assert resumed[2]["metadata"]["context_id"] == before[2]["metadata"]["context_id"]
        assert resumed[2]["metadata"]["automatic_resume_attempts"] == 1
        assert resumed[3] == before[3]
        monkeypatch.setattr(studies, "write_journal", writer)
        assert PatternStudyRepository(worker).run_slice(seconds=5, max_steps=80)
        finished, final_counts = state(migrated_database)
        assert finished[0] == "READY" and finished[1] == 80 and finished[4] == before[4]
        assert final_counts[0] > counts[0]
        assert not PatternStudyRepository(worker).run_slice()
        assert state(migrated_database)[1] == final_counts
        with worker.public_transaction() as c:
            assert (
                c.execute(
                    "SELECT count(*) FROM market_pattern_events WHERE candidate_id='must-rollback'"
                ).fetchone()[0]
                == 0
            )


def test_same_context_automatic_retry_is_limited_to_three(migrated_database, monkeypatch):
    with replay_fixture(migrated_database, monkeypatch) as (repo, _, rows):
        repo.run_slice(max_steps=20)
        before, _ = state(migrated_database)

        def fail(*args, **kwargs):
            raise ValueError("PATTERN_STUDY_TOO_DENSE")

        monkeypatch.setattr(studies, "observe", fail)
        for attempt in range(1, 4):
            assert repo.run_slice(max_steps=2)
            schedule_now(migrated_database, repo)
            current, _ = state(migrated_database)
            assert current[0] == "QUEUED" and current[1] == before[1] and current[4] == before[4]
            assert current[2]["metadata"]["automatic_resume_attempts"] == attempt
        assert repo.run_slice(max_steps=2)
        schedule_now(migrated_database, repo)
        exhausted, _ = state(migrated_database)
        assert exhausted[0] == "FAILED" and exhausted[5] == "PATTERN_STUDY_TOO_DENSE"
        assert exhausted[1] == before[1] and exhausted[4] == before[4]
        assert exhausted[2]["metadata"]["automatic_resume_attempts"] == 3
        assert not repo.run_slice()


@pytest.mark.parametrize("reason", ["PATTERN_VERSION_CHANGED", "PATTERN_HISTORY_SHORT"])
def test_incompatible_failed_replay_starts_new_context_without_removing_old_journal(
    migrated_database, monkeypatch, reason
):
    with replay_fixture(migrated_database, monkeypatch) as (repo, _, rows):
        repo.run_slice(max_steps=20)
        before, counts = state(migrated_database)
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_pattern_studies SET state='FAILED',safe_error=%s", (reason,))
        schedule_now(migrated_database, repo)
        queued, retained = state(migrated_database)
        assert queued[0] == "QUEUED" and queued[1] == 0 and queued[2] is None and queued[3] is None
        assert queued[4] > before[4] and retained == counts


def test_tracking_merges_late_replay_samples_at_each_bar_boundary(migrated_database, monkeypatch):
    with replay_fixture(migrated_database, monkeypatch) as (repo, worker, rows):
        first = rows[-2][0]
        second = rows[-1][0]
        cell = "elliott_impulse_bull:FORMING:3"
        initial = {
            "history_seeded": True,
            "step_seconds": 3600,
            "last_ts": first - 3600,
            "last_index": 77,
            "active": [],
            "records": {
                "original": {"phase": "EXPIRED", "last_index": 77, "first": {"forecast": {"price": 123}}}
            },
            "forecast_training": {cell: [[first - 3600, 1.9, "recorded"]]},
        }
        replay = {
            cell: [
                [first - 3600, 2.5, "recorded"],
                [first + 3600, 2.0, "late-replay"],
                [second + 3600, 2.1, "later-replay"],
            ]
        }
        with worker.public_transaction() as c:
            c.execute(
                "INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json) VALUES(%s,'1h',%s,%s)",
                (INSTRUMENT, VERSION, Jsonb(initial)),
            )
            c.execute(
                "UPDATE market_pattern_studies SET checkpoint_json=%s",
                (Jsonb({"last_ts": second, "forecast_training": replay}),),
            )
        seen = []

        def capture(bars, index, checkpoint, **kwargs):
            seen.append(deepcopy(checkpoint["forecast_training"][cell]))
            checkpoint.update(last_ts=bars[index]["ts"], last_index=checkpoint["last_index"] + 1)
            return []

        monkeypatch.setattr(studies, "observe", capture)
        assert repo.track_slice(seconds=5, max_steps=2)
        assert seen[0] == [[first - 3600, 1.9, "recorded"], [first + 3600, 2.0, "late-replay"]]
        assert seen[1] == seen[0] + [[second + 3600, 2.1, "later-replay"]]
        with worker.public_transaction() as c:
            saved = c.execute("SELECT checkpoint_json FROM market_pattern_tracking").fetchone()[0]
        assert saved["records"] == initial["records"] and saved["last_ts"] == second
