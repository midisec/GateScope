from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from gatescope.adapters.legacy_sqlite import (
    LEGACY_QUALITY_FLAGS,
    LegacySQLiteReader,
    _formal_status,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def legacy_database(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE instruments (
          id TEXT PRIMARY KEY, market TEXT, settle TEXT, symbol TEXT, base TEXT,
          quote TEXT, status TEXT, metadata_json TEXT, updated_at INTEGER
        );
        CREATE TABLE candles (
          instrument_id TEXT, interval TEXT, ts INTEGER, open REAL, high REAL,
          low REAL, close REAL, volume REAL, updated_at INTEGER
        );
        INSERT INTO instruments VALUES
          ('futures:usdt:BTC_USDT','futures','usdt','BTC_USDT','BTC','USDT','tradable','{}',1767225660);
        INSERT INTO candles VALUES
          ('futures:usdt:BTC_USDT','1m',1767225600,100,102,99,101,42,1767225665);
        """
    )
    connection.commit()
    connection.close()
    return path


def test_snapshot_and_iteration_never_change_source(legacy_database: Path) -> None:
    before = digest(legacy_database)
    reader = LegacySQLiteReader(legacy_database)
    snapshot = reader.snapshot()
    bars = list(reader.iter_bars())
    after = digest(legacy_database)

    assert before == after == snapshot.source_sha256
    assert snapshot.instrument_count == 1
    assert snapshot.candle_count == 1
    assert len(snapshot.canonical_digest()) == 64
    assert bars[0].quality_flags == LEGACY_QUALITY_FLAGS
    assert not bars[0].live_eligible_at(bars[0].available_at)
    assert bars[0].available_at.timestamp() == 1767225665


def test_connection_is_query_only(legacy_database: Path) -> None:
    connection = LegacySQLiteReader(legacy_database).connect()
    with connection, pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("DELETE FROM candles")


def test_legacy_status_is_normalized_for_formal_subscriptions() -> None:
    assert _formal_status("tradable") == "ACTIVE"
    assert _formal_status("unknown-provider-value") == "ACTIVE"
    assert _formal_status("delisting") == "DISABLED"
    assert _formal_status("untradable") == "DISABLED"
