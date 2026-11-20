"""Real PostgreSQL/API coverage: screening is a read-only checkpoint projection."""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from apps.api.pattern_studies import build_pattern_study_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.quant.structure_detection import VERSION
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, USER_A, seed_two_tenants


PHASES = ("FORMING", "FORMED", "CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED")


def checkpoint_fixture(now):
    """Times refer exclusively to closed 1h buckets before the current wall clock."""
    cutoff = int(now.timestamp()) // 3600 * 3600
    records = {}
    for index in range(240):
        identifier = hashlib.sha256(f"screening-fixture-{index}".encode()).hexdigest()
        score = (None, 0, .75, 1)[index % 4]
        pattern = "double_top" if index % 2 == 0 else "future_wave_v2"
        phase = PHASES[index % len(PHASES)]
        observed = cutoff - (239 - index) * 3600
        records[identifier] = {
            "id": identifier, "pattern": pattern, "phase": phase,
            "observed_time": observed, "first_observed_time": observed,
            "stages": {},  # No invented original decision or historical probability.
            "latest": {
                "id": identifier, "pattern": pattern, "phase": phase,
                "name": "双顶" if index % 2 == 0 else "未来波浪",
                "group": "反转形态" if index % 2 == 0 else "波浪理论",
                "experimental": bool(index % 2), "confidence": score,
                "signal_direction": ("bullish", "bearish", "neutral")[index % 3],
                "direction": "bullish",  # Must not override signal_direction.
                "start_time": observed - 20 * 3600, "end_time": observed - 3600,
                "confirmed": phase == "CONFIRMED", "points": [], "lines": [],
            },
        }
    assert all(r["observed_time"] <= cutoff <= now.timestamp() for r in records.values())
    return {"last_ts": cutoff - 3600, "step_seconds": 3600, "bars_scanned": 240,
        "records": records, "metadata": {"snapshot_at": now.isoformat()}}


def assert_summary_balanced(payload):
    summary = payload["summary"]
    assert summary["total"] == payload["total"]
    assert summary["scored_count"] + summary["unscored_count"] == summary["total"]
    assert sum(summary["directions"].values()) == summary["total"]
    assert sum(summary["phase_counts"].values()) == summary["total"]
    assert sum(item["count"] for item in summary["patterns"]) == summary["total"]
    assert sum(item["share"] for item in summary["patterns"]) == (1 if summary["total"] else 0)
    assert summary["patterns"] == sorted(summary["patterns"], key=lambda item: (-item["count"], item["pattern"]))
    for item in summary["patterns"]:
        assert sum(item["directions"].values()) == item["count"]
        assert item["share"] == item["count"] / summary["total"]
    # Original global tracking totals remain independent of filters/pagination.
    assert payload["tracked_total"] == 240
    assert payload["phase_counts"] == dict.fromkeys(PHASES, 40)


def test_current_pattern_screening_real_api_role_pagination_and_immutable_checkpoint(migrated_database):
    seed_two_tenants(migrated_database)
    now = datetime.now(timezone.utc)
    fixture = checkpoint_fixture(now)
    with psycopg.connect(migrated_database) as conn:
        conn.execute("""INSERT INTO market_pattern_tracking
            (instrument_id,timeframe,version,checkpoint_json,updated_at,requested_at)
            VALUES(%s,'1h',%s,%s,%s,%s)""", (INSTRUMENT, VERSION, Jsonb(fixture), now, now))
        original = conn.execute("""SELECT checkpoint_json,checkpoint_json::text FROM market_pattern_tracking
            WHERE instrument_id=%s AND timeframe='1h' AND version=%s""", (INSTRUMENT, VERSION)).fetchone()
        journal_before = conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0]
        assert journal_before == 0
    api_pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"), max_size=2)
    api_pool.open()
    try:
        with api_pool.public_transaction() as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == "gatescope_api"
            assert conn.execute("SELECT has_table_privilege(current_user,'market_pattern_tracking','UPDATE')").fetchone()[0] is False
        app = FastAPI()
        app.include_router(build_pattern_study_router(api_pool))

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.auth_session = SimpleNamespace(
                tenant_id=TENANT_A, user_id=USER_A, role=SimpleNamespace(value="VIEWER"))
            return await call_next(request)

        with TestClient(app) as client:
            base = {"instrument_id": str(INSTRUMENT), "timeframe": "1h"}

            def get(**filters):
                response = client.get("/api/v2/patterns/current", params={**base, **filters})
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                payload = response.json()
                assert_summary_balanced(payload)
                return payload

            default = get()
            assert default["total"] == 240 and len(default["items"]) == 96
            assert default["summary"]["scored_count"] == 180
            assert default["summary"]["unscored_count"] == 60
            assert default["summary"]["directions"] == {"bullish": 80, "bearish": 80, "neutral": 80}

            first = get(confidence_min=.75, confidence_max=1)
            second = get(confidence_min=.75, confidence_max=1, offset=96)
            assert first["total"] == second["total"] == 120
            assert len(first["items"]) == 96 and len(second["items"]) == 24
            assert first["has_more"] and not second["has_more"]
            assert first["summary"] == second["summary"]
            assert first["summary"]["scored_count"] == 120 and first["summary"]["unscored_count"] == 0
            assert first["summary"]["directions"] == {"bullish": 40, "bearish": 40, "neutral": 40}
            assert [p["pattern"] for p in first["summary"]["patterns"]] == ["double_top", "future_wave_v2"]
            items = first["items"] + second["items"]
            assert len({item["id"] for item in items}) == 120
            assert {item["phase"] for item in items} == set(PHASES)
            assert all(.75 <= item["confidence"] <= 1 for item in items)
            assert all(item["historical_reference"]["reference_state"] == "STAGE_UNAVAILABLE" for item in items)
            assert all(item["historical_reference"]["sample_count"] is None for item in items)
            assert get(confidence_min=.75, confidence_max=1, offset=192)["summary"] == first["summary"]

            unscored = get(unscored_only="true")
            assert unscored["total"] == unscored["summary"]["unscored_count"] == 60
            assert unscored["summary"]["scored_count"] == 0
            assert all(item["confidence"] is None for item in unscored["items"])
            assert unscored["summary"]["patterns"][0]["score_min"] is None
            assert unscored["summary"]["patterns"][0]["score_max"] is None
            zero = get(confidence_min=0, confidence_max=0)
            one = get(confidence_min=1, confidence_max=1)
            assert zero["total"] == one["total"] == 60
            assert all(item["confidence"] == 0 for item in zero["items"])
            assert all(item["confidence"] == 1 for item in one["items"])
            assert get(confidence_min=0, confidence_max=1)["total"] == 180

            exact = get(pattern="future_wave_v2")
            assert exact["total"] == 120
            assert [item["pattern"] for item in exact["summary"]["patterns"]] == ["future_wave_v2"]
            assert all(item["pattern"] == "future_wave_v2" for item in exact["items"])
            combined = get(pattern="future_wave_v2", family="wave", q="未来", confidence_min=1, phase="CONFIRMED")
            assert combined["total"] == 0  # Odd wave IDs cannot have index % 6 == 2.
            formed = get(phase="CONFIRMED", confidence_min=.75)
            assert formed["total"] == 20 and formed["summary"]["phase_counts"]["CONFIRMED"] == 20
            empty = get(pattern="not_a_catalog_pattern")
            assert empty["items"] == empty["summary"]["patterns"] == []
            assert empty["summary"]["total"] == empty["summary"]["scored_count"] == empty["summary"]["unscored_count"] == 0
            assert not any(empty["summary"]["directions"].values()) and not any(empty["summary"]["phase_counts"].values())

            for invalid in ({"confidence_min": "nan"}, {"confidence_max": "inf"}, {"confidence_min": "-Infinity"},
                            {"confidence_min": "-0.1"}, {"confidence_max": "1.1"},
                            {"confidence_min": ".9", "confidence_max": ".8"},
                            {"unscored_only": "true", "confidence_min": "0"},
                            {"unscored_only": "true", "confidence_max": "1"}):
                response = client.get("/api/v2/patterns/current", params={**base, **invalid})
                assert response.status_code == 422, response.text
                assert "历史 K 线存在缺口" not in response.text
            assert get(confidence_min=.75, confidence_max=1) == first  # Cache/filter isolation.

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with api_pool.public_transaction() as conn:
                conn.execute("""UPDATE market_pattern_tracking SET checkpoint_json=checkpoint_json
                    WHERE instrument_id=%s AND timeframe='1h' AND version=%s""", (INSTRUMENT, VERSION))
        with psycopg.connect(migrated_database) as conn:
            after = conn.execute("""SELECT checkpoint_json,checkpoint_json::text FROM market_pattern_tracking
                WHERE instrument_id=%s AND timeframe='1h' AND version=%s""", (INSTRUMENT, VERSION)).fetchone()
            assert after == original and after[0] == fixture
            assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == journal_before
            assert conn.execute("SELECT count(*) FROM market_structure_stages").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM market_structure_returns").fetchone()[0] == 0
    finally:
        api_pool.close()
