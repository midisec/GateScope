#!/usr/bin/env python3
"""Rotate the local migration/test PostgreSQL login without printing its secret."""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from prepare_service_envs import read_env, write_env


ROLE = "gatescope_madmin"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    path = root / "data" / "runtime-secrets" / "formal-runtime.env"
    previous = read_env(path)
    migration_url = previous.get("GATESCOPE_MIGRATION_DATABASE_URL", "")
    test_url = previous.get("TEST_DATABASE_URL", "")
    if not migration_url or not test_url:
        raise RuntimeError("migration and test URLs are required")
    password = secrets.token_hex(32)
    changed = dict(previous)
    changed["GATESCOPE_MIGRATION_DATABASE_URL"] = replace_password(migration_url, password)
    changed["TEST_DATABASE_URL"] = replace_password(test_url, password)
    try:
        with psycopg.connect(migration_url.replace("postgresql+psycopg://", "postgresql://", 1)) as connection:
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(ROLE), sql.Literal(password))
            )
            write_env(path, changed)
    except Exception:
        write_env(path, previous)
        raise
    with psycopg.connect(
        changed["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    ) as connection:
        row = connection.execute("SELECT current_user").fetchone()
        if row is None or row[0] != ROLE:
            raise RuntimeError("rotated database login verification failed")
    password = ""
    print("Local PostgreSQL migration/test login rotated and verified; no runtime role changed")
    return 0


def replace_password(value: str, password: str) -> str:
    parsed = urlsplit(value)
    if parsed.username != ROLE or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("refusing to rotate a non-local or unexpected database login")
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(ROLE, safe='')}:{quote(password, safe='')}@{parsed.hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


if __name__ == "__main__":
    raise SystemExit(main())
