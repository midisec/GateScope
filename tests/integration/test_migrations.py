from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from database import Database
from gatescope.migrations import alembic_config, upgrade_database


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def test_phase0_migration_up_down_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The explicit SQLite target wins even when the parent process configures
    # an unrelated PostgreSQL service. No request may be sent to that service.
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused@127.0.0.1:1/must_not_connect")
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / "migration.db"
    Database(path)

    upgrade_database(root, path)
    assert {"auth_users", "auth_sessions", "security_audit_events"}.issubset(table_names(path))

    command.downgrade(alembic_config(root, path), "base")
    names = table_names(path)
    assert "auth_users" not in names
    assert "instruments" in names

    upgrade_database(root, path)
    assert "auth_users" in table_names(path)
