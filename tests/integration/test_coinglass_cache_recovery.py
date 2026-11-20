from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_coinglass import PostgresCoinglassRepository
from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.domain.coinglass_context import normalize_series
from tests.integration.test_coinglass_web_configuration import API_KEY, _crypto_pair
from tests.integration.test_postgres_rls import (
    INSTRUMENT,
    TENANT_A,
    TENANT_B,
    USER_A,
    USER_B,
    seed_two_tenants,
)
from tests.unit.test_coinglass_cache import good_bundle


class RolePool(PostgresPool):
    def __init__(self, database_url, role):
        super().__init__(database_url, min_size=1, max_size=1)
        self.role = role

    @contextmanager
    def transaction(self, tenant_id, subject_id):
        with super().transaction(tenant_id, subject_id) as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.role)))
            yield conn

    @contextmanager
    def public_transaction(self):
        with super().public_transaction() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.role)))
            yield conn


@pytest.fixture
def setup_cache(migrated_database):
    seed_two_tenants(migrated_database)
    admin = PostgresPool(migrated_database, min_size=1, max_size=1)
    admin.open()
    encryptor, _ = _crypto_pair()
    configs = []
    for tenant, user in [(TENANT_A, USER_A), (TENANT_B, USER_B)]:
        config, _ = PostgresCoinglassRepository(admin).save_and_enqueue(
            tenant, user, api_key=API_KEY, test_symbol="BTC", encryptor=encryptor
        )
        configs.append(config)
    with psycopg.connect(migrated_database) as conn:
        conn.execute("UPDATE coinglass_configs SET status='ACTIVE'")
    pools = {role: RolePool(migrated_database, "gatescope_" + role) for role in ("api", "mcp", "market")}
    for pool in pools.values():
        pool.open()
    try:
        yield {
            "db": migrated_database,
            "configs": configs,
            "pools": pools,
            **{role: PostgresCoinglassContextRepository(pool) for role, pool in pools.items()},
        }
    finally:
        for pool in pools.values():
            pool.close()
        admin.close()


def test_mcp_enqueue_is_owned_deduplicated_and_bounded(setup_cache):
    s = setup_cache
    mcp = s["mcp"]
    assert mcp.get(TENANT_A, USER_A, INSTRUMENT, "4h", request=True)["refresh_requested"]
    assert mcp.get(TENANT_A, USER_B, INSTRUMENT, "4h", request=True)["status"] == "NO_CONFIG"
    invalid = mcp.get(TENANT_A, USER_A, uuid4(), "4h", request=True)
    assert invalid["status"] == "NOT_CACHED" and not invalid["refresh_requested"]
    assert invalid["reason"] == "CACHE_REQUEST_NOT_QUEUED"
    with s["pools"]["mcp"].transaction(TENANT_A, USER_A) as conn:
        for tenant, user, instrument, tf in [
            (TENANT_B, USER_B, INSTRUMENT, "1h"),
            (TENANT_A, USER_A, INSTRUMENT, "2h"),
            (TENANT_A, USER_A, uuid4(), "1h"),
        ]:
            conn.execute(
                "SELECT gatescope_security.request_coinglass_chart(%s,%s,%s,%s)",
                (tenant, user, instrument, tf),
            )
        assert conn.execute("SELECT count(*) FROM coinglass_chart_cache").fetchone() == (1,)
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT secret_ciphertext FROM coinglass_configs")
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT * FROM gatescope_security.coinglass_provider_cooldowns")
    job = s["market"].claim()
    assert job and job["timeframe"] == "4h"
    for _ in range(3):
        assert mcp.get(TENANT_A, USER_A, INSTRUMENT, "4h", request=True)["refresh_requested"]
    assert s["market"].claim() is None
    with psycopg.connect(s["db"]) as conn:
        assert conn.execute(
            "SELECT lease_until-now()>interval '170 seconds' FROM coinglass_chart_cache"
        ).fetchone() == (True,)
        # Fill a single owner's finite cache allowance, without any provider I/O.
        for position in range(255):
            instrument = uuid4()
            conn.execute(
                """INSERT INTO instruments(id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
                          VALUES(%s,'GATE','FUTURES_USDT',%s,'TEST','USDT','USDT','ACTIVE',now(),now())""",
                (instrument, f"T{position}_USDT"),
            )
            conn.execute(
                """INSERT INTO coinglass_chart_cache(tenant_id,user_id,instrument_id,timeframe,config_id,secret_version)
                          VALUES(%s,%s,%s,'1h',%s,1)""",
                (TENANT_A, USER_A, instrument, s["configs"][0].id),
            )
    capped = mcp.get(TENANT_A, USER_A, INSTRUMENT, "1d", request=True)
    assert not capped["refresh_requested"] and capped["status"] == "NOT_CACHED"
    assert mcp.get(TENANT_A, USER_A, INSTRUMENT, "4h", request=True)["refresh_requested"]


def test_cooldown_is_private_key_shared_persistent_and_never_shortened(setup_cache):
    s = setup_cache
    config_a, config_b = s["configs"]
    s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    s["mcp"].get(TENANT_B, USER_B, INSTRUMENT, "1h", request=True)
    wait = s["market"].pause_provider(TENANT_A, USER_A, config_a.id, 1, 3601)
    assert 3600 <= wait <= 3601
    assert s["mcp"].provider_wait(TENANT_B, USER_B, config_b.id, 1) >= 3600
    assert s["market"].pause_provider(TENANT_A, USER_A, config_a.id, 1, 10) >= 3600
    assert s["mcp"].provider_wait(TENANT_A, USER_B, config_a.id, 1) == 0
    for role in ("api", "mcp"):
        with pytest.raises(InsufficientPrivilege):
            s[role].pause_provider(TENANT_A, USER_A, config_a.id, 1, 60)
    assert s["market"].claim() is None
    restarted = PostgresCoinglassContextRepository(s["pools"]["mcp"])
    result = restarted.get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    assert result["retry_after_seconds"] >= 3600
    assert result["next_refresh_at"] >= result["retry_at"]
    with psycopg.connect(s["db"]) as conn:
        conn.execute(
            "UPDATE coinglass_configs SET key_fingerprint='SHA256:000000000001' WHERE id=%s", (config_b.id,)
        )
    assert restarted.provider_wait(TENANT_B, USER_B, config_b.id, 1) == 0
    assert s["market"].claim()["tenant"] == TENANT_B
    maximum = s["market"].pause_provider(TENANT_A, USER_A, config_a.id, 1, 2_147_483_647)
    assert 2_147_483_640 <= maximum <= 2_147_483_647


def test_failed_refresh_preserves_evidence_then_expired_limit_wakes_mcp(setup_cache):
    s = setup_cache
    s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    job = s["market"].claim()
    old = good_bundle()
    old["instrument_id"] = str(INSTRUMENT)
    s["market"].complete(job, old)
    with psycopg.connect(s["db"]) as conn:
        conn.execute("UPDATE coinglass_chart_cache SET due_at='epoch',requested_at='epoch'")
    assert s["market"].claim() is None
    assert s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)["refresh_requested"]
    job = s["market"].claim()
    s["market"].complete(job, {"status": "RATE_LIMITED", "series": [], "retry_after_seconds": 60})
    result = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert result["status"] == "STALE" and result["cache_retained"]
    assert result["fetched_at"] == old["fetched_at"] and result["expires_at"] == old["expires_at"]
    assert result["series"][0]["points"] == old["series"][0]["points"]
    assert result["last_attempt_at"] > old["fetched_at"] and result["refresh_status"] == "RATE_LIMITED"
    assert 58 <= result["retry_after_seconds"] <= 60
    with psycopg.connect(s["db"]) as conn:
        conn.execute("UPDATE gatescope_security.coinglass_provider_cooldowns SET pause_until='epoch'")
        legacy = {"status": "RATE_LIMITED", "retry_after_seconds": 10, "fetched_at": 1000, "series": []}
        conn.execute(
            "UPDATE coinglass_chart_cache SET payload=%s,updated_at=to_timestamp(1000),due_at='epoch',requested_at='epoch'",
            (Jsonb(legacy),),
        )
    assert s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")["status"] == "DEFERRED"
    expired = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    assert expired["status"] == "QUEUED" and expired["retry_after_seconds"] == 0
    assert expired["retry_at"] == 1010 and expired["refresh_status"] == "QUEUED"
    assert s["market"].claim() is not None


def test_defer_and_rotated_completion_do_not_rewrite_evidence_or_pause_new_key(setup_cache):
    s = setup_cache
    s["api"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    job = s["market"].claim()
    with psycopg.connect(s["db"]) as conn:
        before = conn.execute("SELECT payload,updated_at FROM coinglass_chart_cache").fetchone()
    s["market"].defer(job, delay=600)
    with psycopg.connect(s["db"]) as conn:
        after = conn.execute(
            "SELECT payload,updated_at,lease_id,due_at FROM coinglass_chart_cache"
        ).fetchone()
        assert after[:2] == before and after[2] is None
        assert after[3].timestamp() > datetime.now(timezone.utc).timestamp() + 590
        conn.execute("UPDATE coinglass_chart_cache SET due_at='epoch'")
    job = s["market"].claim()
    with psycopg.connect(s["db"]) as conn:
        conn.execute("UPDATE coinglass_configs SET secret_version=2 WHERE id=%s", (s["configs"][0].id,))
    s["market"].complete(job, {"status": "RATE_LIMITED", "retry_after_seconds": 60, "series": []})
    assert s["mcp"].provider_wait(TENANT_A, USER_A, s["configs"][0].id, 2) == 0
    assert not s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")["cache_retained"]
    fresh = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    assert fresh["status"] == "QUEUED" and fresh["refresh_requested"]
    s["market"].complete(job, good_bundle())
    assert s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")["status"] == "NOT_CACHED"


def progress_bundle():
    now = int(datetime.now(timezone.utc).timestamp())
    opened = (now // 3600 - 1) * 3600
    series = normalize_series(
        "oi",
        [{"time": opened * 1000, "open": 100, "high": 100, "low": 100, "close": 100}],
        timeframe="1h",
        fetched_at=now,
    )
    series.update(fetched_at=now, expires_at=now + 600, as_of=opened + 3600)
    return {
        "instrument_id": str(INSTRUMENT),
        "timeframe": "1h",
        "status": "PARTIAL",
        "match": {"status": "EXACT"},
        "scope": {"exchange": "Gate", "symbol": "BTC_USDT", "interval": "1h"},
        "series": [series],
        "fetched_at": now,
        "expires_at": now + 600,
        "as_of": opened + 3600,
    }


def test_real_queue_lifecycle_progress_and_poll_without_reenqueue(setup_cache):
    s = setup_cache
    assert s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")["refresh_state"] == "NOT_REQUESTED"
    queued = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    assert queued["refresh_state"] == "QUEUED" and queued["queued_at"]
    poll = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert poll["refresh_state"] == "QUEUED" and not poll["refresh_requested"]
    assert poll["queued_at"] == queued["queued_at"]
    job = s["market"].claim()
    collecting = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert collecting["refresh_state"] == "COLLECTING" and collecting["collection_started_at"]
    with psycopg.connect(s["db"]) as conn:
        before = conn.execute(
            "SELECT lease_id,lease_until,due_at,pending_since,collection_started_at FROM coinglass_chart_cache"
        ).fetchone()
    partial = progress_bundle()
    assert s["market"].publish_progress(job, partial)
    with psycopg.connect(s["db"]) as conn:
        after = conn.execute(
            "SELECT lease_id,lease_until,due_at,pending_since,collection_started_at FROM coinglass_chart_cache"
        ).fetchone()
    assert before == after
    published = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert published["status"] == "PARTIAL" and published["refresh_state"] == "COLLECTING"
    assert published["cache_available"] and published["cached_points_count"] == 1
    assert published["last_success_at"] == partial["series"][0]["points"][0]["available_at"]
    assert published["series"][0]["fetched_at"] == partial["series"][0]["fetched_at"]
    s["market"].complete(job, partial, delay=5)
    complete = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert complete["refresh_state"] == "SCHEDULED" and complete["queued_at"] is None
    assert 1 <= complete["next_poll_after_seconds"] <= 5
    assert complete["collection_started_at"] is None and complete["cache_available"]
    assert not s["market"].publish_progress(job, partial)


def test_durable_first_request_survives_two_hours_and_owners_are_fair(setup_cache):
    s = setup_cache
    for tenant, user, tf in [(TENANT_A, USER_A, "1h"), (TENANT_A, USER_A, "4h"), (TENANT_B, USER_B, "1h")]:
        s["mcp"].get(tenant, user, INSTRUMENT, tf, request=True)
    with psycopg.connect(s["db"]) as conn:
        conn.execute(
            "UPDATE coinglass_chart_cache SET pending_since=now()-interval '12 hours',requested_at='epoch',due_at='epoch' WHERE tenant_id=%s",
            (TENANT_A,),
        )
        original = conn.execute(
            "SELECT pending_since FROM coinglass_chart_cache WHERE tenant_id=%s AND timeframe='1h'",
            (TENANT_A,),
        ).fetchone()[0]
    for _ in range(3):
        s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    with psycopg.connect(s["db"]) as conn:
        assert (
            conn.execute(
                "SELECT pending_since FROM coinglass_chart_cache WHERE tenant_id=%s AND timeframe='1h'",
                (TENANT_A,),
            ).fetchone()[0]
            == original
        )
        conn.execute("UPDATE coinglass_chart_cache SET requested_at='epoch' WHERE tenant_id=%s", (TENANT_A,))
    first = s["market"].claim()
    assert first["tenant"] == TENANT_A
    s["market"].defer(first, delay=600)
    # Owner B cannot be starved by A's backlog or A repeatedly polling its queue.
    second = s["market"].claim()
    assert second["tenant"] == TENANT_B
    s["market"].complete(second, {"status": "ERROR", "series": []})
    third = s["market"].claim()
    assert third["tenant"] == TENANT_A and third["timeframe"] != first["timeframe"]
    s["market"].complete(third, {"status": "ERROR", "series": []})
    assert s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, third["timeframe"])["refresh_state"] == "IDLE"


def test_progress_rejects_bad_dimensions_rotation_and_preserves_prior_partial_on_429(setup_cache):
    s = setup_cache
    s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h", request=True)
    job = s["market"].claim()
    partial = progress_bundle()
    assert not s["market"].publish_progress(job, {**partial, "timeframe": "4h"})
    assert not s["market"].publish_progress(job, {**partial, "series": []})
    assert not s["market"].publish_progress(job, {**partial, "scope": None})
    assert not s["market"].publish_progress(
        job, {**partial, "series": [{**partial["series"][0], "unit": "BASE"}]}
    )
    assert s["market"].publish_progress(job, partial)
    s["market"].complete(job, {"status": "RATE_LIMITED", "retry_after_seconds": 60, "series": []})
    result = s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")
    assert result["refresh_state"] == "COOLDOWN" and result["cache_available"]
    assert result["status"] == "STALE" and result["series"][0]["cache_retained"]
    assert result["series"][0]["points"] == partial["series"][0]["points"]
    with psycopg.connect(s["db"]) as conn:
        conn.execute("UPDATE coinglass_chart_cache SET pending_since=now()-interval '12 hours'")
        conn.execute("UPDATE coinglass_configs SET secret_version=2 WHERE id=%s", (s["configs"][0].id,))
    assert not s["market"].publish_progress(job, partial)
    assert not s["mcp"].get(TENANT_A, USER_A, INSTRUMENT, "1h")["cache_available"]
    s["market"].seed()
    with psycopg.connect(s["db"]) as conn:
        reset = conn.execute(
            "SELECT pending_since>now()-interval '5 seconds',collection_started_at,payload FROM coinglass_chart_cache"
        ).fetchone()
    assert reset == (True, None, {})
