from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_coinglass import PostgresCoinglassRepository
from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.quant.structure_detection import VERSION
from tests.integration.test_coinglass_web_configuration import _crypto_pair, API_KEY
from tests.integration.test_postgres_rls import (
    seed_two_tenants,
    TENANT_A,
    TENANT_B,
    USER_A,
    USER_B,
    INSTRUMENT,
)


def test_owner_cache_queue_and_immutable_review(migrated_database):
    seed_two_tenants(migrated_database)
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repo = PostgresCoinglassContextRepository(pool)
        assert repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")["status"] == "NO_CONFIG"
        encryptor, _ = _crypto_pair()
        config, _ = PostgresCoinglassRepository(pool).save_and_enqueue(
            TENANT_A, USER_A, api_key=API_KEY, test_symbol="BTC", encryptor=encryptor
        )
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE coinglass_configs SET status='ACTIVE' WHERE id=%s", (config.id,))
        assert repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")["status"] == "NOT_CACHED"
        assert repo.get(TENANT_A, USER_A, INSTRUMENT, "1m", request=True)["status"] == "QUEUED"
        job = repo.claim()
        assert job and job["instrument"]["venue"] == "GATE"
        assert repo.claim() is None  # lease suppresses duplicate work
        now = int(datetime.now(timezone.utc).timestamp())
        repo.complete(
            job, {"status": "PARTIAL", "fetched_at": now, "series": [], "match": {"status": "EXACT"}}
        )
        assert repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")["status"] == "PARTIAL"
        assert isinstance(repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")["expires_at"], int)
        assert repo.get(TENANT_A, USER_B, INSTRUMENT, "1m")["status"] == "NO_CONFIG"
        assert repo.get(TENANT_B, USER_A, INSTRUMENT, "1m")["status"] == "NO_CONFIG"
        candidate, evidence = "a" * 64, "b" * 64
        event = {
            "id": candidate,
            "evidence_id": evidence,
            "phase": "FORMING",
            "signal_direction": "neutral",
            "forecast": {"kind": "NEXT_PIVOT"},
            "levels": {},
        }
        with psycopg.connect(migrated_database) as conn:
            conn.execute(
                "SELECT gatescope_security.request_pattern_tracking(%s,'1m',%s)", (INSTRUMENT, VERSION)
            )
            conn.execute(
                "UPDATE market_pattern_tracking SET checkpoint_json=%s WHERE instrument_id=%s AND timeframe='1m'",
                (Jsonb({"last_ts": now - 60, "records": {candidate: {"latest": event}}}), INSTRUMENT),
            )
        review = repo.review(
            TENANT_A, USER_A, INSTRUMENT, "1m", context_id(INSTRUMENT, "1m"), candidate, evidence
        )
        assert review["review"]["original"]["forecast"]["kind"] == "NEXT_PIVOT"
        assert review["review"]["adjusted_win_rate"] is None
        assert "SNAPSHOT_STALE" not in review["review"]["reason_codes"]
        assert repo.review_detail(TENANT_A, USER_A, review["review_id"])
        # Superuser bypasses RLS; actual workload isolation is checked below.
        refreshed = repo.review(
            TENANT_A, USER_A, INSTRUMENT, "1m", context_id(INSTRUMENT, "1m"), candidate, "c" * 64
        )
        assert refreshed["review"]["evidence_changed"] is True
        legacy = repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")
        legacy["expires_at"] += 0.75
        with psycopg.connect(migrated_database) as conn:
            conn.execute(
                "UPDATE coinglass_chart_cache SET payload=%s WHERE instrument_id=%s AND timeframe='1m'",
                (Jsonb(legacy), INSTRUMENT),
            )
        compatible = repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")
        assert isinstance(compatible["expires_at"], int)
        assert compatible["expires_at"] == int(legacy["expires_at"])
        assert compatible == repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")
        assert repo.review_detail(TENANT_A, USER_A, review["review_id"]) == review
        with pytest.raises(ValueError, match="PATTERN_EVIDENCE_CHANGED"):
            repo.review(
                TENANT_A,
                USER_A,
                INSTRUMENT,
                "1m",
                context_id(INSTRUMENT, "1m"),
                candidate,
                "c" * 64,
                scenario={"entry": 1, "target_lower": 2, "target_upper": 3},
            )
        with psycopg.connect(migrated_database) as conn:
            conn.execute("SET ROLE gatescope_mcp")
            conn.execute("SELECT set_config('app.tenant_id',%s,true)", (str(TENANT_A),))
            conn.execute("SELECT set_config('app.subject_id',%s,true)", (str(USER_A),))
            assert conn.execute("SELECT count(*) FROM coinglass_chart_cache").fetchone() == (1,)
            assert conn.execute(
                "SELECT * FROM gatescope_security.coinglass_chart_access(%s,%s)", (TENANT_A, USER_A)
            ).fetchone()
            conn.execute("SELECT * FROM market_sync_state LIMIT 1")
            conn.execute("SELECT * FROM market_live_candles LIMIT 1")
            conn.execute("SELECT set_config('app.subject_id',%s,true)", (str(USER_B),))
            assert conn.execute("SELECT count(*) FROM coinglass_chart_cache").fetchone() == (0,)
            assert (
                conn.execute(
                    "SELECT * FROM gatescope_security.coinglass_chart_access(%s,%s)", (TENANT_A, USER_A)
                ).fetchone()
                is None
            )
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute("SELECT secret_ciphertext FROM coinglass_configs")
            conn.execute("RESET ROLE")
            conn.execute("SET ROLE gatescope_api")
            assert conn.execute("SELECT count(*) FROM coinglass_pattern_reviews").fetchone() == (0,)
            conn.execute("SELECT set_config('app.subject_id',%s,true)", (str(USER_A),))
            assert conn.execute("SELECT count(*) FROM coinglass_pattern_reviews").fetchone() == (2,)
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute("UPDATE coinglass_pattern_reviews SET payload='{}'")
            with pytest.raises(InsufficientPrivilege), conn.transaction():
                conn.execute("DELETE FROM coinglass_pattern_reviews")
        with psycopg.connect(migrated_database) as conn:
            watch = uuid4()
            conn.execute(
                "INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at) VALUES(%s,%s,%s,'CG defaults',now(),now())",
                (watch, TENANT_A, USER_A),
            )
            conn.execute(
                "INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at) VALUES(%s,%s,%s,%s,0,'15m',now(),now())",
                (uuid4(), TENANT_A, watch, INSTRUMENT),
            )
        repo.seed()
        with psycopg.connect(migrated_database) as conn:
            assert conn.execute("SELECT count(*) FROM coinglass_chart_cache").fetchone() == (2,)
            conn.execute(
                "INSERT INTO coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version,requested_at,due_at) VALUES(%s,%s,%s,'4h',%s,1,'epoch','epoch')",
                (TENANT_A, USER_A, INSTRUMENT, config.id),
            )
        assert repo.claim()["timeframe"] == "15m"  # old eagerly seeded 4h does not consume quota
        with psycopg.connect(migrated_database) as conn:
            conn.execute("UPDATE coinglass_configs SET status='DISABLED' WHERE id=%s", (config.id,))
        assert repo.get(TENANT_A, USER_A, INSTRUMENT, "1m")["status"] == "NO_CONFIG"
    finally:
        pool.close()
