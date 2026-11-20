"""Lossless physical repacking preserves facts, failure counts and role bounds."""

import hashlib
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gatescope.quant.structure_archive_codec import (
    canonical_record,
    pack_structure_record,
    unpack_structure_record,
)
from tests.integration.test_postgres_rls import INSTRUMENT, seed_two_tenants
from tests.unit.test_structure_archive_codec import record

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from compact_structure_storage import compact_stage, repack_event  # noqa: E402


def test_repack_roundtrip_immutability_permissions_and_comparison(migrated_database):
    seed_two_tenants(migrated_database)
    value = record()
    value["first"]["forecast"]["condition"] = "".join(
        hashlib.sha256(str(i).encode()).hexdigest() for i in range(400)
    )
    context = "c" * 64
    candidate = value["id"]
    with psycopg.connect(migrated_database) as conn:
        conn.execute(
            """INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
          version,source_mode,phase,from_phase,observed_time,event_json)
          VALUES(%s,%s,3,%s,'1h','structure-study-3.2','REPLAY','INVALIDATED','FORMING',to_timestamp(200),%s)""",
            (context, candidate, INSTRUMENT, Jsonb(value)),
        )
        original = conn.execute(
            "SELECT context_id,candidate_id,sequence,event_json,pg_column_size(event_json) FROM market_pattern_events"
        ).fetchone()
        assert repack_event(conn, original, apply=False) > 0
        assert conn.execute("SELECT event_blob IS NULL FROM market_pattern_events").fetchone()[0]
        assert repack_event(conn, original, apply=True) > 0
        stored = conn.execute(
            "SELECT gatescope_security.structure_event_record(event_json,event_blob) FROM market_pattern_events"
        ).fetchone()[0]
        assert unpack_structure_record(stored) == value
        assert conn.execute(
            "SELECT phase,event_json->>'pattern',event_json->'latest'->>'name' FROM market_pattern_events"
        ).fetchone() == ("INVALIDATED", "double_bottom", "双底")
        assert stored["_archive"]["sha256"] == hashlib.sha256(canonical_record(value)).hexdigest()
        assert repack_event(conn, original, apply=True) == 0
        forecast = value["first"]["forecast"]
        conn.execute(
            """INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,score_bucket,
          stage_time,phase,resolved_time,sampled,formed,prediction,forecast_json,outcome_json)
          VALUES(%s,%s,'double_bottom','FORMING','HIGH',100,'INVALIDATED',200,true,false,.61,%s,'{"status":"NOT_REACHED"}')""",
            (context, candidate, Jsonb(forecast)),
        )
        row = conn.execute(
            "SELECT context_id,candidate_id,entry_phase,forecast_json,pg_column_size(forecast_json) FROM market_structure_stages"
        ).fetchone()
        assert compact_stage(conn, row, apply=True) > 0
        result = conn.execute(
            "SELECT phase,prediction,sampled,formed,forecast_json,outcome_json FROM market_structure_stages"
        ).fetchone()
        assert result[:4] == ("INVALIDATED", 0.61, True, False)
        assert result[4]["target"]["price"] == forecast["target"]["price"]
        assert result[4]["_evidence"]["sequence"] == 3
        assert result[5] == {"status": "NOT_REACHED"}
    for role in (
        "gatescope_api",
        "gatescope_mcp",
        "gatescope_strategy",
        "gatescope_notification",
        "gatescope_paper",
    ):
        with psycopg.connect(migrated_database) as conn:
            conn.execute(f"SET LOCAL ROLE {role}")
            assert conn.execute(
                "SELECT has_function_privilege(current_user,'gatescope_security.repack_structure_event(text,text,integer,jsonb,jsonb,bytea)','EXECUTE')"
            ).fetchone() == (False,)
            if role != "gatescope_paper":
                assert (
                    unpack_structure_record(
                        conn.execute(
                            "SELECT gatescope_security.structure_event_record(event_json,event_blob) FROM market_pattern_events"
                        ).fetchone()[0]
                    )
                    == value
                )
    with psycopg.connect(migrated_database) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE market_pattern_events SET phase='CONFIRMED'")
    with psycopg.connect(migrated_database) as conn:
        conn.execute("SET LOCAL ROLE gatescope_strategy")
        conn.execute("SELECT set_config('gatescope.structure_repack','verified-restore-v1',true)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE market_pattern_events SET event_blob=NULL")


def test_projection_mismatch_rejected_and_no_failed_event_lost(migrated_database):
    seed_two_tenants(migrated_database)
    value = record()
    header, body = pack_structure_record(value)
    header["phase"] = "CONFIRMED"
    with psycopg.connect(migrated_database) as conn:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            conn.execute(
                "SELECT gatescope_security.repack_structure_event(%s,%s,3,%s,%s,%s)",
                ("c" * 64, value["id"], Jsonb(value), Jsonb(header), body),
            )
