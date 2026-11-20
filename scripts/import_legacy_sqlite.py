#!/usr/bin/env python3
"""Import the old SQLite market cache without ever opening it writable."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import psycopg

from gatescope.adapters.legacy_sqlite import LegacySQLiteReader, import_legacy_market


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--batch-size", type=int, default=1_000)
    args = parser.parse_args()
    database_url = os.environ.get("GATESCOPE_DATABASE_URL")
    if not database_url:
        parser.error("GATESCOPE_DATABASE_URL is required")
    with stable_snapshot(args.source) as snapshot:
        reader = LegacySQLiteReader(snapshot)
        with psycopg.connect(database_url) as connection:
            result = import_legacy_market(reader, connection, batch_size=args.batch_size)
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


@contextmanager
def stable_snapshot(source: Path) -> Iterator[Path]:
    """Create a transactionally consistent copy even while the legacy worker is writing."""

    resolved = source.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("legacy source must be a regular file")
    with tempfile.TemporaryDirectory(prefix="gatescope-import-") as directory:
        destination = Path(directory) / "legacy-snapshot.db"
        uri = f"file:{resolved.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as source_connection:
            with sqlite3.connect(destination) as destination_connection:
                source_connection.backup(destination_connection)
        yield destination


if __name__ == "__main__":
    raise SystemExit(main())
