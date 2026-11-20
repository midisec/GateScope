"""Tenant admin statistics and mutation boundaries without database/network I/O."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from gatescope.application.admin_console import (
    AdminConsoleError, AdminConsoleService, BUSINESS_KEYS, SHARED_TABLES, require_owner, _visible_user_status,
)
from gatescope.domain.identity import Role


def actor(**changes):
    values = dict(tenant_id=uuid4(), user_id=uuid4(), session_id=uuid4(), role=Role.OWNER,
                  fully_authenticated=True, recent_auth_at=datetime.now(timezone.utc))
    return SimpleNamespace(**(values | changes))


class Connection:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        if sql.startswith("SET LOCAL"):
            return self
        assert self.answers, f"Unexpected statement: {sql}"
        marker, rows = self.answers.pop(0)
        assert marker in sql
        if isinstance(rows, Exception):
            raise rows
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


class Pool:
    def __init__(self, connection):
        self.connection, self.contexts, self.rollbacks = connection, [], 0

    @contextmanager
    def read_transaction(self, tenant, user):
        self.contexts.append(("read", tenant, user))
        yield self.connection

    @contextmanager
    def transaction(self, tenant, user):
        self.contexts.append(("write", tenant, user))
        try:
            yield self.connection
        except Exception:
            self.rollbacks += 1
            raise


def service(auth, api=None):
    identity = SimpleNamespace(pool=Pool(auth), _audit=Mock())
    return AdminConsoleService(Pool(api or Connection()), identity), identity


def scope(who):
    return ("SELECT t.id,t.slug,t.name", [(who.tenant_id, "test-workspace", "测试工作区")])


@pytest.mark.parametrize("who,code", [
    (None, "AUTH_REQUIRED"), (actor(fully_authenticated=False), "MFA_REQUIRED"),
    (actor(role=Role.OPERATOR), "ADMIN_OWNER_REQUIRED"),
    (actor(recent_auth_at=None), "RECENT_AUTH_REQUIRED"),
    (actor(recent_auth_at=datetime.now(timezone.utc)-timedelta(hours=1)), "RECENT_AUTH_REQUIRED"),
    (actor(recent_auth_at=datetime.now(timezone.utc)+timedelta(minutes=1)), "RECENT_AUTH_REQUIRED"),
    (actor(recent_auth_at=datetime.now()), "RECENT_AUTH_REQUIRED"),
])
def test_owner_and_recent_auth_fail_closed(who, code):
    with pytest.raises(AdminConsoleError) as failure:
        require_owner(who, recent=True)
    assert failure.value.code == code


def test_overview_uses_tenant_counts_separate_pools_and_seven_utc_days():
    who = actor()
    now = datetime.now(timezone.utc)
    auth = Connection(scope(who), ("FROM memberships m", [(4, 2, 1, 0, 1, 3, 1)]),
                      ("FROM sessions s", [(3, 2, 1)]),
                      ("FROM identity_audit_facts", [(now.date(), 2, 1, 0)]))
    api = Connection(("UNION ALL", [(key, index) for index, key in enumerate(BUSINESS_KEYS)]))
    app, identity = service(auth, api)
    result = app.overview(who)
    assert result["scope"]["tenant_id"] == str(who.tenant_id)
    assert result["members"]["active"] == 2
    assert result["sessions"] == dict(active=3, active_users=2, seen_within_15m=1, includes_pending_mfa=True)
    trend = result["login_trend"]
    assert trend["timezone"] == "UTC" and trend["basis"] == "RECORDED_PASSWORD_LOGIN_EVENTS"
    assert len(trend["items"]) == 7 and trend["items"][-1]["success"] == 2
    assert sum(row["success"] for row in trend["items"]) == 2
    assert identity.pool.contexts == [("read", who.tenant_id, who.user_id)]
    assert app.api_pool.contexts == [("read", who.tenant_id, who.user_id)]
    sql, params = api.calls[-1]
    assert len(params) == len(BUSINESS_KEYS) and set(params) == {who.tenant_id}
    assert "paper_orders" not in sql and "research_" not in sql
    assert "COUNT" not in sql or "bars" not in sql
    assert "occurred_at<=%s" in auth.calls[-1][0]
    member_sql, member_params = next(call for call in auth.calls if "count(*) FILTER(WHERE u.status='ACTIVE'" in call[0])
    assert "u.locked_until IS NULL OR u.locked_until<=%s" in member_sql
    assert "u.status='LOCKED' OR (u.status='ACTIVE' AND u.locked_until>%s)" in member_sql
    assert member_params == (datetime.fromisoformat(result["generated_at"]),)*2+(who.tenant_id,)
    assert "locked_until" not in next(sql for sql, _ in auth.calls if "FROM sessions s" in sql)


@pytest.mark.parametrize("status,predicate", [("ACTIVE", "u.status='ACTIVE' AND m.status='ACTIVE'"),
                                               ("LOCKED", "u.status='LOCKED' OR (u.status='ACTIVE' AND u.locked_until>%s)"),
                                               ("REVOKED", "m.status='REVOKED'")])
def test_users_literal_search_pagination_and_dual_status(status, predicate):
    who, other, membership = actor(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    auth = Connection(scope(who), ("SELECT count(*)", [(3,)]),
        ("SELECT u.id", [(other, "Reader", "reader", "ACTIVE", True, 2, now, membership, "VIEWER", "REVOKED", 3, None)]),
        ("SELECT s.user_id", [(other, now, now, 0)]))
    api = Connection(("UNION ALL", [(other, "watchlists", 2), (other, "paper_accounts", 1)]))
    app, _ = service(auth, api)
    result = app.users(who, q="  a%_\\  ", status=status, role="VIEWER", page=2, page_size=1)
    assert (result["page"], result["page_size"], result["total"], result["total_pages"], result["has_more"]) == (2, 1, 3, 3, True)
    item = result["items"][0]
    assert item["membership"] == dict(id=str(membership), role="VIEWER", status="REVOKED", etag=3)
    assert item["active_sessions"] == 0 and not item["is_self"]
    assert item["resources"]["watchlists"] == 2 and item["resources"]["alert_rules"] == 0
    listing = next(call for call in auth.calls if "SELECT u.id" in call[0])
    assert predicate in listing[0] and "m.tenant_id=%s" in listing[0]
    time_params = [datetime.fromisoformat(result["generated_at"])] if status in {"ACTIVE", "LOCKED"} else []
    assert listing[1] == [who.tenant_id, "%a\\%\\_\\\\%", "%a\\%\\_\\\\%", *time_params, "VIEWER", 1, 1]
    assert "a%_" not in listing[0] and "password_hash" not in listing[0]
    assert all(value == who.tenant_id for value in api.calls[-1][1][::2])
    assert all(value == [other] for value in api.calls[-1][1][1::2])


@pytest.mark.parametrize("status,offset,expected", [
    ("ACTIVE", None, "ACTIVE"), ("ACTIVE", -1, "ACTIVE"), ("ACTIVE", 0, "ACTIVE"),
    ("ACTIVE", 1, "LOCKED"), ("LOCKED", -1, "LOCKED"), ("DISABLED", 1, "DISABLED"),
])
def test_temporary_lock_presentation_expiry_boundary(status, offset, expected):
    now = datetime.now(timezone.utc)
    until = now+timedelta(seconds=offset) if offset is not None else None
    assert _visible_user_status(status, until, now) == expected


def test_temporarily_locked_member_is_displayed_locked_but_existing_sessions_remain_valid():
    who, other, membership = actor(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    until = now+timedelta(minutes=15)
    auth = Connection(scope(who), ("SELECT count(*)", [(1,)]),
        ("SELECT u.id", [(other, "Reader", "reader", "ACTIVE", True, 1, now, membership, "VIEWER", "ACTIVE", 1, until)]),
        ("SELECT s.user_id", [(other, now, now, 1)]))
    app, _ = service(auth, Connection(("UNION ALL", [])))
    result = app.users(who, status="LOCKED")
    assert result["items"][0]["status"] == "LOCKED"
    assert result["items"][0]["locked_until"] == until.isoformat()
    assert result["items"][0]["active_sessions"] == 1
    assert "locked_until" not in next(sql for sql, _ in auth.calls if "SELECT s.user_id" in sql)
    assert not any("UPDATE" in sql for sql, _ in auth.calls)


def test_empty_user_page_does_not_query_other_users_resources():
    who = actor()
    app, _ = service(Connection(scope(who), ("SELECT count(*)", [(0,)]), ("SELECT u.id", [])))
    result = app.users(who)
    assert result["items"] == [] and result["total_pages"] == 0
    assert not app.api_pool.contexts


@pytest.mark.parametrize("arguments", [dict(page=0), dict(page=True), dict(page_size=101), dict(page=10001),
                                         dict(q="x"*81), dict(role="SUPERUSER"), dict(status="UNKNOWN")])
def test_invalid_user_filters_do_not_touch_database(arguments):
    app, identity = service(Connection())
    with pytest.raises(AdminConsoleError) as failure:
        app.users(actor(), **arguments)
    assert failure.value.status == 422 and not identity.pool.contexts


def test_audit_projects_safe_fields_and_names_only_from_same_tenant_membership():
    who, subject = actor(), uuid4()
    auth = Connection(scope(who), ("SELECT count(*)", [(1,)]), ("SELECT a.id", [
        (uuid4(), datetime.now(timezone.utc), "admin.sessions.revoke", "SUCCESS", who.user_id, "Owner", subject, "Reader", "safe-trace")]))
    app, _ = service(auth)
    result = app.audit(who, action="admin.sessions.revoke", outcome="SUCCESS")
    assert set(result["items"][0]) == {"id", "occurred_at", "action", "outcome", "actor_user_id", "actor_username", "subject_user_id", "subject_username", "trace_id"}
    sql, params = auth.calls[-1]
    assert "am.tenant_id=a.tenant_id" in sql and "sm.tenant_id=a.tenant_id" in sql
    assert "a.tenant_id=%s" in sql and params[:3] == [who.tenant_id, "admin.sessions.revoke", "SUCCESS"]
    assert all(name not in sql for name in ("detail_json", "fact_hash", "password", "token_hash"))


def test_physical_storage_uses_catalogs_inherited_chunks_and_explicit_shared_labels(monkeypatch):
    who = actor()
    monkeypatch.setattr("gatescope.application.admin_console.shutil.disk_usage", lambda _: SimpleNamespace(total=1000, used=700, free=300))
    api = Connection(("pg_database_size", [(9000,)]), ("WITH RECURSIVE", [("bars", 1200, 300, 8, False)]))
    app, _ = service(Connection(), api)
    result = app._physical(who)
    assert result["scope"] == "SHARED_DATABASE" and result["not_per_user"]
    assert result["database_bytes"] == 9000 and result["row_count_kind"] == "ESTIMATE_NOT_EXACT"
    assert next(row for row in result["tables"] if row["table"] == "bars") == dict(table="bars", total_bytes=1200, estimated_rows=300, includes_descendants=True, available=True)
    missing = next(row for row in result["tables"] if row["table"] == "research_structure_inputs")
    assert missing["total_bytes"] is None and not missing["available"]
    assert result["disk"]["scope"] == "APPLICATION_HOST_FILESYSTEM" and result["disk"]["used_percent"] == 70
    assert "EXTERNAL_COMPRESSION_STORAGE_NOT_ATTRIBUTED" in result["warnings"]
    sql, params = api.calls[-1]
    assert "pg_inherits" in sql and "pg_stat_user_tables" in sql and "LIMIT 5001" in sql
    assert params == (list(SHARED_TABLES),)
    assert "FROM bars" not in sql and "FROM market_structure_stages" not in sql


def test_unavailable_storage_never_becomes_zero_or_exposes_internal_error(monkeypatch):
    def unavailable(_):
        raise OSError("sensitive-host-path")
    monkeypatch.setattr("gatescope.application.admin_console.shutil.disk_usage", unavailable)
    api = Connection(("pg_database_size", RuntimeError("sensitive-dsn")), ("WITH RECURSIVE", RuntimeError("secret-sql")))
    app, _ = service(Connection(), api)
    result = app._physical(actor())
    assert result["database_bytes"] is None and result["disk"]["total_bytes"] is None
    assert all(row["total_bytes"] is None and row["estimated_rows"] is None for row in result["tables"])
    assert set(result["warnings"]) >= {"DATABASE_SIZE_UNAVAILABLE", "TABLE_STORAGE_UNAVAILABLE", "APPLICATION_DISK_UNAVAILABLE"}
    assert all(value not in str(result) for value in ("sensitive", "secret-sql"))


def revoke_connection(who, target, *, current=True, member=True):
    return Connection(("pg_advisory_xact_lock", []), ("SELECT s.id FROM sessions", [(who.session_id,)] if current else []),
                      scope(who), ("SELECT id FROM memberships", [(uuid4(),)] if member else []),
                      ("WITH revoked AS", [(2,)]))


def test_revoke_self_is_forbidden_before_any_database_write():
    who = actor()
    app, identity = service(Connection())
    with pytest.raises(AdminConsoleError, match="SELF_SESSION_REVOKE_FORBIDDEN"):
        app.revoke_sessions(who, who.user_id, trace_id="test")
    assert not identity.pool.contexts


@pytest.mark.parametrize("current,member,code", [(False, True, "RECENT_AUTH_REQUIRED"), (True, False, "USER_NOT_FOUND")])
def test_revoke_rechecks_current_session_and_foreign_membership_without_mutation(current, member, code):
    who, target = actor(), uuid4()
    auth = revoke_connection(who, target, current=current, member=member)
    app, identity = service(auth)
    with pytest.raises(AdminConsoleError, match=code):
        app.revoke_sessions(who, target, trace_id="test")
    assert not any("UPDATE sessions" in sql for sql, _ in auth.calls)
    identity._audit.assert_not_called()
    assert identity.pool.rollbacks == 1


def test_revoke_locks_membership_namespace_then_updates_exact_tenant_and_audits_atomically():
    who, target = actor(), uuid4()
    auth = revoke_connection(who, target)
    app, identity = service(auth)
    result = app.revoke_sessions(who, target, trace_id="trace")
    assert result["revoked_sessions"] == 2 and result["audit_recorded"]
    lock = next(call for call in auth.calls if "pg_advisory" in call[0])
    assert lock[1] == (f"membership:{who.tenant_id}",)
    current = next(call for call in auth.calls if "SELECT s.id" in call[0])
    assert current[1][:3] == (who.tenant_id, who.user_id, who.session_id)
    assert "s.revoked_at IS NULL" in current[0] and "s.mfa_verified_at IS NOT NULL" in current[0]
    sql, params = auth.calls[-1]
    assert params[1:3] == (who.tenant_id, target) and "tenant_id=%s AND user_id=%s" in sql
    identity._audit.assert_called_once_with(auth, who.tenant_id, who.user_id, target, "admin.sessions.revoke", "SUCCESS", "trace")
    assert identity.pool.contexts == [("write", who.tenant_id, who.user_id)]


def test_audit_failure_rolls_back_session_revoke():
    who, target = actor(), uuid4()
    app, identity = service(revoke_connection(who, target))
    identity._audit.side_effect = RuntimeError("audit unavailable")
    with pytest.raises(RuntimeError):
        app.revoke_sessions(who, target, trace_id="test")
    assert identity.pool.rollbacks == 1
