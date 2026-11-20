"""Gate's public monthly archive, persisted once and streamed in bounded pages.

No API key is accepted. Only a fixed official host is contacted; redirects are
rejected. Archive candles fill holes only; they never overwrite REST revisions.
"""

from __future__ import annotations

import csv
import gzip
import math
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from gate_client import GateRateLimited
from gatescope.domain.market import TIMEFRAME_SECONDS

HOST = "https://download.gatedata.org"
BASE_PERIOD = {
    "1m": "1m",
    "5m": "5m",
    "15m": "5m",
    "30m": "5m",
    "1h": "1h",
    "4h": "4h",
    "8h": "4h",
    "1d": "1d",
}
MAX_COMPRESSED = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_SYMBOL_CHARACTERS = 80
# Leave room below the common 255-byte filename limit for '-YYYYMM.csv.gz'.
MAX_SYMBOL_BYTES = 240
MAX_ARCHIVE_PAGE_BARS = 900


class ArchiveFormatError(ValueError):
    """Unsupported catalog identity, not a transient transport failure.

    Only fixed reason codes leave this adapter; never include the raw symbol or
    a URL in operational error records. Retry slowly in case catalog corrections
    become available while allowing other instruments to continue syncing.
    """

    retry_after = 86400

    def __init__(self, reason: str):
        if reason not in {"UNSUPPORTED_ARCHIVE_SYMBOL", "UNSUPPORTED_ARCHIVE_MARKET"}:
            raise ValueError("INVALID_ARCHIVE_FORMAT_REASON")
        self.reason = reason
        super().__init__(reason)


class ArchiveUnavailable(Exception):
    def __init__(self, start, end, reason="ARCHIVE_NOT_PUBLISHED", retry_after=21600):
        self.start, self.end, self.reason, self.retry_after = start, end, reason, retry_after


def month_bounds(ts: int):
    first = datetime.fromtimestamp(ts, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (first + timedelta(days=32)).replace(day=1)
    return int(first.timestamp()), int(following.timestamp())


def aggregate_rows(rows, source_step: int, target_step: int):
    """Only full UTC-aligned candles. Partial/gapped groups are discarded."""
    group = []
    bucket = None
    for row in rows:
        current = row[0] // target_step * target_step
        if bucket is not None and current != bucket:
            if len(group) == target_step // source_step and all(
                r[0] == bucket + i * source_step for i, r in enumerate(group)
            ):
                yield (
                    bucket,
                    group[0][1],
                    max(r[2] for r in group),
                    min(r[3] for r in group),
                    group[-1][4],
                    sum(r[5] for r in group),
                )
            group = []
        if bucket is not None and current < bucket:
            raise ValueError("ARCHIVE_NON_MONOTONE")
        bucket = current
        group.append(row)
    if (
        group
        and bucket is not None
        and len(group) == target_step // source_step
        and all(r[0] == bucket + i * source_step for i, r in enumerate(group))
    ):
        yield (
            bucket,
            group[0][1],
            max(r[2] for r in group),
            min(r[3] for r in group),
            group[-1][4],
            sum(r[5] for r in group),
        )


class GateHistoryArchive:
    def __init__(self, directory: str | Path, session=None):
        self.directory = Path(directory)
        self.session = session or requests.Session()
        self.session.trust_env = False

    def check_capacity(self):
        existing = self.directory
        while not existing.exists():
            existing = existing.parent
        if shutil.disk_usage(existing).free < 2 * 1024**3:
            raise ValueError("HISTORY_DISK_LOW")

    def _file(self, instrument, period, ts):
        symbol = instrument.get("symbol")
        # Exchange identifiers can contain Chinese and other Unicode letters.
        # Preserve their exact code points: normalization/case folding could
        # silently fetch another contract. Separators, dots, controls, combining
        # marks, percent escapes and URL metacharacters are not identifiers.
        if (
            not isinstance(symbol, str)
            or not 1 <= len(symbol) <= MAX_SYMBOL_CHARACTERS
            or not all(character == "_" or character.isalnum() for character in symbol)
            or len(symbol.encode("utf-8")) > MAX_SYMBOL_BYTES
        ):
            raise ArchiveFormatError("UNSUPPORTED_ARCHIVE_SYMBOL")
        source_market = instrument.get("market")
        settle = instrument.get("settle", "usdt")
        if source_market == "spot":
            market = "spot"
        elif source_market == "futures" and isinstance(settle, str):
            market = "futures_" + settle.lower()
        else:
            raise ArchiveFormatError("UNSUPPORTED_ARCHIVE_MARKET")
        if market not in {"spot", "futures_usdt", "futures_btc"}:
            raise ArchiveFormatError("UNSUPPORTED_ARCHIVE_MARKET")
        period = BASE_PERIOD[period]
        start, end = month_bounds(ts)
        month = datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m")
        filename = f"{symbol}-{month}.csv.gz"
        relative = f"{market}/candlesticks_{period}/{month}/{filename}"
        # Keep the local filename literal, but encode the complete URL segment
        # exactly once. The host and every parent path segment are fixed above.
        url = f"{HOST}/{market}/candlesticks_{period}/{month}/{quote(filename, safe='')}"
        path = self.directory / relative
        self.check_capacity()
        if path.is_file() and 0 < path.stat().st_size <= MAX_COMPRESSED:
            return path, start, end
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with self.session.get(url, stream=True, timeout=(3, 8), allow_redirects=False) as response:
                if response.status_code == 429:
                    value = response.headers.get("Retry-After", "30")
                    try:
                        delay = float(value)
                    except ValueError:
                        try:
                            delay = (
                                parsedate_to_datetime(value) - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            delay = 30
                    if not math.isfinite(delay) or delay > 31536000:
                        delay = 30
                    raise GateRateLimited(delay)
                if response.status_code == 404:
                    raise ArchiveUnavailable(start, end - 1)
                if response.status_code != 200:
                    raise ValueError("ARCHIVE_DOWNLOAD_FAILED")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > MAX_COMPRESSED:
                    raise ValueError("ARCHIVE_TOO_LARGE")
                size = 0
                started = time.monotonic()
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as handle:
                    temporary = Path(handle.name)
                    for block in response.iter_content(65536):
                        size += len(block)
                        if size > MAX_COMPRESSED or time.monotonic() - started > 12:
                            raise ValueError("ARCHIVE_DOWNLOAD_BUDGET")
                        handle.write(block)
                # Validate gzip/CSV before atomic promotion. Streamed validation is bounded.
                for _ in self._rows(temporary):
                    pass
                os.replace(temporary, path)
                temporary = None
            return path, start, end
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _rows(path):
        size = 0
        previous = None
        with gzip.open(path, "rt", newline="", encoding="utf-8") as handle:
            while line := handle.readline(4097):
                size += len(line)
                if len(line) > 4096 or size > MAX_EXPANDED:
                    raise ValueError("ARCHIVE_EXPANSION_LIMIT")
                values = next(csv.reader([line]))
                if not values:
                    continue
                # Official archive order: time, volume, close, high, low, open.
                if len(values) < 6:
                    raise ValueError("ARCHIVE_INVALID_ROW")
                try:
                    ts = int(values[0])
                    volume, close, high, low, opened = map(float, values[1:6])
                except ValueError:
                    if previous is None and values[0].lower() in {"time", "timestamp", "t"}:
                        continue
                    raise ValueError("ARCHIVE_INVALID_ROW") from None
                if (
                    not all(math.isfinite(v) for v in (volume, close, high, low, opened))
                    or min(close, high, low, opened) <= 0
                    or volume < 0
                    or high < max(opened, close, low)
                    or low > min(opened, close)
                ):
                    raise ValueError("ARCHIVE_INVALID_OHLC")
                if previous is not None and ts <= previous:
                    raise ValueError("ARCHIVE_NON_MONOTONE")
                previous = ts
                yield ts, opened, high, low, close, volume

    def fetch(self, instrument, period, start, end):
        # At most one month and <=900 target slots, including absent candles.
        # Bounding only the returned row count would allow an arbitrarily wide
        # sparse request and an unbounded number of inferred missing intervals.
        step = TIMEFRAME_SECONDS[period]
        month_start, month_end = month_bounds(end)
        left = (max(start, month_start) + step - 1) // step * step
        right = min(end // step * step, month_end - step)
        if left > right:
            raise ValueError("ARCHIVE_QUERY_RANGE_EMPTY")
        if (right - left) // step + 1 > MAX_ARCHIVE_PAGE_BARS:
            raise ValueError("ARCHIVE_PAGE_TOO_LARGE")
        path, month_start, month_end = self._file(instrument, period, end)
        source_step = TIMEFRAME_SECONDS[BASE_PERIOD[period]]
        rows = (r for r in self._rows(path) if left <= r[0] <= right + step - source_step)
        values = list(aggregate_rows(rows, source_step, step))
        available = {row[0] for row in values}
        missing_ranges = []
        missing_start = None
        for timestamp in range(left, right + step, step):
            if timestamp not in available:
                if missing_start is None:
                    missing_start = timestamp
            elif missing_start is not None:
                missing_ranges.append((missing_start, timestamp - step))
                missing_start = None
        if missing_start is not None:
            missing_ranges.append((missing_start, right))
        return values, {
            "source": "GATE_MONTHLY_ARCHIVE",
            "base_timeframe": BASE_PERIOD[period],
            "aggregated": source_step != step,
            "month_start": month_start,
            "month_end": month_end,
            "queried_from": left,
            "queried_to": right,
            "missing_ranges": missing_ranges,
        }
