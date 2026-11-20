"""Bounded, JSON-only spill sequences for annual research.

Never pickle persisted/user-controlled objects. One compressed chunk and its
write buffer are resident per sequence; the private temporary directory is
removed by its context manager, including after ordinary calculation failures.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from gatescope.domain.market import Bar

CHUNK_ROWS = 512
MAX_CHUNK_BYTES = 16 * 1024 * 1024


def encode_chunk(items: list[Any]) -> tuple[bytes, str]:
    raw = json.dumps(items, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_CHUNK_BYTES:
        raise ValueError("RESEARCH_CHUNK_TOO_LARGE")
    return zlib.compress(raw, 6), hashlib.sha256(raw).hexdigest()


def decode_chunk(payload: bytes, digest: str) -> list[Any]:
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(payload, MAX_CHUNK_BYTES + 1)
    except zlib.error:
        raise ValueError("RESEARCH_CHUNK_INVALID") from None
    if len(raw) > MAX_CHUNK_BYTES or not decompressor.eof or decompressor.unused_data:
        raise ValueError("RESEARCH_CHUNK_INVALID")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("RESEARCH_CHUNK_DIGEST_MISMATCH")
    items = json.loads(raw)
    if not isinstance(items, list) or len(items) > CHUNK_ROWS:
        raise ValueError("RESEARCH_CHUNK_INVALID")
    return items


def bar_document(bar: Bar) -> dict[str, Any]:
    return {"venue": bar.venue, "market": bar.market, "instrument_id": bar.instrument_id,
        "timeframe": bar.timeframe, "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(), "available_at": bar.available_at.isoformat(),
        **{field: str(getattr(bar, field)) for field in ("open", "high", "low", "close", "volume")},
        "is_final": bar.is_final, "revision": bar.revision, "source_sequence": bar.source_sequence,
        "quality_flags": sorted(bar.quality_flags), "payload_hash": bar.payload_hash}


def document_bar(item: dict[str, Any]) -> Bar:
    return Bar(**{key: item[key] for key in ("venue", "market", "instrument_id", "timeframe", "is_final", "revision", "source_sequence")},
        **{key: datetime.fromisoformat(item[key]) for key in ("open_time", "close_time", "available_at")},
        **{key: Decimal(item[key]) for key in ("open", "high", "low", "close", "volume")},
        quality_flags=frozenset(item["quality_flags"]))


class SpillSequence(Sequence):
    def __init__(self, connection: sqlite3.Connection, name: str, *, encode: Callable = lambda value: value,
                 decode: Callable = lambda value: value) -> None:
        self.connection, self.name, self.encode, self.decode = connection, name, encode, decode
        self.buffer: list[Any] = []
        self.count = 0
        self.cache_index = -1
        self.cache: list[Any] = []

    def __len__(self) -> int:
        return self.count

    def append(self, value: Any) -> None:
        self.buffer.append(self.encode(value))
        self.count += 1
        if len(self.buffer) == CHUNK_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        index = (self.count - len(self.buffer)) // CHUNK_ROWS
        payload, digest = encode_chunk(self.buffer)
        self.connection.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?)",
            (self.name, index, len(self.buffer), payload, digest))
        self.connection.commit()
        # A partial flush is for finalization only; append would violate offsets.
        self.cache_index, self.cache = index, [self.decode(value) for value in self.buffer]
        self.buffer = []

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return [self[index] for index in range(*key.indices(self.count))]
        index = key + self.count if key < 0 else key
        if not 0 <= index < self.count:
            raise IndexError(index)
        start = self.count - len(self.buffer)
        if index >= start:
            return self.decode(self.buffer[index - start])
        chunk = index // CHUNK_ROWS
        if chunk != self.cache_index:
            row = self.connection.execute("SELECT payload,digest FROM chunks WHERE kind=? AND chunk_index=?",
                (self.name, chunk)).fetchone()
            if row is None:
                raise ValueError("RESEARCH_SPOOL_CHUNK_MISSING")
            self.cache_index, self.cache = chunk, [self.decode(value) for value in decode_chunk(*row)]
        return self.cache[index % CHUNK_ROWS]

    def __iter__(self) -> Iterator[Any]:
        for index in range(self.count):
            yield self[index]

    def chunks(self) -> Iterator[tuple[int, int, bytes, str]]:
        self.flush()
        yield from self.connection.execute("SELECT chunk_index,row_count,payload,digest FROM chunks WHERE kind=? ORDER BY chunk_index", (self.name,))


class ResearchSpool:
    def __init__(self) -> None:
        self.directory = TemporaryDirectory(prefix="gatescope-research-")
        self.connection = sqlite3.connect(Path(self.directory.name) / "spool.sqlite")
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA cache_size=-2048")
        self.connection.execute("PRAGMA max_page_count=131072")  # At most 512 MiB of temporary spool.
        self.connection.execute("CREATE TABLE chunks(kind TEXT,chunk_index INTEGER,row_count INTEGER,payload BLOB,digest TEXT,PRIMARY KEY(kind,chunk_index))")
        self.sequences: dict[str, SpillSequence] = {}

    def sequence(self, name: str, *, bars: bool = False) -> SpillSequence:
        if name in self.sequences:
            raise ValueError("RESEARCH_SEQUENCE_ALREADY_EXISTS")
        item = SpillSequence(self.connection, name, encode=bar_document if bars else lambda value: value,
            decode=document_bar if bars else lambda value: value)
        self.sequences[name] = item
        return item

    def __enter__(self) -> "ResearchSpool":
        return self

    def __exit__(self, *_: Any) -> None:
        self.connection.close()
        self.directory.cleanup()
