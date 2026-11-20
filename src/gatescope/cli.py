"""Administrative commands that never accept passwords on the command line."""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher

from gatescope.auth import AuthService, normalize_username, validate_password
from gatescope.migrations import upgrade_database


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GateScope security administration")
    parser.add_argument(
        "command",
        choices=["create-owner", "revoke-all-sessions", "create-formal-owner", "revoke-formal-sessions"],
    )
    parser.add_argument("--db", default=os.environ.get("GATESCOPE_DB_PATH", "data/gate_patterns.db"))
    parser.add_argument("--username", default=os.environ.get("GATESCOPE_BOOTSTRAP_USERNAME", "owner"))
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--tenant-name", default="Default Tenant")
    args = parser.parse_args()

    root = Path(os.environ.get("GATESCOPE_ROOT", Path.cwd())).resolve()
    if not (root / "alembic.ini").is_file():
        root = Path(__file__).resolve().parents[2]
    if args.command in {"create-formal-owner", "revoke-formal-sessions"}:
        _formal_command(root, args.command, args.tenant_slug, args.tenant_name, args.username)
        return
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = root / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_database(root, db_path)
    store = SqliteStore(db_path)

    if args.command == "create-owner":
        service = AuthService(store)
        if service.user_count() > 0:
            print("OWNER already exists")
            return
        password = getpass.getpass("New OWNER password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match")
        try:
            validate_password(password)
        except ValueError as exc:
            password = confirmation = ""
            raise SystemExit(f"Password rejected: {exc}") from None
        created = service.bootstrap(args.username, password, trace_id="admin-cli")
        password = confirmation = ""
        if not created:
            raise SystemExit("An OWNER already exists; Phase 0 permits exactly one local account")
        print("OWNER created")
        return

    with store.connect() as connection:
        connection.execute("UPDATE auth_sessions SET revoked_at=strftime('%s','now') WHERE revoked_at IS NULL")
    print("All sessions revoked")


def _formal_command(root: Path, operation: str, tenant_slug: str, tenant_name: str, username: str) -> None:
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("GATESCOPE_DATABASE_URL is required for formal identity commands")
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")
    connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if operation == "revoke-formal-sessions":
        with psycopg.connect(connection_url) as connection:
            changed = connection.execute(
                "UPDATE sessions SET revoked_at=now() WHERE revoked_at IS NULL"
            ).rowcount
        print(f"Revoked {changed} formal sessions")
        return

    normalized = normalize_username(username).lower()
    slug = tenant_slug.strip().lower()
    if not slug or not tenant_name.strip():
        raise SystemExit("Tenant slug and name are required")
    password = getpass.getpass("New formal OWNER password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    validate_password(password)
    password_hash = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(password)
    now_sql = "now()"
    tenant_id, user_id = uuid4(), uuid4()
    with psycopg.connect(connection_url) as connection:
        existing = connection.execute("SELECT COUNT(*) FROM memberships").fetchone()
        if existing is not None and int(existing[0]) > 0:
            raise SystemExit("Formal identity is already initialized")
        connection.execute(
            f"""INSERT INTO tenants (id, slug, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'ACTIVE', {now_sql}, {now_sql})""",
            (tenant_id, slug, tenant_name.strip()),
        )
        connection.execute(
            f"""INSERT INTO users
                 (id, normalized_username, display_username, password_hash, status,
                  mfa_required, password_changed_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'ACTIVE', true, {now_sql}, {now_sql}, {now_sql})""",
            (user_id, normalized, username.strip(), password_hash),
        )
        connection.execute(
            f"""INSERT INTO memberships
                 (id, tenant_id, user_id, role, status, created_at, updated_at)
                VALUES (%s, %s, %s, 'OWNER', 'ACTIVE', {now_sql}, {now_sql})""",
            (uuid4(), tenant_id, user_id),
        )
        connection.execute(
            f"""INSERT INTO password_history
                 (id, tenant_id, user_id, password_hash, created_at)
                VALUES (%s, %s, %s, %s, {now_sql})""",
            (uuid4(), tenant_id, user_id, password_hash),
        )
    password = confirmation = ""
    print("Formal OWNER created; MFA enrollment is required at first login")
