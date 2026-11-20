"""Membership transaction ordering contracts; real PostgreSQL races run separately."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from uuid import UUID

import pytest

from gatescope.domain.identity import Role
from gatescope.formal_auth import (
    FormalAuthError, FormalIdentityService, FormalSession, IdentityPreconditionFailed, LastOwnerRequired,
)

TENANT, OTHER_TENANT = UUID(int=100), UUID(int=200)


class Result:
    def __init__(self, values=()):
        self.values = values

    def fetchone(self):
        return self.values[0] if self.values else None

    def fetchall(self):
        return list(self.values)


class Database:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.members = {
            UUID(int=i): {"tenant": TENANT, "user": UUID(int=i+10), "role": "OWNER", "status": "ACTIVE", "etag": 1}
            for i in (1, 2)
        }
        self.users = {UUID(int=i+10): "ACTIVE" for i in (1, 2)}
        self.sessions = {
            UUID(int=i+20): {"user": UUID(int=i+10), "tenant": TENANT, "revoked": False,
                             "recent": now, "expires": now+timedelta(hours=1), "mfa": True}
            for i in (1, 2)
        }
        self.tenant_active = True
        self.statements = []
        self.audits = []
        self.lock = Lock()
        self.barrier = None

    @contextmanager
    def transaction(self, tenant, user):
        connection = Connection(self, tenant, user)
        if self.barrier:
            self.barrier.wait(timeout=2)
        try:
            yield connection
        finally:
            if connection.locked:
                self.lock.release()


class Connection:
    def __init__(self, database, tenant, user):
        self.database, self.tenant, self.user = database, tenant, user
        self.locked = False

    def execute(self, statement, parameters=()):
        sql = " ".join(statement.split())
        db = self.database
        db.statements.append((sql, parameters))
        if sql.startswith("SET LOCAL"):
            return Result()
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            assert parameters == (f"membership:{self.tenant}",)
            assert db.lock.acquire(timeout=2)
            self.locked = True
            return Result()
        assert self.locked, "row reads and writes must follow the tenant membership lock"
        if sql.startswith("SELECT m.id FROM memberships m JOIN users u") and "JOIN sessions s" in sql:
            tenant, user, session_id, now, recent_cutoff = parameters
            session = db.sessions.get(session_id)
            member = next((value for value in db.members.values() if value["tenant"] == tenant and value["user"] == user), None)
            allowed = (member and member["status"] == "ACTIVE" and member["role"] == "OWNER"
                       and db.users.get(user) == "ACTIVE" and db.tenant_active and session
                       and session["user"] == user and session["tenant"] == tenant
                       and not session["revoked"] and session["expires"] > now
                       and session["recent"] >= recent_cutoff and session["mfa"])
            return Result([(UUID(int=1),)] if allowed else [])
        if sql.startswith("SELECT m.id FROM memberships m JOIN users u"):
            assert "ORDER BY m.id FOR UPDATE OF m" in sql
            return Result([(key,) for key, member in sorted(db.members.items())
                           if member["tenant"] == parameters[0] and member["role"] == "OWNER"
                           and member["status"] == "ACTIVE" and db.users[member["user"]] == "ACTIVE"])
        if sql.startswith("SELECT user_id, role, status, etag FROM memberships"):
            assert "WHERE id=%s AND tenant_id=%s FOR UPDATE" in sql
            identity, tenant = parameters
            member = db.members.get(identity)
            return Result([(member["user"], member["role"], member["status"], member["etag"])]) \
                if member and member["tenant"] == tenant else Result()
        if sql.startswith("UPDATE memberships SET"):
            role, status, _, identity, tenant = parameters
            assert db.members[identity]["tenant"] == tenant
            db.members[identity].update(role=role, status=status, etag=db.members[identity]["etag"]+1)
            return Result()
        if sql.startswith("UPDATE sessions SET revoked_at"):
            assert "WHERE tenant_id=%s AND user_id=%s" in sql
            _, tenant, user = parameters
            for session in db.sessions.values():
                if session["tenant"] == tenant and session["user"] == user:
                    session["revoked"] = True
            return Result()
        raise AssertionError(f"Unexpected membership SQL: {sql}")


def setup():
    database = Database()
    service = object.__new__(FormalIdentityService)
    service.pool = database
    service._audit = lambda *args: database.audits.append(args[4:6])
    now = datetime.now(timezone.utc)
    actors = [FormalSession(UUID(int=i+20), TENANT, UUID(int=i+10), f"owner{i}", Role.OWNER,
                            now+timedelta(hours=1), now, now, "MFA", True) for i in (1, 2)]
    return database, service, actors


def update(service, actor, member=2, *, role=Role.VIEWER, status="ACTIVE", etag=1):
    return service.update_membership(actor, UUID(int=member), role=role, status=status,
                                     expected_etag=etag, trace_id="membership-safety-test")


def test_lock_then_current_actor_then_ordered_owners_then_target_and_etag():
    database, service, actors = setup()
    result = update(service, actors[0])
    reads = [sql for sql, _ in database.statements if not sql.startswith("SET LOCAL")]
    assert reads[0].startswith("SELECT pg_advisory_xact_lock")
    assert "JOIN sessions s" in reads[1]
    assert "ORDER BY m.id FOR UPDATE OF m" in reads[2]
    assert "WHERE id=%s AND tenant_id=%s FOR UPDATE" in reads[3]
    assert result["role"] == "VIEWER" and result["etag"] == 2
    assert database.audits == [("membership.update", "SUCCESS")]


@pytest.mark.parametrize("state", ["role", "membership", "user", "tenant", "revoked", "expired", "recent", "mfa"])
def test_stale_formal_session_cannot_authorize_a_write(state):
    database, service, actors = setup()
    member, session = database.members[UUID(int=1)], database.sessions[actors[0].session_id]
    if state == "role": member["role"] = "VIEWER"
    if state == "membership": member["status"] = "REVOKED"
    if state == "user": database.users[actors[0].user_id] = "DISABLED"
    if state == "tenant": database.tenant_active = False
    if state == "revoked": session["revoked"] = True
    if state == "expired": session["expires"] -= timedelta(hours=2)
    if state == "recent": session["recent"] -= timedelta(minutes=11)
    if state == "mfa": session["mfa"] = False
    with pytest.raises(FormalAuthError, match="active OWNER"):
        update(service, actors[0])
    assert database.members[UUID(int=2)]["etag"] == 1
    assert not any(sql.startswith("UPDATE") for sql, _ in database.statements)


def test_last_usable_owner_cannot_be_removed_even_if_disabled_owner_membership_remains():
    database, service, actors = setup()
    database.users[actors[1].user_id] = "DISABLED"
    with pytest.raises(LastOwnerRequired):
        update(service, actors[0], 1)
    assert database.members[UUID(int=1)]["role"] == "OWNER"
    assert database.audits == [("membership.update", "DENIED")]


def test_self_demotion_is_allowed_with_another_active_owner_but_cannot_be_reused():
    database, service, actors = setup()
    assert update(service, actors[0], 1)["role"] == "VIEWER"
    with pytest.raises(FormalAuthError, match="active OWNER"):
        update(service, actors[0], 2)
    assert database.members[UUID(int=2)]["role"] == "OWNER"


def test_parallel_self_demotions_serialize_before_owner_count():
    database, service, actors = setup()
    database.barrier = Barrier(2)
    def remove(index):
        try:
            update(service, actors[index], index+1)
            return "updated"
        except LastOwnerRequired:
            return "last-owner"
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(remove, (0, 1)))
    assert sorted(outcomes) == ["last-owner", "updated"]
    assert sum(member["role"] == "OWNER" for member in database.members.values()) == 1


def test_stale_etag_and_cross_tenant_id_are_rejected_without_mutation():
    database, service, actors = setup()
    with pytest.raises(IdentityPreconditionFailed):
        update(service, actors[0], etag=9)
    database.members[UUID(int=2)]["tenant"] = OTHER_TENANT
    with pytest.raises(FormalAuthError, match="not found"):
        update(service, actors[0])
    assert database.members[UUID(int=2)]["etag"] == 1


def test_revocation_only_revokes_sessions_in_the_managed_workspace():
    database, service, actors = setup()
    database.sessions[UUID(int=999)] = {**database.sessions[actors[1].session_id], "tenant": OTHER_TENANT}
    result = update(service, actors[0], role=Role.OWNER, status="REVOKED")
    assert result["status"] == "REVOKED"
    assert database.sessions[actors[1].session_id]["revoked"]
    assert not database.sessions[UUID(int=999)]["revoked"]


def test_expired_cached_owner_auth_is_rejected_before_acquiring_locks():
    database, service, actors = setup()
    expired = replace(actors[0], recent_auth_at=datetime.now(timezone.utc)-timedelta(minutes=11))
    with pytest.raises(FormalAuthError, match="recent"):
        update(service, expired)
    assert not database.statements
