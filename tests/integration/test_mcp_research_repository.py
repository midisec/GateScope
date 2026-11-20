from datetime import timedelta
from pathlib import Path
from uuid import uuid4
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from alembic import command
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from gatescope.adapters.postgres_mcp_research import McpResearchRepository
from gatescope.adapters.postgres_oauth import OAuthError, PostgresOAuthRepository, STABLE_CALLBACK
from tests.integration.conftest import migration_config
from tests.integration.test_postgres_rls import (
    TENANT_A,
    TENANT_B,
    USER_A,
    USER_B,
    INSTRUMENT,
    seed_two_tenants,
)


def _seed_research(database_url):
    seed_two_tenants(database_url)
    runs = {}
    with psycopg.connect(database_url) as conn:
        for tenant, user, name in [
            (TENANT_A, USER_A, "owned"),
            (TENANT_A, USER_B, "other-owner"),
            (TENANT_B, USER_B, "other-tenant"),
        ]:
            watch = uuid4()
            conn.execute(
                "INSERT INTO watchlists(id,tenant_id,owner_user_id,name,created_at,updated_at) VALUES(%s,%s,%s,%s,now(),now())",
                (watch, tenant, user, name),
            )
            conn.execute(
                "INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at) VALUES(%s,%s,%s,%s,0,'1h',now(),now())",
                (uuid4(), tenant, watch, INSTRUMENT),
            )
            modes = ("INDICATOR", "STRUCTURE") if name == "owned" else ("INDICATOR",)
            for mode in modes:
                run = uuid4()
                runs[(name, mode)] = run
                spec = {"engine_mode": mode, "fee_bps": 5, "slippage_bps": 7, "funding_bps_per_8h": 1}
                conn.execute(
                    "INSERT INTO research_backtests(id,tenant_id,owner_user_id,instrument_id,timeframe,spec_json,bar_limit,state,completed_at) VALUES(%s,%s,%s,%s,'1h',%s,300,'SUCCEEDED',now())",
                    (run, tenant, user, INSTRUMENT, Jsonb(spec)),
                )
                report = {
                    "engine_version": "frozen-test",
                    "bar_count": 5,
                    "spec": spec,
                    "artifact_digest": "saved-digest",
                    "full": {
                        "metrics": {"win_rate": 0.5},
                        "trades": [{"entry_time": i, "fees": 0.1} for i in range(5)],
                        "equity_curve": [{"time": i, "equity": 100 + i} for i in range(5)],
                    },
                    "training": {"trade_count": 4},
                    "validation": {"trade_count": 1},
                    "range": {"embargo_bars": 48},
                    "evidence": {"sealed_holdout": False},
                    "input_bars": [{"open_time": i, "revision": 1} for i in range(5)],
                    "pattern_events": [{"id": i, "phase": "INVALIDATED"} for i in range(5)],
                    "structure_research": {
                        "portfolio_metrics_available": False,
                        "walk_forward": {
                            "folds": [{"training": {"sample_count": 4}, "validation": {"sample_count": 1}}]
                        },
                        "cost_stress": [{"multiplier": 2}],
                    },
                }
                if mode == "STRUCTURE":
                    for index in range(3):
                        conn.execute(
                            "INSERT INTO research_structure_inputs(run_id,tenant_id,owner_user_id,bar_index,open_time,bar_json) VALUES(%s,%s,%s,%s,%s,%s)",
                            (
                                run,
                                tenant,
                                user,
                                index,
                                index,
                                Jsonb({"ts": index, "available_at": index + 1, "revision": 2}),
                            ),
                        )
                        conn.execute(
                            "INSERT INTO research_structure_events(run_id,tenant_id,owner_user_id,candidate_id,sequence,observed_time,phase,pattern,event_json) VALUES(%s,%s,%s,%s,1,%s,'INVALIDATED','double_bottom',%s)",
                            (
                                run,
                                tenant,
                                user,
                                str(index),
                                index,
                                Jsonb({"phase": "INVALIDATED", "original": index}),
                            ),
                        )
                        conn.execute(
                            """INSERT INTO research_structure_cases(run_id,tenant_id,owner_user_id,candidate_id,pattern,observed_time,selected,baseline,selection_reasons,environment,state,entry_time,exit_time,fees,funding,case_json)
                                      VALUES(%s,%s,%s,%s,'double_bottom',%s,true,true,'[]','{}','STOP',%s,%s,.1,.01,%s)""",
                            (
                                run,
                                tenant,
                                user,
                                str(index),
                                index,
                                index,
                                index + 1,
                                Jsonb({"state": "STOP", "frozen_stop": 90}),
                            ),
                        )
                conn.execute(
                    "INSERT INTO research_backtest_results(run_id,tenant_id,owner_user_id,report_json) VALUES(%s,%s,%s,%s)",
                    (run, tenant, user, Jsonb(report)),
                )
    return runs


def test_mcp_research_real_role_owner_rls_privileged_explicit_filters_and_pagination(migrated_database):
    runs = _seed_research(migrated_database)
    mcp = PostgresPool(
        make_conninfo(migrated_database, options="-c role=gatescope_mcp"), min_size=1, max_size=1
    )
    admin = PostgresPool(migrated_database, min_size=1, max_size=1)
    mcp.open()
    admin.open()
    try:
        for pool in (mcp, admin):
            repo = McpResearchRepository(pool)
            watch = repo.watchlist(TENANT_A, USER_A)
            assert watch["total"] == 1 and watch["items"][0]["watchlist_name"] == "owned"
            assert watch["items"][0]["cache"] is None  # no implicit warming/subscription
            assert repo.backtests(TENANT_A, USER_A)["total"] == 2
            for key in (("other-owner", "INDICATOR"), ("other-tenant", "INDICATOR")):
                with pytest.raises(LookupError, match="RESEARCH_NOT_FOUND"):
                    repo.backtest_detail(TENANT_A, USER_A, runs[key], section="inputs")
            indicator = runs[("owned", "INDICATOR")]
            for section in ("trades", "equity", "events", "inputs"):
                page = repo.backtest_detail(TENANT_A, USER_A, indicator, section=section, limit=2, offset=2)
                assert page["total"] == 5 and page["returned"] == 2 and page["next_offset"] == 4
            validation = repo.backtest_detail(TENANT_A, USER_A, indicator, section="validation")
            assert validation["items"][0]["training"] == {"trade_count": 4}
            assert validation["items"][0]["validation"] == {"trade_count": 1}
            structural = runs[("owned", "STRUCTURE")]
            for section in ("cases", "trades", "events", "inputs"):
                page = repo.backtest_detail(TENANT_A, USER_A, structural, section=section, limit=1, offset=1)
                assert page["total"] == 3 and page["next_offset"] == 2
            assert (
                repo.backtest_detail(TENANT_A, USER_A, structural, section="equity")["availability"]
                == "NOT_APPLICABLE"
            )
            assert (
                repo.backtest_detail(TENANT_A, USER_A, structural, section="events")["items"][0]["phase"]
                == "INVALIDATED"
            )
        with mcp.transaction(TENANT_A, USER_A) as conn:
            assert conn.execute("SELECT count(*) FROM watchlists").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM watchlist_items").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM research_backtests").fetchone() == (2,)
            for statement in (
                "UPDATE watchlists SET name='forbidden'",
                "DELETE FROM research_structure_events",
                "INSERT INTO research_backtests DEFAULT VALUES",
                "SELECT * FROM research_structure_checkpoints",
                "SELECT secret_ciphertext FROM coinglass_configs",
            ):
                with pytest.raises(InsufficientPrivilege), conn.transaction():
                    conn.execute(statement)
        with psycopg.connect(migrated_database) as conn:
            assert conn.execute("SELECT count(*) FROM market_sync_state").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM research_backtests").fetchone() == (4,)
    finally:
        mcp.close()
        admin.close()


def _oauth_token(repo, scope):
    client = repo.register_client(
        {"client_name": "scope regression", "redirect_uris": [STABLE_CALLBACK], "scope": scope}
    )
    nonce = secrets.token_urlsafe(32)
    proof = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).rstrip(b"=").decode()
    resource = "https://example.com/mcp"
    handle = repo.begin_authorization(
        {
            "client_id": client["client_id"],
            "redirect_uri": STABLE_CALLBACK,
            "response_type": "code",
            "state": "test-scope-state",
            "resource": resource,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": scope,
        },
        nonce,
    )
    repo.pending_authorization(handle, nonce, TENANT_A, USER_A)
    location = repo.approve_authorization(handle, nonce, TENANT_A, USER_A, approved_scopes=scope.split())
    code = parse_qs(urlsplit(location).query)["code"][0]
    token = repo.exchange_code(
        client_id=client["client_id"],
        code=code,
        code_verifier=proof,
        redirect_uri=STABLE_CALLBACK,
        resource=resource,
    )
    return client, token


def test_research_scope_upgrade_does_not_expand_legacy_and_downgrade_revokes_only_research(migrated_database):
    seed_two_tenants(migrated_database)
    config = migration_config(Path(__file__).resolve().parents[2], migrated_database)
    pool = PostgresPool(migrated_database, min_size=1, max_size=1)
    pool.open()
    try:
        pat = PostgresMcpRepository(pool)
        client = pat.create_client(TENANT_A, USER_A, name="scope-check")
        legacy = pat.create_token(
            TENANT_A,
            USER_A,
            client.id,
            label="legacy",
            scopes=["mcp:connect", "market:read"],
            ttl=timedelta(hours=2),
        )
        oauth = PostgresOAuthRepository(pool, public_origin="https://example.com")
        _, legacy_oauth = _oauth_token(oauth, "mcp:connect market:read analysis:read")
        command.downgrade(config, "0044_coinglass_queue_progress")
        command.upgrade(config, "0045_mcp_research_read")
        assert set(pat.verify_token(legacy.secret).scopes) == {"mcp:connect", "market:read"}
        assert "research:read" not in oauth.verify_access(legacy_oauth["access_token"]).scopes
        research = pat.create_token(
            TENANT_A,
            USER_A,
            client.id,
            label="research",
            scopes=["mcp:connect", "market:read", "analysis:read", "alerts:read", "research:read"],
            ttl=timedelta(hours=2),
        )
        research_client, research_oauth = _oauth_token(
            oauth, "mcp:connect market:read analysis:read research:read"
        )
        assert "research:read" in pat.verify_token(research.secret).scopes
        assert "research:read" in oauth.verify_access(research_oauth["access_token"]).scopes
        command.downgrade(config, "0044_coinglass_queue_progress")
        assert pat.verify_token(research.secret) is None
        assert oauth.verify_access(research_oauth["access_token"]) is None
        assert set(pat.verify_token(legacy.secret).scopes) == {"mcp:connect", "market:read"}
        assert oauth.verify_access(legacy_oauth["access_token"]) is not None
        with pytest.raises(OAuthError):
            oauth.refresh(
                client_id=research_client["client_id"],
                refresh_token=research_oauth["refresh_token"],
                resource="https://example.com/mcp",
            )
    finally:
        pool.close()
