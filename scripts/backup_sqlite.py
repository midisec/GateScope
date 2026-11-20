#!/usr/bin/env python3
"""Create and verify an online SQLite backup plus a provenance manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CORE_SOURCES = ["app.py", "database.py", "gate_client.py", "scanner.py", "opportunity_scanner.py", "pattern_engine.py"]
COUNT_TABLES = [
    "instruments", "candles", "watchlist", "alert_rules", "alert_events",
    "opportunity_runs", "opportunity_tasks", "opportunity_results",
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    existing = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in COUNT_TABLES:
        if table in existing:
            result[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/gate_patterns.db")
    parser.add_argument("--output-root", default="artifacts/baseline")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = (root / args.database).resolve() if not Path(args.database).is_absolute() else Path(args.database)
    if not source.is_file():
        raise SystemExit(f"database does not exist: {source}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = (root / args.output_root).resolve() if not Path(args.output_root).is_absolute() else Path(args.output_root)
    destination = output_root / stamp
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup_path = destination / "gate_patterns.backup.sqlite3"

    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    source_counts = counts(source_db)
    target_db = sqlite3.connect(backup_path)
    source_db.backup(target_db)
    target_db.close()
    source_db.close()
    os.chmod(backup_path, 0o600)

    companions: dict[str, dict[str, object]] = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(source) + suffix)
        if candidate.is_file():
            copied = destination / ("source" + suffix + ".snapshot")
            shutil.copy2(candidate, copied)
            os.chmod(copied, 0o600)
            companions[candidate.name] = {"bytes": copied.stat().st_size, "sha256": digest(copied)}

    restored = sqlite3.connect(backup_path)
    integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
    restored_counts = counts(restored)
    restored.close()
    if integrity != "ok" or restored_counts != source_counts:
        raise SystemExit("backup verification failed")

    with tempfile.TemporaryDirectory(prefix="gatescope-restore-") as temp_dir:
        restored_copy = Path(temp_dir) / "restored.sqlite3"
        shutil.copy2(backup_path, restored_copy)
        check = sqlite3.connect(restored_copy)
        restore_integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        restore_counts = counts(check)
        check.close()
    if restore_integrity != "ok" or restore_counts != source_counts:
        raise SystemExit("isolated restore verification failed")

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "backup_file": backup_path.name,
            "bytes": backup_path.stat().st_size,
            "sha256": digest(backup_path),
            "integrity_check": integrity,
            "isolated_restore_check": restore_integrity,
            "row_counts": source_counts,
            "source_companions": companions,
        },
        "source_sha256": {
            relative: digest(root / relative) for relative in CORE_SOURCES if (root / relative).is_file()
        },
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    print(destination.relative_to(root))


if __name__ == "__main__":
    main()
