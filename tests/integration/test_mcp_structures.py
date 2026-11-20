"""Run only in the shared, serial migrated PostgreSQL fixture."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres_mcp_structures import McpStructureRepository
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.quant.structure_detection import VERSION
from tests.integration.test_postgres_rls import INSTRUMENT, seed_two_tenants


class McpRolePool:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def public_transaction(self):
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SET ROLE gatescope_mcp")
            conn.commit()
            with conn.transaction():
                yield conn


def _seed(dsn):
    seed_two_tenants(dsn)
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    stamp = datetime.fromtimestamp(now, timezone.utc)
    candidate, context = "a" * 64, context_id(INSTRUMENT, "1m")
    event = {
        "id": candidate,
        "evidence_id": "b" * 64,
        "pattern": "double_bottom",
        "name": "Double bottom",
        "group": "reversal",
        "phase": "FORMING",
        "confidence": 0.8,
        "signal_direction": "neutral",
        "points": [{"time": now - 120, "price": 12, "basis": "LOW"}],
        "lines": [{"from": {"time": now - 120, "price": 12}, "to": {"time": now - 60, "price": 12}}],
        "forecast": {"id": "first-forecast", "kind": "NEXT_PIVOT", "target": {"price": 14}},
        "levels": {"invalid_price": 10},
    }
    record = {
        "id": candidate,
        "pattern": "double_bottom",
        "phase": "FORMING",
        "first": event,
        "latest": deepcopy(event),
        "first_observed_time": now,
        "observed_time": now,
        "first_snapshot_at": stamp.isoformat(),
        "snapshot_at": stamp.isoformat(),
        "source_mode": "RECORDED",
        "stages": {
            "FORMING": {
                "time": now,
                "score_bucket": "HIGH",
                "prediction": {"sample_count": 6, "successes": 2, "probability": 0.3},
            }
        },
    }
    failed = deepcopy(record)
    failed.update(phase="INVALIDATED", observed_time=now + 60)
    failed["latest"].update(phase="INVALIDATED", forecast={"id": "later-forecast", "target": {"price": 20}})
    report = {
        "version": VERSION,
        "bar_count": 1000,
        "to_time": now,
        "from_time": now - 60000,
        "rows": [
            {"pattern": "double_bottom", "phase": "FORMED", "sample_count": 10, "win_rate": 0.6},
            {"pattern": "double_top", "sample_count": 20},
        ],
        "completion_rows": [{"pattern": "double_bottom", "phase": "FORMING", "sample_count": 6}],
        "metadata": {"snapshot_at": stamp.isoformat(), "coverage_ratio": 0.8},
        "catalog": [{"id": "double_bottom", "name": "Double bottom"}],
        "training_samples": ["not-for-export"],
        "cases": ["not-for-export"],
        "series": ["not-for-export"],
    }
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json,updated_at)
            VALUES(%s,'1m',%s,%s,now())""",
            (
                INSTRUMENT,
                VERSION,
                Jsonb({"last_ts": now - 60, "bars_scanned": 1000, "records": {candidate: failed}}),
            ),
        )
        conn.execute(
            """INSERT INTO market_pattern_studies(instrument_id,timeframe,version,state,cursor,total,report_json,requested_at)
            VALUES(%s,'1m',%s,'READY',1000,1000,%s,%s)""",
            (INSTRUMENT, VERSION, Jsonb(report), stamp),
        )
        for sequence, value in enumerate((record, failed), 1):
            conn.execute(
                """INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
                version,source_mode,phase,observed_time,event_json) VALUES(%s,%s,%s,%s,'1m',%s,'RECORDED',%s,%s,%s)""",
                (
                    context,
                    candidate,
                    sequence,
                    INSTRUMENT,
                    VERSION,
                    value["phase"],
                    datetime.fromtimestamp(value["observed_time"], timezone.utc),
                    Jsonb(value),
                ),
            )
        for offset in (180, 120, 60):
            opened = datetime.fromtimestamp(now - offset, timezone.utc)
            closed = datetime.fromtimestamp(now - offset + 60, timezone.utc)
            conn.execute(
                """INSERT INTO bars(venue,market,instrument_id,timeframe,open_time,close_time,
                open,high,low,close,volume,is_final,revision,available_at,payload_hash,created_at)
                VALUES('GATE','FUTURES_USDT',%s,'1m',%s,%s,13,14,12,13,100,true,1,%s,%s,%s)""",
                (INSTRUMENT, opened, closed, closed, str(offset).zfill(64), closed),
            )
        # A later corrected price must not rewrite the original K-line view.
        conn.execute(
            """INSERT INTO bars(venue,market,instrument_id,timeframe,open_time,close_time,
            open,high,low,close,volume,is_final,revision,available_at,payload_hash,created_at)
            VALUES('GATE','FUTURES_USDT',%s,'1m',to_timestamp(%s),to_timestamp(%s),13,14,10,13,100,true,2,
                to_timestamp(%s),%s,to_timestamp(%s))""",
            (INSTRUMENT, now - 120, now - 60, now + 30, "f" * 64, now + 30),
        )
    return context, candidate, now


def test_mcp_structures_actual_role_reads_without_queue_side_effects(migrated_database):
    context, candidate, now = _seed(migrated_database)
    with psycopg.connect(migrated_database) as conn:
        before = conn.execute(
            "SELECT requested_at,updated_at,checkpoint_json FROM market_pattern_tracking"
        ).fetchall()
    repository = McpStructureRepository(McpRolePool(migrated_database))
    current = repository.current(INSTRUMENT, "1m", phase="INVALIDATED", confidence_min=0.7)
    assert current["total"] == 1 and current["items"][0]["phase"] == "INVALIDATED"
    assert current["items"][0]["initial_prediction"]["probability"] == 0.3
    assert current["items"][0]["historical_reference"]["sample_count"] is None
    assert current["data_watermark"] == now
    study = repository.study(INSTRUMENT, "1m", pattern="double_bottom")
    assert len(study["report"]["rows"]) == 1
    assert not {"training_samples", "cases", "series"}.intersection(study["report"])
    events = repository.events(INSTRUMENT, "1m", context=context, phase="INVALIDATED")
    assert len(events["items"]) == 1 and events["items"][0]["sequence"] == 2
    detail = repository.detail(INSTRUMENT, "1m", context, candidate, transition_limit=1, bar_limit=3)
    assert detail["transitions_has_more"] and len(detail["transitions"]) == 1
    assert detail["first_record"]["first"]["forecast"]["id"] == "first-forecast"
    assert detail["latest_geometry"]["forecast"]["id"] == "later-forecast"
    assert detail["chart"]["status"] == "READY"
    assert len(detail["chart"]["bars"]) == 3
    assert all(bar["revision"] == 1 for bar in detail["chart"]["bars"])
    assert detail["chart"]["bars"][1]["low"].startswith("12")
    json.dumps(detail, allow_nan=False)
    with psycopg.connect(migrated_database) as conn:
        assert (
            conn.execute(
                "SELECT requested_at,updated_at,checkpoint_json FROM market_pattern_tracking"
            ).fetchall()
            == before
        )


def test_mcp_structure_archive_scope_and_old_replay_version(migrated_database):
    context, candidate, _ = _seed(migrated_database)
    repository = McpStructureRepository(McpRolePool(migrated_database))
    assert repository.detail(uuid4(), "1m", context, candidate)["status"] == "NOT_FOUND"
    assert repository.detail(INSTRUMENT, "5m", context, candidate)["status"] == "NOT_FOUND"
    assert repository.events(INSTRUMENT, "1m", context=context, source="REPLAY")["status"] == "NOT_FOUND"
    with psycopg.connect(migrated_database) as conn:
        conn.execute(
            """INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
            version,source_mode,phase,observed_time,event_json)
            SELECT %s,candidate_id,sequence,instrument_id,timeframe,'older-model','REPLAY',phase,observed_time,
                event_json || '{"source_mode":"REPLAY"}'::jsonb FROM market_pattern_events WHERE context_id=%s""",
            ("d" * 64, context),
        )
    replay = repository.events(INSTRUMENT, "1m", context="d" * 64, source="REPLAY")
    assert replay["model_version"] == "older-model" and replay["source_mode"] == "REPLAY"
    detail = repository.detail(INSTRUMENT, "1m", "d" * 64, candidate, bar_limit=0)
    assert detail["model_version"] == "older-model" and detail["source_mode"] == "REPLAY"
