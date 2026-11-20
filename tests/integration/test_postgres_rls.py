from __future__ import annotations

import os
import base64
import time
from uuid import UUID

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import McpNotFound, PostgresMcpRepository
from gatescope.auth import SESSION_COOKIE
from gatescope.formal_auth import FormalIdentityService, FormalInvalidCredentials
from gatescope.security_secrets import AesGcmSecretProtector, totp_code
from apps.api.formal_app import create_formal_app


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")

TENANT_A = UUID("10000000-0000-0000-0000-000000000001")
TENANT_B = UUID("20000000-0000-0000-0000-000000000002")
USER_A = UUID("10000000-0000-0000-0000-000000000011")
USER_B = UUID("20000000-0000-0000-0000-000000000022")
INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")

CANONICAL_WEB_ROUTES = (
    "/markets/chart",
    "/strategies/alerts",
    "/strategies/backtests",
    "/trading/paper",
    "/trading/shadow",
    "/trading/exchange-accounts",
    "/integrations/mcp",
    "/integrations/data-sources",
    "/notifications/inbox",
    "/settings/account",
    "/settings/notifications",
)

LEGACY_WEB_ROUTES = {
    "/": "/markets/chart",
    "/markets": "/markets/chart",
    "/alerts": "/strategies/alerts",
    "/paper": "/trading/paper",
    "/exchange": "/trading/exchange-accounts",
    "/mcp": "/integrations/mcp",
    "/data-sources": "/integrations/data-sources",
    "/notifications": "/notifications/inbox",
    "/settings": "/settings/account",
    "/settings/security": "/settings/account",
}


def seed_two_tenants(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO tenants (id, slug, name, status, created_at, updated_at) VALUES
                 (%s, 'tenant-a', 'Tenant A', 'ACTIVE', now(), now()),
                 (%s, 'tenant-b', 'Tenant B', 'ACTIVE', now(), now())""",
            (TENANT_A, TENANT_B),
        )
        connection.execute(
            """INSERT INTO users
                 (id, normalized_username, display_username, password_hash, status,
                  password_changed_at, created_at, updated_at) VALUES
                 (%s, 'alice', 'Alice', 'not-a-real-hash', 'ACTIVE', now(), now(), now()),
                 (%s, 'bob', 'Bob', 'not-a-real-hash', 'ACTIVE', now(), now(), now())""",
            (USER_A, USER_B),
        )
        connection.execute(
            """INSERT INTO memberships
                 (id, tenant_id, user_id, role, status, created_at, updated_at) VALUES
                 ('10000000-0000-0000-0000-000000000099', %s, %s, 'OWNER', 'ACTIVE', now(), now()),
                 ('20000000-0000-0000-0000-000000000099', %s, %s, 'OWNER', 'ACTIVE', now(), now())""",
            (TENANT_A, USER_A, TENANT_B, USER_B),
        )
        connection.execute(
            """INSERT INTO system_settings
                 (id, tenant_id, setting_key, schema_version, value_json, created_at, updated_at) VALUES
                 ('10000000-0000-0000-0000-000000000101', %s, 'marker', 1, '{"owner":"A"}', now(), now()),
                 ('20000000-0000-0000-0000-000000000202', %s, 'marker', 1, '{"owner":"B"}', now(), now())""",
            (TENANT_A, TENANT_B),
        )
        connection.execute(
            """INSERT INTO instruments
                 (id, venue, market, symbol, base_asset, quote_asset, settle_asset,
                  status, created_at, updated_at)
               VALUES (%s, 'GATE', 'FUTURES_USDT', 'BTC_USDT', 'BTC', 'USDT', 'USDT',
                       'ACTIVE', now(), now())""",
            (INSTRUMENT,),
        )


def visible_owners(connection: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    rows = connection.execute("SELECT value_json->>'owner' FROM system_settings ORDER BY setting_key").fetchall()
    return [str(row[0]) for row in rows]


def test_force_rls_is_fail_closed_and_pool_context_does_not_leak(migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as connection:
        webhook_inbox_grant = connection.execute(
            "SELECT has_table_privilege('gatescope_api', 'inbox_events', 'SELECT,INSERT,UPDATE')",
        ).fetchone()
        assert webhook_inbox_grant is not None and webhook_inbox_grant[0]
        worker_grants = connection.execute(
            """SELECT
                 has_table_privilege('gatescope_notification','operations_current','SELECT,UPDATE'),
                 has_table_privilege('gatescope_notification','alert_rule_versions','SELECT'),
                 has_table_privilege('gatescope_notification','instruments','SELECT')"""
        ).fetchone()
        assert worker_grants == (True, True, True)
        connection.execute("SET ROLE gatescope_api")
        connection.commit()

        with connection.transaction():
            assert visible_owners(connection) == []

        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
            connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
            assert visible_owners(connection) == ["A"]

        with connection.transaction():
            assert visible_owners(connection) == []

        with pytest.raises(RuntimeError, match="force rollback"):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                assert visible_owners(connection) == ["A"]
                raise RuntimeError("force rollback")

        with connection.transaction():
            assert visible_owners(connection) == []
            connection.execute("SELECT set_config('app.tenant_id', 'not-a-uuid', true)")
            assert visible_owners(connection) == []
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_B),))
            assert visible_owners(connection) == ["B"]


def test_formal_identity_mfa_recovery_and_immutable_audit(migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    password_fixture = "correct-horse-battery-staple"
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(password_fixture), USER_A),
        )

    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        service = FormalIdentityService(
            pool,
            AesGcmSecretProtector(b"k" * 32, "integration-v1"),
            b"p" * 32,
        )
        created = service.authenticate_password("tenant-a", "alice", password_fixture, trace_id="login-ok")
        assert not created.session.fully_authenticated
        assert service.session(created.token) is not None
        enrollment = service.enroll_totp(created.session, label="Primary")
        code, _ = totp_code(enrollment.secret)
        service.confirm_totp(created.session, enrollment.method_id, code, trace_id="mfa-confirm")
        authenticated = service.session(created.token)
        assert authenticated is not None and authenticated.fully_authenticated
        with pytest.raises(FormalInvalidCredentials):
            service.reauthenticate_password(
                authenticated, "wrong-password-long-enough", trace_id="reauth-bad",
            )
        reauthenticated = service.reauthenticate_password(
            authenticated, password_fixture, trace_id="reauth-ok",
        )
        assert reauthenticated.recent_auth_at is not None
        with pytest.raises(FormalInvalidCredentials):
            service.verify_session_mfa(authenticated, code, trace_id="mfa-replay")
        recovery_codes = service.regenerate_recovery_codes(authenticated, trace_id="recovery-create")
        recovered = service.consume_recovery_code(authenticated, recovery_codes[0], trace_id="recovery-use")
        assert recovered.auth_strength == "RECOVERY"
        with pytest.raises(FormalInvalidCredentials):
            service.consume_recovery_code(authenticated, recovery_codes[0], trace_id="recovery-replay")
        with pytest.raises(FormalInvalidCredentials):
            service.authenticate_password("tenant-a", "alice", "wrong-password-long-enough", trace_id="login-bad")
    finally:
        pool.close()

    with psycopg.connect(migrated_database) as connection:
        failures = connection.execute("SELECT failed_attempts FROM users WHERE id=%s", (USER_A,)).fetchone()
        facts = connection.execute("SELECT COUNT(*) FROM identity_audit_facts WHERE tenant_id=%s", (TENANT_A,)).fetchone()
        assert failures is not None and failures[0] == 1
        assert facts is not None and facts[0] >= 7
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="immutable fact"):
            with connection.transaction():
                connection.execute("UPDATE identity_audit_facts SET action='tampered' WHERE tenant_id=%s", (TENANT_A,))


def test_formal_api_blocks_business_until_mfa(monkeypatch: pytest.MonkeyPatch, migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    password_fixture = "correct-horse-battery-staple"
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(password_fixture), USER_A),
        )
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", migrated_database)
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEK", encoded)
    monkeypatch.setenv("GATESCOPE_RECOVERY_PEPPER", base64.urlsafe_b64encode(b"p" * 32).decode())
    monkeypatch.setenv("GATESCOPE_CURSOR_SIGNING_KEY", base64.urlsafe_b64encode(b"c" * 32).decode())
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEY_VERSION", "integration-v1")
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setenv("GATESCOPE_TELEGRAM_BOT_USERNAME", "gatescope_test_bot")
    monkeypatch.setenv("GATESCOPE_TELEGRAM_WEBHOOK_ID", "w" * 32)
    monkeypatch.setenv("GATESCOPE_TELEGRAM_WEBHOOK_SECRET", "s" * 32)
    # An authenticated member's one-use link must not depend on an OWNER-managed
    # static Telegram ID allowlist.
    monkeypatch.setenv("GATESCOPE_TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("GATESCOPE_LOGIN_WORKSPACES", "tenant-a,tenant-b")

    with TestClient(create_formal_app(), base_url="https://testserver") as client:
        research_paths = ("/api/v2/research/shadow-trades", "/api/v2/research/shadow-trades/summary",
                          f"/api/v2/research/backtests/{INSTRUMENT}/cases")
        for path in research_paths:
            assert client.get(path).status_code == 401
        for path in (*CANONICAL_WEB_ROUTES, *LEGACY_WEB_ROUTES):
            anonymous_page = client.get(path, follow_redirects=False)
            assert anonymous_page.status_code == 303
            assert anonymous_page.headers["location"] == "/login"

        workspace_page = client.get("/auth/workspaces")
        assert workspace_page.status_code == 200
        assert [item["slug"] for item in workspace_page.json()["items"]] == ["tenant-a", "tenant-b"]
        login = client.post(
            "/auth/login",
            json={"tenant": "tenant-a", "username": "alice", "password": password_fixture},
        )
        assert login.status_code == 200
        assert login.json()["mfa_required"]
        assert client.get("/api/v2/indicators/catalog").status_code == 403
        for path in research_paths:
            assert client.get(path).status_code == 403
        initial_methods = client.get("/auth/mfa-methods")
        assert initial_methods.status_code == 200
        assert initial_methods.json()["items"] == []
        csrf = client.cookies.get("gatescope_csrf")
        enrollment = client.post(
            "/auth/mfa-methods:enroll",
            headers={"X-CSRF-Token": csrf},
            json={"label": "Primary"},
        )
        assert enrollment.status_code == 200
        code, _ = totp_code(enrollment.json()["secret"])
        confirmed = client.post(
            f"/auth/mfa-methods/{enrollment.json()['method_id']}:confirm",
            headers={"X-CSRF-Token": csrf},
            json={"code": code},
        )
        assert confirmed.status_code == 200
        assert client.get("/api/v2/indicators/catalog").status_code == 200
        csrf = client.cookies.get("gatescope_csrf")

        for path in CANONICAL_WEB_ROUTES:
            direct_page = client.get(path, follow_redirects=False)
            assert direct_page.status_code == 200
            assert direct_page.headers["content-type"].startswith("text/html")
            assert b'<div id="root"></div>' in direct_page.content

        for legacy_path, canonical_path in LEGACY_WEB_ROUTES.items():
            moved_page = client.get(legacy_path, follow_redirects=False)
            assert moved_page.status_code == 308
            assert moved_page.headers["location"] == canonical_path

        fully_authenticated_cookies = list(client.cookies.jar)
        client.cookies.clear()
        password_only_login = client.post(
            "/auth/login",
            json={"tenant": "tenant-a", "username": "alice", "password": password_fixture},
        )
        assert password_only_login.status_code == 200
        assert password_only_login.json()["mfa_required"]
        partial_csrf = client.cookies.get("gatescope_csrf")
        listed_methods = client.get("/auth/mfa-methods")
        assert listed_methods.status_code == 200
        active_method_id = listed_methods.json()["items"][0]["id"]
        blocked_enrollment = client.post(
            "/auth/mfa-methods:enroll",
            headers={"X-CSRF-Token": partial_csrf},
            json={"label": "Attacker replacement"},
        )
        assert blocked_enrollment.status_code == 403
        assert blocked_enrollment.json()["error"]["code"] == "MFA_REQUIRED"
        blocked_removal = client.delete(
            f"/auth/mfa-methods/{active_method_id}",
            headers={"X-CSRF-Token": partial_csrf},
        )
        assert blocked_removal.status_code == 403
        assert blocked_removal.json()["error"]["code"] == "MFA_REQUIRED"
        client.cookies.clear()
        for cookie in fully_authenticated_cookies:
            client.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )
        csrf = client.cookies.get("gatescope_csrf")
        assert client.get("/api/v2/indicators/catalog").status_code == 200
        pattern_catalog = client.get("/api/v2/patterns/catalog")
        assert pattern_catalog.status_code == 200
        assert any(item["pattern_id"] == "double_bottom" for item in pattern_catalog.json()["items"])

        sources = client.get("/api/v2/data-sources")
        assert sources.status_code == 200
        assert {item["id"] for item in sources.json()["items"]} == {"gate", "coinglass", "telegram"}
        assert sources.json()["market_worker"]["state"] == "NOT_STARTED"
        missing_coinglass = client.post(
            "/api/v2/data-sources/coinglass:test", headers={"X-CSRF-Token": csrf},
        )
        assert missing_coinglass.status_code == 409
        assert missing_coinglass.json()["error"]["code"] == "COINGLASS_SECRET_REQUIRED"

        link = client.post(
            "/api/v2/telegram/link-tokens", headers={"X-CSRF-Token": csrf},
        )
        assert link.status_code == 201
        link_token = link.json()["deep_link"].split("start=", maxsplit=1)[1]
        rejected_webhook = client.post(
            f"/api/v2/telegram/webhook/{'w' * 32}",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json={"update_id": 1},
        )
        assert rejected_webhook.status_code == 403
        webhook = client.post(
            f"/api/v2/telegram/webhook/{'w' * 32}",
            headers={"X-Telegram-Bot-Api-Secret-Token": "s" * 32},
            json={
                "update_id": 2,
                "message": {
                    "text": f"/start {link_token}",
                    "from": {"id": 123456789, "username": "alice_tg"},
                    "chat": {"id": 123456789, "type": "private"},
                },
            },
        )
        assert webhook.status_code == 200
        assert webhook.json()["method"] == "sendMessage"
        connections = client.get("/api/v2/telegram/connections")
        assert connections.status_code == 200
        assert connections.json()["binding_policy"] == "AUTHENTICATED_MEMBER_SELF_SERVICE"
        assert connections.json()["bot_username"] == "gatescope_test_bot"
        pending_connection = connections.json()["items"][0]
        assert pending_connection["status"] == "PENDING_CONFIRMATION"
        next_code, _ = totp_code(enrollment.json()["secret"], at=int(time.time()) + 30)
        confirmed_connection = client.post(
            f"/api/v2/telegram/connections/{pending_connection['id']}:confirm",
            headers={"X-CSRF-Token": csrf},
            json={"code": next_code},
        )
        assert confirmed_connection.status_code == 200
        assert confirmed_connection.json()["status"] == "ACTIVE"
        channel = client.post(
            "/api/v2/notification-channels",
            headers={"X-CSRF-Token": csrf},
            json={"alias": "Private alerts", "connection_id": pending_connection["id"]},
        )
        assert channel.status_code == 201
        channel_id = channel.json()["id"]
        route_payload = {
            "name": "Critical and fills",
            "schema_version": 1,
            "channel_id": channel_id,
            "event_types": ["RISK_CRITICAL", "FILL"],
            "strategy_ids": [],
            "exchange_account_ids": [],
            "symbols": ["BTC_USDT"],
            "environments": ["PAPER"],
            "minimum_severity": "INFO",
            "quiet_hours": {"timezone": "Asia/Shanghai", "start": "23:00", "end": "07:00"},
            "digest_mode": False,
            "max_per_minute": 10,
            "dedupe_seconds": 60,
            "bypass_quiet_rules": [{"event_type": "RISK_CRITICAL", "environments": []}],
            "enabled": True,
        }
        notification_route = client.post(
            "/api/v2/notification-routes",
            headers={"X-CSRF-Token": csrf},
            json=route_payload,
        )
        assert notification_route.status_code == 201
        assert notification_route.headers["etag"] == '"1"'
        route_id = notification_route.json()["id"]
        route_payload["max_per_minute"] = 5
        updated_route = client.patch(
            f"/api/v2/notification-routes/{route_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json=route_payload,
        )
        assert updated_route.status_code == 200
        assert updated_route.headers["etag"] == '"2"'
        stale_route = client.patch(
            f"/api/v2/notification-routes/{route_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json=route_payload,
        )
        assert stale_route.status_code == 412
        test_delivery = client.post(
            f"/api/v2/notification-channels/{channel_id}/test",
            headers={"X-CSRF-Token": csrf},
        )
        assert test_delivery.status_code == 202
        operation = client.get(f"/api/v2/operations/{test_delivery.json()['operation_id']}")
        assert operation.status_code == 200
        assert operation.json()["status"] == "QUEUED"

        created = client.post(
            "/api/v2/users",
            headers={"X-CSRF-Token": csrf},
            json={
                "username": "charlie", "initial_password": "charlie-initial-password",
                "role": "VIEWER",
            },
        )
        assert created.status_code == 201
        memberships = client.get("/api/v2/memberships")
        assert memberships.status_code == 200
        owner = next(item for item in memberships.json()["items"] if item["username"] == "Alice")
        demotion = client.patch(
            f"/api/v2/memberships/{owner['id']}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"role": "VIEWER", "status": "ACTIVE"},
        )
        assert demotion.status_code == 409
        assert demotion.json()["error"]["code"] == "LAST_OWNER_REQUIRED"

        layout = client.post(
            "/api/v2/chart-layouts",
            headers={"X-CSRF-Token": csrf},
            json={"name": "Main desk", "spec": {"panes": [{"id": "price"}]}},
        )
        assert layout.status_code == 201
        assert layout.headers["etag"] == '"1"'
        layout_id = layout.json()["id"]
        changed = client.patch(
            f"/api/v2/chart-layouts/{layout_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"name": "Main desk", "spec": {"panes": [{"id": "price"}, {"id": "volume"}]}},
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] == '"2"'
        stale = client.patch(
            f"/api/v2/chart-layouts/{layout_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"name": "Stale", "spec": {}},
        )
        assert stale.status_code == 412
        deleted = client.delete(
            f"/api/v2/chart-layouts/{layout_id}", headers={"X-CSRF-Token": csrf},
        )
        assert deleted.status_code == 200
        assert client.get(f"/api/v2/chart-layouts/{layout_id}").status_code == 404

        watchlist = client.post(
            "/api/v2/watchlists",
            headers={"X-CSRF-Token": csrf},
            json={"name": "Primary"},
        )
        assert watchlist.status_code == 201
        assert watchlist.headers["etag"] == '"1"'
        watchlist_id = watchlist.json()["id"]
        with_item = client.post(
            f"/api/v2/watchlists/{watchlist_id}/items",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"instrument_id": str(INSTRUMENT), "default_timeframe": "15m"},
        )
        assert with_item.status_code == 201
        assert with_item.headers["etag"] == '"2"'
        assert with_item.json()["items"][0]["symbol"] == "BTC_USDT"
        removed = client.delete(
            f"/api/v2/watchlists/{watchlist_id}/items/{INSTRUMENT}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"2"'},
        )
        assert removed.status_code == 200
        assert removed.headers["etag"] == '"3"'
        assert removed.json()["items"] == []

        rule_payload = {
            "name": "BTC reversal",
            "condition": {
                "schema_version": 1,
                "instrument_id": str(INSTRUMENT),
                "timeframe": "15m",
                "pattern_ids": ["double_bottom", "bull_flag"],
                "match_mode": "ANY",
                "minimum_confidence": 0.7,
                "confirmed_only": True,
                "coincidence_bars": 3,
                "cooldown_seconds": 900,
            },
            "routing": {"schema_version": 1, "channel_ids": []},
        }
        alert_rule = client.post(
            "/api/v2/alert-rules", headers={"X-CSRF-Token": csrf}, json=rule_payload,
        )
        assert alert_rule.status_code == 201
        assert alert_rule.headers["etag"] == '"1"'
        rule_id = alert_rule.json()["id"]
        rule_payload["condition"]["minimum_confidence"] = 0.75
        versioned = client.post(
            f"/api/v2/alert-rules/{rule_id}/versions",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"condition": rule_payload["condition"], "routing": rule_payload["routing"]},
        )
        assert versioned.status_code == 201
        assert versioned.headers["etag"] == '"2"'
        assert versioned.json()["current_version"] == 2
        stale_rule = client.patch(
            f"/api/v2/alert-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
            json={"name": "stale"},
        )
        assert stale_rule.status_code == 412
        paused = client.post(
            f"/api/v2/alert-rules/{rule_id}/pause",
            headers={"X-CSRF-Token": csrf, "If-Match": '"2"'},
        )
        assert paused.status_code == 200
        assert paused.headers["etag"] == '"3"'
        assert paused.json()["status"] == "PAUSED"
        resumed = client.post(
            f"/api/v2/alert-rules/{rule_id}/resume",
            headers={"X-CSRF-Token": csrf, "If-Match": '"3"'},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "ACTIVE"
        assert resumed.json()["current_version"] == 2
        assert resumed.headers["etag"] == '"4"'
        assert client.post(f"/api/v2/alert-rules/{rule_id}/resume",
            headers={"X-CSRF-Token": csrf, "If-Match": '"3"'}).status_code == 412
        rule_payload["name"] = "Updated BTC reversal"
        edited = client.put(f"/api/v2/alert-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"4"'}, json=rule_payload)
        assert edited.status_code == 200
        assert edited.json()["current_version"] == 3
        assert edited.json()["name"] == "Updated BTC reversal"
        assert edited.headers["etag"] == '"5"'
        assert client.put(f"/api/v2/alert-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"4"'}, json=rule_payload).status_code == 412
        assert client.delete(f"/api/v2/alert-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"4"'}).status_code == 412
        deleted = client.delete(f"/api/v2/alert-rules/{rule_id}",
            headers={"X-CSRF-Token": csrf, "If-Match": '"5"'})
        assert deleted.status_code == 200 and deleted.json()["history_preserved"]
        assert client.get(f"/api/v2/alert-rules/{rule_id}").status_code == 404
        notifications_page = client.get("/api/v2/notifications?limit=1&offset=0&status=SENT")
        assert notifications_page.status_code == 200
        assert "total" in notifications_page.json()
        assert client.get("/api/v2/notifications?offset=-1").status_code == 422

        mcp_client = client.post(
            "/api/v2/mcp/clients",
            headers={"X-CSRF-Token": csrf},
            json={"name": "Codex research"},
        )
        assert mcp_client.status_code == 201
        mcp_client_id = mcp_client.json()["id"]
        # Two prior sensitive actions consumed the current and next accepted
        # TOTP steps. Advance the verifier clock instead of weakening replay
        # protection or sleeping for a real minute in the integration suite.
        mcp_time = int(time.time()) + 60
        monkeypatch.setattr("gatescope.security_secrets.time.time", lambda: mcp_time)
        mcp_code, _ = totp_code(enrollment.json()["secret"], at=mcp_time)
        issued = client.post(
            f"/api/v2/mcp/clients/{mcp_client_id}/tokens",
            headers={"X-CSRF-Token": csrf},
            json={
                "label": "Local Codex",
                "scopes": ["mcp:connect", "market:read", "analysis:read", "alerts:read"],
                "ttl_hours": 24,
                "mfa_code": mcp_code,
            },
        )
        assert issued.status_code == 201
        assert issued.headers["cache-control"] == "no-store"
        raw_mcp_token = issued.json()["token"]
        assert raw_mcp_token.startswith("gsmcp_")
        verified = PostgresMcpRepository(client.app.state.pool).verify_token(raw_mcp_token)
        assert verified is not None
        assert verified.tenant_id == TENANT_A
        assert verified.subject_user_id == USER_A
        listed_clients = client.get("/api/v2/mcp/clients")
        assert listed_clients.status_code == 200
        assert listed_clients.json()["items"][0]["tokens"][0]["token_hint"] != raw_mcp_token

        # An OWNER's normal long-lived session must remain usable after the
        # sensitive-write window expires, without repeating login or losing MFA.
        current_session = client.app.state.identity.session(client.cookies.get(SESSION_COOKIE))
        assert current_session is not None
        with psycopg.connect(migrated_database) as connection:
            connection.execute(
                "UPDATE sessions SET recent_auth_at=now()-interval '11 minutes' WHERE id=%s",
                (current_session.session_id,),
            )
        expired = client.post("/api/v2/mcp/clients", headers={"X-CSRF-Token": csrf},
                              json={"name": "After password confirmation"})
        assert expired.status_code == 403
        assert expired.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
        assert client.post("/auth/reauthenticate", json={"password": password_fixture}).status_code == 403
        reauthenticated = client.post("/auth/reauthenticate", headers={"X-CSRF-Token": csrf},
                                       json={"password": password_fixture})
        assert reauthenticated.status_code == 200 and reauthenticated.json()["reauthenticated"]
        resumed_session = client.app.state.identity.session(client.cookies.get(SESSION_COOKIE))
        assert resumed_session is not None
        assert resumed_session.session_id == current_session.session_id
        assert resumed_session.mfa_verified_at == current_session.mfa_verified_at
        assert resumed_session.auth_strength == current_session.auth_strength
        assert resumed_session.recent_auth_at > current_session.recent_auth_at
        assert client.post("/api/v2/mcp/clients", headers={"X-CSRF-Token": csrf},
                           json={"name": "After password confirmation"}).status_code == 201


def test_mcp_owner_access_is_isolated_in_same_and_other_tenant(migrated_database: str) -> None:
    from datetime import timedelta
    from psycopg.conninfo import make_conninfo

    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            """INSERT INTO memberships (id,tenant_id,user_id,role,status,created_at,updated_at)
               VALUES ('10000000-0000-0000-0000-000000000088',%s,%s,'OWNER','ACTIVE',now(),now())""",
            (TENANT_A, USER_B),
        )
    api_pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    mcp_pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_mcp"))
    api_pool.open()
    mcp_pool.open()
    try:
        repo = PostgresMcpRepository(api_pool)
        verifier = PostgresMcpRepository(mcp_pool)
        own_client = repo.create_client(TENANT_A, USER_A, name="Own research client")
        own_token = repo.create_token(TENANT_A, USER_A, own_client.id, label="Read-only",
                                     scopes=["mcp:connect", "market:read"], ttl=timedelta(hours=1))
        assert verifier.verify_token(own_token.secret) is not None
        for tenant, user in ((TENANT_A, USER_B), (TENANT_B, USER_B), (TENANT_B, USER_A)):
            assert repo.list_clients(tenant, user) == []
            with pytest.raises(McpNotFound):
                repo.get_client(tenant, user, own_client.id)
            with pytest.raises(McpNotFound):
                repo.list_tokens(tenant, user, own_client.id)
            with pytest.raises(McpNotFound):
                repo.create_token(tenant, user, own_client.id, label="Not mine",
                                  scopes=["mcp:connect"], ttl=timedelta(hours=1))
            with pytest.raises(McpNotFound):
                repo.revoke_token(tenant, user, own_client.id, own_token.token.id)
            with pytest.raises(McpNotFound):
                repo.revoke_client(tenant, user, own_client.id)
        assert verifier.verify_token(own_token.secret) is not None
        assert repo.revoke_client(TENANT_A, USER_A, own_client.id).status == "REVOKED"
        assert verifier.verify_token(own_token.secret) is None
    finally:
        api_pool.close()
        mcp_pool.close()
