#!/usr/bin/env python3
"""Private, scoped PostgreSQL migration snapshot; never prints credentials.

Run only while all GateScope writers and its healthcheck timer are stopped.
The export includes GateScope roles, not other applications' credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from prepare_service_envs import SECRET_DIR, read_env


def inventory(conn):
    tables = conn.execute("SELECT schemaname, tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    return {
        "database": conn.execute("SELECT current_database()").fetchone()[0],
        "extensions": conn.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname").fetchall(),
        "tables": {f"{schema}.{table}": conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0] for schema, table in tables},
        "revision": conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall(),
    }


def private_write(path, content):
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def export_roles(conn):
    statements = ["-- Contains password hashes. Keep private; restore only into a fresh cluster."]
    roles = conn.execute("SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls,rolconnlimit,rolpassword,rolvaliduntil,(SELECT rolconfig FROM pg_roles r WHERE r.oid=a.oid) FROM pg_authid a WHERE rolname LIKE 'gatescope%' ORDER BY rolname").fetchall()
    for row in roles:
        name, *rest = row
        flags = [flag if value else "NO" + flag for flag, value in zip(("SUPERUSER", "INHERIT", "CREATEROLE", "CREATEDB", "LOGIN", "REPLICATION", "BYPASSRLS"), rest[:7], strict=True)]
        statement = sql.SQL("CREATE ROLE {} WITH {} CONNECTION LIMIT {} PASSWORD {}").format(sql.Identifier(name), sql.SQL(" ".join(flags)), sql.Literal(row[8]), sql.Literal(row[9]))
        if row[10] is not None:
            statement += sql.SQL(" VALID UNTIL {}").format(sql.Literal(str(row[10])))
        statements.append(statement.as_string(conn) + ";")
        for setting in row[11] or []:
            key, value = setting.split("=", 1)
            statements.append(sql.SQL("ALTER ROLE {} SET {} TO {};").format(sql.Identifier(name), sql.Identifier(key), sql.Literal(value)).as_string(conn))
    memberships = conn.execute("SELECT parent.rolname,child.rolname,m.admin_option FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname LIKE 'gatescope%' OR parent.rolname LIKE 'gatescope%'").fetchall()
    for parent, child, admin in memberships:
        if not child.startswith("gatescope") or not (parent.startswith("gatescope") or parent.startswith("pg_")):
            raise RuntimeError("Cross-application role membership requires manual review")
        statements.append(sql.SQL("GRANT {} TO {}{}").format(sql.Identifier(parent), sql.Identifier(child), sql.SQL(" WITH ADMIN OPTION" if admin else "")).as_string(conn) + ";")
    return "\n".join(statements) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("export", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    dsn = read_env(SECRET_DIR / "formal-runtime.env")["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT current_database()").fetchone()[0] != "gatescope":
            raise RuntimeError("Refusing non-production GateScope database")
        if args.mode == "verify":
            expected = json.loads((args.directory / "inventory.json").read_text())
            actual = json.loads(json.dumps(inventory(conn)))
            if expected != actual:
                differences = [key for key in actual["tables"] if actual["tables"][key] != expected["tables"].get(key)]
                print("Snapshot verification FAILED; differing tables:", differences)
                return 1
            print(f"Snapshot verified: {len(actual['tables'])} tables, {sum(actual['tables'].values())} rows; schema and extensions match")
            return 0
        args.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        private_write(args.directory / "roles.sql", export_roles(conn))
        name, owner, encoding, collate, ctype = conn.execute("SELECT datname,pg_get_userbyid(datdba),pg_encoding_to_char(encoding),datcollate,datctype FROM pg_database WHERE datname=current_database()").fetchone()
        create = sql.SQL("CREATE DATABASE {} WITH OWNER {} TEMPLATE template0 ENCODING {} LC_COLLATE {} LC_CTYPE {};\n").format(sql.Identifier(name), sql.Identifier(owner), sql.Literal(encoding), sql.Literal(collate), sql.Literal(ctype))
        private_write(args.directory / "database.sql", create.as_string(conn))
        before = inventory(conn)
        private_write(args.directory / "inventory.json", json.dumps(before, indent=2) + "\n")
        config = conninfo_to_dict(dsn)
        child_env = dict(os.environ)
        for key, target in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"), ("password", "PGPASSWORD"), ("dbname", "PGDATABASE"), ("sslmode", "PGSSLMODE")):
            if key in config:
                child_env[target] = config[key]
        dump = args.directory / "gatescope.dump"
        result = subprocess.run(["pg_dump", "--format=custom", "--file", str(dump)], env=child_env, capture_output=True)
        if result.returncode:
            raise RuntimeError("pg_dump failed (details suppressed to protect credentials)")
        if before != inventory(conn):
            raise RuntimeError("Database changed during export; stop all writers and retry")
        hashes = {}
        for path in sorted(args.directory.iterdir()):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            hashes[path.name] = digest.hexdigest()
        private_write(args.directory / "SHA256SUMS", "".join(f"{digest}  {name}\n" for name, digest in hashes.items()))
        print(f"Private snapshot complete: {len(before['tables'])} tables, {sum(before['tables'].values())} rows; dump bytes={dump.stat().st_size}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration snapshot failed: {type(exc).__name__}; no credential details logged")
        raise SystemExit(1) from None
