"""Private pre-release code/database backup; never prints database credentials.

Run on the deployment host before applying migrations. The output is a new,
0700 release directory; this command does not remove or restore any user data.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from prepare_service_envs import ROOT, SECRET_DIR, read_env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("full", "structure", "schema"), default="full",
                        help="structure backs up replay/live checkpoints; schema backs up DDL only, not stored data")
    args = parser.parse_args()
    os.umask(0o077)
    parent = ROOT / "data" / "release-backups"
    parent.mkdir(mode=0o700, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="event-repair-", dir=parent))
    source = read_env(SECRET_DIR / "formal-runtime.env")
    parsed = conninfo_to_dict(source["GATESCOPE_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1))
    env = os.environ.copy()
    for field, key in (("host", "PGHOST"), ("port", "PGPORT"), ("dbname", "PGDATABASE"),
                       ("user", "PGUSER"), ("password", "PGPASSWORD"), ("sslmode", "PGSSLMODE")):
        if field in parsed:
            env[key] = parsed[field]
    env["PGCONNECT_TIMEOUT"] = "10"
    backup = target / "database.dump"
    print("Private release backup started:", target, flush=True)
    commands = [
        ["nice", "-n", "10", "pg_dump", "--format=custom", "--compress=1",
         "--lock-wait-timeout=10s", "--file", str(backup),
         *(["--table=market_pattern_studies", "--table=market_pattern_tracking"] if args.scope == "structure"
           else ["--schema-only"] if args.scope == "schema" else [])],
        ["pg_restore", "--list", str(backup)],
        ["tar", "-czf", str(target / "code.tar.gz"), "--exclude=__pycache__",
         "src", "apps", "migrations", "scripts", "deploy", "web/dist", "web/src", "contracts",
         "pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json"],
    ]
    for index, command in enumerate(commands):
        result = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"backup stage {index + 1} failed; private error output suppressed")
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "scope": args.scope, "files": {}}
    for path in (backup, target / "code.tar.gz"):
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
        manifest["files"][path.name] = {"bytes": path.stat().st_size, "sha256": sha.hexdigest()}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Backup complete; archive list verified (not a full restore rehearsal):", target, flush=True)
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Backup aborted: {type(error).__name__}; no deployment applied", flush=True)
        raise SystemExit(1) from None
