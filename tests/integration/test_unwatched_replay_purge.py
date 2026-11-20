"""Destructive cases run ONLY in the existing disposable gatescope_test fixture."""
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
import pytest

from tests.integration.test_postgres_rls import (
    INSTRUMENT, TENANT_A, TENANT_B, USER_A, USER_B, seed_two_tenants,
)

OTHER = UUID("30000000-0000-0000-0000-000000000034")
TARGET = "1" * 64
PROTECTED = "2" * 64
LIVE = "3" * 64
FOREIGN = "4" * 64
MIXED = "5" * 64


@pytest.fixture
def purge_database(migrated_database):
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as conn:
        conn.execute("""INSERT INTO instruments(id,venue,market,symbol,base_asset,quote_asset,settle_asset,
          status,created_at,updated_at) VALUES(%s,'GATE','FUTURES_USDT','ETH_USDT','ETH','USDT','USDT','ACTIVE',now(),now())""", (OTHER,))
    try:
        yield migrated_database
    finally:
        # Test-only tombstones must be removed before the shared fixture downgrades.
        # Production downgrade deliberately refuses after any completed deletion.
        with psycopg.connect(migrated_database) as conn:
            conn.execute("DELETE FROM gatescope_security.replay_purge_contexts")
            conn.execute("DELETE FROM gatescope_security.replay_purge_jobs")


def event(conn, context=TARGET, instrument=INSTRUMENT, mode="REPLAY", candidate="one"):
    conn.execute("""INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,
      version,source_mode,phase,observed_time,event_json)
      VALUES(%s,%s,1,%s,'1h','structure-study-3.2',%s,'FORMING',now(),'{}')""", (context, candidate, instrument, mode))


def review(conn, context, *, instrument=INSTRUMENT, tenant=TENANT_B, user=USER_B):
    conn.execute("""INSERT INTO coinglass_pattern_reviews(id,tenant_id,user_id,instrument_id,timeframe,
      context_id,candidate_id,payload) VALUES(%s,%s,%s,%s,'1h',%s,'one','{}')""",
      (uuid4(), tenant, user, instrument, context))


def open_job(conn, job):
    conn.execute("SET LOCAL lock_timeout='200ms'")
    conn.execute("SET LOCAL statement_timeout='3s'")
    return conn.execute("SELECT gatescope_security.open_replay_purge(%s,%s)", (job, INSTRUMENT)).fetchone()[0]


def purge_all(conn, job):
    total = 0
    for _ in range(20):
        result = conn.execute("SELECT gatescope_security.replay_purge_batch(%s,1)", (job,)).fetchone()[0]
        assert 0 <= result["deleted_rows"] <= 1
        total += result["deleted_rows"]
        if result["status"] == "COMPLETE":
            return total
    pytest.fail("bounded fixture should finish in 20 single-row batches")


def test_exact_context_boundaries_private_references_and_resumable_batches(purge_database):
    job = uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        event(conn, PROTECTED)
        event(conn, LIVE, mode="RECORDED")
        event(conn, FOREIGN, instrument=OTHER)
        event(conn, MIXED)
        event(conn, MIXED, mode="RECORDED", candidate="mixed-live")
        review(conn, PROTECTED)  # Another tenant's private owner-scoped reference.
        conn.execute("""INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,
          score_bucket,stage_time,phase,sampled,formed) VALUES(%s,'one','double_bottom','FORMING','HIGH',1,'FORMING',true,false)""", (TARGET,))
        conn.execute("""INSERT INTO market_structure_returns(context_id,evidence_id,pattern,phase,horizon,
          observed_time,status,side) VALUES(%s,'one','double_bottom','FORMING',24,1,'PENDING',1)""", (TARGET,))
        conn.execute("""INSERT INTO market_pattern_reports(context_id,instrument_id,timeframe,version,report_json)
          VALUES(%s,%s,'1h','structure-study-3.2','{}')""", (TARGET, INSTRUMENT))
        conn.execute("INSERT INTO market_pattern_report_versions VALUES(%s,'2026-01-01','{}',now())", (TARGET,))
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.subject_id',%s,true)", (str(TENANT_A), str(USER_A)))
        preview = conn.execute("SELECT gatescope_security.inspect_replay_purge(%s,16)", (INSTRUMENT,)).fetchone()[0]
        assert [item["context_id"] for item in preview["contexts"]] == [TARGET]
        assert open_job(conn, job)["contexts"] == 1
    with psycopg.connect(purge_database) as conn:
        result = conn.execute("SELECT gatescope_security.replay_purge_batch(%s,1)", (job,)).fetchone()[0]
        assert result["counts"] == {"market_structure_stages": 1}
        conn.execute("SELECT gatescope_security.close_replay_purge(%s,'PAUSED')", (job,))
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_CONTEXT"):
            review(conn, TARGET)
    with psycopg.connect(purge_database) as conn:
        assert open_job(conn, job)["contexts"] == 1
        assert purge_all(conn, job) == 4
        conn.execute("SELECT gatescope_security.close_replay_purge(%s,'DONE')", (job,))
        assert conn.execute("SELECT count(*) FROM market_pattern_events WHERE context_id=%s", (TARGET,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM coinglass_pattern_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT state,deleted_rows FROM gatescope_security.replay_purge_contexts WHERE context_id=%s", (TARGET,)).fetchone() == ("PURGED", 5)
    for table in ("market_pattern_reports", "market_pattern_events"):
        with psycopg.connect(purge_database) as conn:
            # No global immutability relaxation, even for the maintenance owner.
            with pytest.raises(psycopg.Error):
                if table == "market_pattern_events":
                    conn.execute("DELETE FROM market_pattern_events WHERE context_id=%s", (LIVE,))
                else:
                    conn.execute("UPDATE market_pattern_reports SET report_json='{}'")
                    # No report rows remain; use a direct source write into the tombstone instead.
                    conn.execute("INSERT INTO market_pattern_report_versions VALUES(%s,'late','{}',now())", (TARGET,))


def test_read_before_purge_cannot_later_create_reference_or_source(purge_database):
    job = uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        event(conn, LIVE, mode="RECORDED")
        event(conn, FOREIGN, instrument=OTHER)
    with psycopg.connect(purge_database, autocommit=True) as reader:
        with reader.transaction():
            assert reader.execute("SELECT context_id FROM market_pattern_events WHERE context_id=%s", (TARGET,)).fetchone()
            with psycopg.connect(purge_database) as maintenance:
                open_job(maintenance, job)
                assert purge_all(maintenance, job) == 1
                maintenance.execute("SELECT gatescope_security.close_replay_purge(%s,'DONE')", (job,))
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_CONTEXT"):
                review(reader, TARGET)
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_CONTEXT"):
            event(conn)
    with psycopg.connect(purge_database) as conn:
        # Unrelated and RECORDED references remain writable after a tombstone.
        review(conn, LIVE)
        review(conn, FOREIGN, instrument=OTHER)
        event(conn, LIVE, mode="RECORDED", candidate="later-live")


def test_first_job_waits_for_uncommitted_reference_and_then_preserves_it(purge_database):
    with psycopg.connect(purge_database) as conn:
        event(conn)
    with psycopg.connect(purge_database, autocommit=True) as writer:
        with writer.transaction():
            review(writer, TARGET)
            with psycopg.connect(purge_database) as maintenance:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    open_job(maintenance, uuid4())
        with psycopg.connect(purge_database) as maintenance:
            preview = maintenance.execute("SELECT gatescope_security.inspect_replay_purge(%s,16)", (INSTRUMENT,)).fetchone()[0]
            assert preview["contexts"] == []
            assert maintenance.execute("SELECT count(*) FROM gatescope_security.replay_purge_jobs").fetchone()[0] == 0


def test_business_roles_cannot_call_or_forge_scope_and_bad_refs_fail_closed(purge_database):
    with psycopg.connect(purge_database) as conn:
        event(conn)
        for role in ("gatescope_api", "gatescope_strategy", "gatescope_paper", "gatescope_mcp", "gatescope_notification"):
            assert conn.execute("SELECT has_function_privilege(%s,'gatescope_security.open_replay_purge(uuid,uuid)','EXECUTE')", (role,)).fetchone() == (False,)
    with psycopg.connect(purge_database) as conn:
        conn.execute("SET LOCAL ROLE gatescope_api")
        conn.execute("SELECT set_config('gatescope.replay_purge_job',%s,true)", (str(uuid4()),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT gatescope_security.open_replay_purge(%s,%s)", (uuid4(), INSTRUMENT))
    with psycopg.connect(purge_database) as conn:
        review(conn, "legacy-unknown-context")
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_REFERENCE_UNVERIFIED"):
            open_job(conn, uuid4())


def test_alert_reference_paths_union_and_conflicts_are_rejected(purge_database):
    with psycopg.connect(purge_database) as conn:
        for entry in ({"context_id": TARGET}, {"source_event": {"context_id": TARGET}},
                      {"trade_notice": {"evidence": {"context_id": TARGET}}}):
            assert conn.execute("SELECT gatescope_security.replay_reference_contexts('alert_event_facts',%s)",
                                (Jsonb({"matched": [entry]}),)).fetchone()[0] == [TARGET]
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            conn.execute("SELECT gatescope_security.replay_reference_contexts('alert_event_facts',%s)",
                         (Jsonb({"matched": [{"context_id": TARGET, "source_event": {"context_id": FOREIGN}}]}),))


def test_untouched_expired_lease_releases_and_new_reference_is_preserved(purge_database):
    job = uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        open_job(conn, job)
        conn.execute("UPDATE gatescope_security.replay_purge_jobs SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s", (job,))
        review(conn, TARGET)
        event(conn, candidate="allowed-after-expiry")
    with psycopg.connect(purge_database) as conn:
        open_job(conn, job)
        result = conn.execute("SELECT gatescope_security.replay_purge_batch(%s,1)", (job,)).fetchone()[0]
        assert result["status"] == "RETAINED_REFERENCE" and result["deleted_rows"] == 0
        assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == 2


def test_coinglass_update_cannot_repoint_to_purged_context(purge_database):
    job = uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        event(conn, LIVE, mode="RECORDED")
        review(conn, LIVE)
        open_job(conn, job)
        assert purge_all(conn, job) == 1
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_CONTEXT"):
            conn.execute("UPDATE coinglass_pattern_reviews SET context_id=%s", (TARGET,))


def test_real_business_roles_keep_other_instruments_and_recorded_writable(purge_database):
    job = uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        event(conn, LIVE, mode="RECORDED")
        event(conn, FOREIGN, instrument=OTHER)
        open_job(conn, job)
        assert purge_all(conn, job) == 1
        conn.execute("SELECT gatescope_security.close_replay_purge(%s,'DONE')", (job,))
    with psycopg.connect(purge_database) as conn:
        conn.execute("SET LOCAL ROLE gatescope_strategy")
        event(conn, LIVE, mode="RECORDED", candidate="live-worker-still-writes")
        event(conn, FOREIGN, instrument=OTHER, candidate="other-worker-still-writes")
        conn.execute("""INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,
          score_bucket,stage_time,phase,sampled,formed) VALUES(%s,'live-worker','double_bottom','FORMING','HIGH',1,'FORMING',true,false)""", (LIVE,))
    with psycopg.connect(purge_database) as conn:
        conn.execute("SET LOCAL ROLE gatescope_api")
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.subject_id',%s,true)",
                     (str(TENANT_A), str(USER_A)))
        review(conn, LIVE, tenant=TENANT_A, user=USER_A)
        review(conn, FOREIGN, instrument=OTHER, tenant=TENANT_A, user=USER_A)


def test_new_watchlist_or_active_collection_blocks_before_any_deletion(purge_database):
    watchlist, item = uuid4(), uuid4()
    with psycopg.connect(purge_database) as conn:
        event(conn)
        conn.execute("""INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at)
          VALUES(%s,%s,%s,'protected',now(),now())""", (watchlist, TENANT_B, USER_B))
        conn.execute("""INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,
          default_timeframe,created_at,updated_at) VALUES(%s,%s,%s,%s,0,'1h',now(),now())""",
          (item, TENANT_B, watchlist, INSTRUMENT))
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_TARGET_WATCHED"):
            open_job(conn, uuid4())
    with psycopg.connect(purge_database) as conn:
        conn.execute("DELETE FROM watchlist_items WHERE id=%s", (item,))
        conn.execute("INSERT INTO market_sync_state(instrument_id,timeframe,active) VALUES(%s,'1h',true)", (INSTRUMENT,))
    with psycopg.connect(purge_database) as conn:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="PURGE_TARGET_ACTIVE_COLLECTION"):
            open_job(conn, uuid4())
    with psycopg.connect(purge_database) as conn:
        assert conn.execute("SELECT count(*) FROM gatescope_security.replay_purge_jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == 1


def test_new_manifest_contains_only_one_context_and_retains_referenced_context(purge_database):
    first_job, second_job = uuid4(), uuid4()
    second_context = "6" * 64
    with psycopg.connect(purge_database) as conn:
        event(conn)
        event(conn, PROTECTED)
        event(conn, second_context)
        event(conn, LIVE, mode="RECORDED")
        event(conn, FOREIGN, instrument=OTHER)
        review(conn, PROTECTED)
        assert open_job(conn, first_job)["contexts"] == 1
        assert conn.execute("SELECT context_id FROM gatescope_security.replay_purge_contexts WHERE job_id=%s",
                            (first_job,)).fetchall() == [(TARGET,)]
        assert purge_all(conn, first_job) == 1
        conn.execute("SELECT gatescope_security.close_replay_purge(%s,'DONE')", (first_job,))
        assert conn.execute("SELECT count(*) FROM market_pattern_events WHERE context_id=%s", (second_context,)).fetchone()[0] == 1
    with psycopg.connect(purge_database) as conn:
        assert open_job(conn, second_job)["contexts"] == 1
        assert conn.execute("SELECT context_id FROM gatescope_security.replay_purge_contexts WHERE job_id=%s",
                            (second_job,)).fetchall() == [(second_context,)]
        assert purge_all(conn, second_job) == 1
        assert conn.execute("SELECT count(*) FROM market_pattern_events WHERE context_id=%s", (PROTECTED,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM coinglass_pattern_reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM market_pattern_events").fetchone()[0] == 3
