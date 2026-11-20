"""Read-only bridge from the legacy SQLite market cache.

The bridge deliberately treats every legacy candle as research-only because the
old schema did not record exchange finality or the true data availability time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import Connection

from gatescope.domain.market import Bar, TIMEFRAME_SECONDS

LEGACY_QUALITY_FLAGS = frozenset(
    {"LEGACY_SOURCE", "LEGACY_FINALITY_ASSUMED", "AVAILABLE_AT_UNKNOWN"}
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_instrument_uuid(legacy_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"gatescope:legacy:gate:{legacy_id}")


@dataclass(frozen=True)
class LegacySnapshot:
    source_path: str
    source_sha256: str
    table_counts: Mapping[str, int]
    instrument_count: int
    candle_count: int
    minimum_open_time: datetime | None
    maximum_open_time: datetime | None

    def canonical_digest(self) -> str:
        payload = {
            "candle_count": self.candle_count,
            "instrument_count": self.instrument_count,
            "maximum_open_time": self.maximum_open_time.isoformat() if self.maximum_open_time else None,
            "minimum_open_time": self.minimum_open_time.isoformat() if self.minimum_open_time else None,
            "source_sha256": self.source_sha256,
            "table_counts": dict(sorted(self.table_counts.items())),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ImportResult:
    snapshot_digest: str
    instruments_seen: int
    instruments_inserted: int
    bars_seen: int
    bars_inserted: int


class LegacySQLiteReader:
    """Open a SQLite snapshot with write operations disabled at two layers."""

    REQUIRED_TABLES = frozenset({"instruments", "candles"})

    def __init__(self, source_path: Path) -> None:
        source = source_path.resolve(strict=True)
        if not source.is_file():
            raise ValueError("legacy source must be a regular file")
        self.source_path = source

    def connect(self) -> sqlite3.Connection:
        uri = f"file:{self.source_path.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        self._validate_schema(connection)
        return connection

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = self.REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"legacy database missing required tables: {', '.join(sorted(missing))}")

    def snapshot(self) -> LegacySnapshot:
        before = sha256_file(self.source_path)
        with self.connect() as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {
                name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in names
            }
            bounds = connection.execute("SELECT MIN(ts), MAX(ts) FROM candles").fetchone()
        after = sha256_file(self.source_path)
        if before != after:
            raise RuntimeError("legacy source changed while it was being inspected")
        minimum = _epoch(bounds[0]) if bounds and bounds[0] is not None else None
        maximum = _epoch(bounds[1]) if bounds and bounds[1] is not None else None
        return LegacySnapshot(
            source_path=str(self.source_path),
            source_sha256=after,
            table_counts=counts,
            instrument_count=counts["instruments"],
            candle_count=counts["candles"],
            minimum_open_time=minimum,
            maximum_open_time=maximum,
        )

    def iter_instruments(self, *, batch_size: int = 1_000) -> Iterator[dict[str, Any]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        with self.connect() as connection:
            cursor = connection.execute(
                """SELECT id, market, settle, symbol, base, quote, status,
                          metadata_json, updated_at
                   FROM instruments ORDER BY id"""
            )
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield dict(row)

    def iter_bars(self, *, batch_size: int = 2_000) -> Iterator[Bar]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        with self.connect() as connection:
            cursor = connection.execute(
                """SELECT c.instrument_id, c.interval, c.ts, c.open, c.high,
                          c.low, c.close, c.volume, c.updated_at, i.market
                   FROM candles c JOIN instruments i ON i.id = c.instrument_id
                   ORDER BY c.instrument_id, c.interval, c.ts"""
            )
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    timeframe = str(row["interval"])
                    if timeframe not in TIMEFRAME_SECONDS:
                        raise ValueError(f"unsupported legacy timeframe: {timeframe}")
                    open_time = _epoch(row["ts"])
                    close_time = open_time + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                    last_recorded = _epoch(row["updated_at"])
                    yield Bar(
                        venue="GATE",
                        market=_formal_market(str(row["market"]), str(row["instrument_id"])),
                        instrument_id=str(legacy_instrument_uuid(str(row["instrument_id"]))),
                        timeframe=timeframe,
                        open_time=open_time,
                        close_time=close_time,
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=Decimal(str(row["volume"])),
                        is_final=True,
                        revision=1,
                        available_at=max(close_time, last_recorded),
                        quality_flags=LEGACY_QUALITY_FLAGS,
                    )


def import_legacy_market(
    reader: LegacySQLiteReader,
    connection: Connection[Any],
    *,
    batch_size: int = 1_000,
) -> ImportResult:
    """Idempotently copy legacy instruments/bars into the PostgreSQL foundation."""

    snapshot = reader.snapshot()
    instruments_seen = instruments_inserted = 0
    for legacy in reader.iter_instruments(batch_size=batch_size):
        instruments_seen += 1
        metadata = _metadata(legacy["metadata_json"])
        updated_at = _epoch(legacy["updated_at"])
        result = connection.execute(
            """INSERT INTO instruments
                 (id, venue, market, symbol, base_asset, quote_asset, settle_asset,
                  status, created_at, updated_at)
               VALUES (%s, 'GATE', %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                legacy_instrument_uuid(str(legacy["id"])),
                _formal_market(str(legacy["market"]), str(legacy["id"])),
                str(legacy["symbol"]),
                str(legacy["base"] or metadata.get("base") or "UNKNOWN"),
                str(legacy["quote"] or metadata.get("quote") or "UNKNOWN"),
                str(legacy["settle"] or "") or None,
                _formal_status(str(legacy["status"] or "")),
                updated_at,
                updated_at,
            ),
        )
        instruments_inserted += result.rowcount

    bars_seen = bars_inserted = 0
    for bar in reader.iter_bars(batch_size=batch_size):
        bars_seen += 1
        result = connection.execute(
            """INSERT INTO bars
                 (venue, market, instrument_id, timeframe, open_time, close_time,
                  open, high, low, close, volume, is_final, source_sequence,
                  revision, available_at, payload_hash, quality_flags, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (
                bar.venue, bar.market, UUID(bar.instrument_id), bar.timeframe,
                bar.open_time, bar.close_time, bar.open, bar.high, bar.low, bar.close,
                bar.volume, bar.is_final, bar.source_sequence, bar.revision,
                bar.available_at, bar.payload_hash, list(sorted(bar.quality_flags)),
                bar.available_at,
            ),
        )
        bars_inserted += result.rowcount
    return ImportResult(
        snapshot_digest=snapshot.canonical_digest(),
        instruments_seen=instruments_seen,
        instruments_inserted=instruments_inserted,
        bars_seen=bars_seen,
        bars_inserted=bars_inserted,
    )


def _epoch(value: object) -> datetime:
    if not isinstance(value, (int, float, str)):
        raise ValueError("legacy timestamp must be numeric")
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _metadata(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _formal_market(market: str, legacy_id: str) -> str:
    normalized = market.strip().lower()
    if normalized == "spot":
        return "SPOT"
    if normalized == "futures":
        parts = legacy_id.split(":")
        settle = parts[1].upper() if len(parts) >= 3 else ""
        if settle in {"USDT", "BTC"}:
            return f"FUTURES_{settle}"
    raise ValueError("unsupported legacy market")


def _formal_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"delisting", "disabled", "untradable"}:
        return "DISABLED"
    return "ACTIVE"
