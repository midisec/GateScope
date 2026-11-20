#!/usr/bin/env python3
"""Provision a dedicated local PostgreSQL login for the strategy workload."""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from prepare_service_envs import read_env, write_env


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data" / "runtime-secrets" / "formal-runtime.env"
ROLE = "gatescope_strategy_login"


def main() -> int:
    values = read_env(RUNTIME)
    if values.get("GATESCOPE_STRATEGY_DATABASE_URL"):
        print("Strategy workload database login is already provisioned")
        return 0
    migration_url = values.get("GATESCOPE_MIGRATION_DATABASE_URL", "")
    if not migration_url:
        raise RuntimeError("GATESCOPE_MIGRATION_DATABASE_URL is required")
    password = secrets.token_urlsafe(36)
    connection_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(connection_url) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (ROLE,),
        ).fetchone()
        if exists is None:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS PASSWORD {}")
                .format(sql.Identifier(ROLE), sql.Literal(password))
            )
        else:
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(ROLE), sql.Literal(password),
                )
            )
        connection.execute(
            sql.SQL("GRANT gatescope_strategy TO {}").format(sql.Identifier(ROLE))
        )
    parsed = urlsplit(connection_url)
    hostname = parsed.hostname or "127.0.0.1"
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{quote(ROLE, safe='')}:{quote(password, safe='')}@{host}"
    values["GATESCOPE_STRATEGY_DATABASE_URL"] = urlunsplit(
        ("postgresql+psycopg", netloc, parsed.path, parsed.query, "")
    )
    write_env(RUNTIME, values)
    print("Strategy workload database login provisioned; password was not displayed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
