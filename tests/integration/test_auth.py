from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import Database
from gatescope.auth import AuthService, InvalidCredentials
from gatescope.migrations import upgrade_database


@pytest.fixture
def auth(tmp_path: Path) -> tuple[Database, AuthService]:
    root = Path(__file__).resolve().parents[2]
    database = Database(tmp_path / "auth.db")
    upgrade_database(root, database.path)
    service = AuthService(database, session_ttl_seconds=900)
    assert service.bootstrap("owner", "correct-horse-battery-staple", "test")
    return database, service


def test_session_token_is_hashed_and_csrf_is_required(auth) -> None:
    database, service = auth
    new_session = service.authenticate("owner", "correct-horse-battery-staple", "trace-1")
    with database.connect() as connection:
        row = connection.execute("SELECT token_hash FROM auth_sessions").fetchone()
    assert row["token_hash"] != new_session.token
    loaded = service.session(new_session.token)
    assert loaded is not None
    assert service.valid_csrf(loaded, new_session.csrf_token, new_session.csrf_token)
    assert not service.valid_csrf(loaded, new_session.csrf_token, "wrong")


def test_failures_persist_and_lock_account(auth) -> None:
    database, service = auth
    for _ in range(service.MAX_FAILURES):
        with pytest.raises(InvalidCredentials):
            service.authenticate("owner", "this-is-the-wrong-password", "trace-denied")
    with database.connect() as connection:
        row = connection.execute("SELECT failed_attempts, locked_until FROM auth_users").fetchone()
    assert row["failed_attempts"] == service.MAX_FAILURES
    assert row["locked_until"] is not None
    with pytest.raises(InvalidCredentials):
        service.authenticate("owner", "correct-horse-battery-staple", "trace-locked")


def test_audit_table_is_append_only(auth) -> None:
    database, _ = auth
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.connect() as connection:
            connection.execute("UPDATE security_audit_events SET outcome='changed'")

