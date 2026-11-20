"""Point-in-time market facts used by indicators and strategies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14_400, "8h": 28_800, "1d": 86_400,
}
LIVE_BLOCKING_QUALITY_FLAGS = frozenset(
    {"AVAILABLE_AT_UNKNOWN", "AVAILABLE_AT_APPROXIMATED", "LEGACY_FINALITY_ASSUMED", "GAP_BEFORE",
     "EXCHANGE_FINALITY_ASSUMED", "HISTORICAL_BACKFILL"}
)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def decimal_value(value: Decimal | str | int) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    return result


@dataclass(frozen=True)
class Bar:
    venue: str
    market: str
    instrument_id: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_final: bool
    revision: int
    available_at: datetime
    source_sequence: str | None = None
    quality_flags: frozenset[str] = field(default_factory=frozenset)
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("unsupported timeframe")
        for name in ("venue", "market", "instrument_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        open_time = utc(self.open_time)
        close_time = utc(self.close_time)
        available_at = utc(self.available_at)
        if close_time <= open_time:
            raise ValueError("close_time must be after open_time")
        expected = timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])
        if close_time - open_time != expected:
            raise ValueError("bar duration does not match timeframe")
        values = {name: decimal_value(getattr(self, name)) for name in ("open", "high", "low", "close", "volume")}
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            raise ValueError("OHLC values must be positive")
        if values["volume"] < 0:
            raise ValueError("volume cannot be negative")
        if values["high"] < max(values["open"], values["low"], values["close"]):
            raise ValueError("high violates OHLC invariant")
        if values["low"] > min(values["open"], values["high"], values["close"]):
            raise ValueError("low violates OHLC invariant")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)
        object.__setattr__(self, "available_at", available_at)
        for name, value in values.items():
            object.__setattr__(self, name, value)
        calculated = self.calculate_payload_hash()
        if self.payload_hash and self.payload_hash != calculated:
            raise ValueError("payload_hash does not match bar content")
        object.__setattr__(self, "payload_hash", calculated)

    @property
    def key(self) -> tuple[str, str, str, str, datetime]:
        return self.venue, self.market, self.instrument_id, self.timeframe, self.open_time

    def calculate_payload_hash(self) -> str:
        payload: dict[str, Any] = {
            "available_at": utc(self.available_at).isoformat(),
            "close": str(decimal_value(self.close)),
            "close_time": utc(self.close_time).isoformat(),
            "high": str(decimal_value(self.high)),
            "instrument_id": self.instrument_id,
            "is_final": self.is_final,
            "low": str(decimal_value(self.low)),
            "market": self.market,
            "open": str(decimal_value(self.open)),
            "open_time": utc(self.open_time).isoformat(),
            "quality_flags": sorted(self.quality_flags),
            "source_sequence": self.source_sequence,
            "timeframe": self.timeframe,
            "venue": self.venue,
            "volume": str(decimal_value(self.volume)),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def visible_at(self, watermark: datetime) -> bool:
        return self.is_final and self.available_at <= utc(watermark)

    def live_eligible_at(self, watermark: datetime) -> bool:
        return self.visible_at(watermark) and not (self.quality_flags & LIVE_BLOCKING_QUALITY_FLAGS)


@dataclass(frozen=True)
class Gap:
    instrument_id: str
    timeframe: str
    start_open_time: datetime
    end_open_time: datetime
    missing_count: int


class BarRevisionBook:
    """Append-only revision reducer; identical replays do not create revisions."""

    def __init__(self) -> None:
        self._facts: dict[tuple[str, str, str, str, datetime], list[Bar]] = {}

    def append(self, candidate: Bar) -> tuple[Bar, bool]:
        facts = self._facts.setdefault(candidate.key, [])
        if facts and facts[-1].payload_hash == candidate.payload_hash:
            return facts[-1], False
        expected_revision = len(facts) + 1
        if candidate.revision != expected_revision:
            raise ValueError(f"expected revision {expected_revision}")
        facts.append(candidate)
        return candidate, True

    def current(self) -> list[Bar]:
        return sorted((facts[-1] for facts in self._facts.values()), key=lambda bar: bar.open_time)

    def facts(self) -> tuple[Bar, ...]:
        return tuple(fact for facts in self._facts.values() for fact in facts)


class GapReconciler:
    def find_gaps(self, bars: list[Bar]) -> list[Gap]:
        if len(bars) < 2:
            return []
        ordered = sorted(bars, key=lambda bar: bar.open_time)
        first = ordered[0]
        step = timedelta(seconds=TIMEFRAME_SECONDS[first.timeframe])
        gaps: list[Gap] = []
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left.instrument_id != right.instrument_id or left.timeframe != right.timeframe:
                raise ValueError("a reconciliation series must contain one instrument/timeframe")
            missing = int((right.open_time - left.open_time) / step) - 1
            if missing > 0:
                gaps.append(Gap(first.instrument_id, first.timeframe, left.open_time + step, right.open_time - step, missing))
        return gaps

    def reconcile(self, bars: list[Bar], fetch: Any) -> list[Bar]:
        merged = {bar.open_time: bar for bar in bars}
        for gap in self.find_gaps(bars):
            for bar in fetch(gap.start_open_time, gap.end_open_time):
                if not isinstance(bar, Bar) or bar.instrument_id != gap.instrument_id or bar.timeframe != gap.timeframe:
                    raise ValueError("gap fetch returned an incompatible bar")
                merged[bar.open_time] = bar
        result = sorted(merged.values(), key=lambda bar: bar.open_time)
        if self.find_gaps(result):
            raise ValueError("gap reconciliation incomplete")
        return result

    def contiguous_watermark(self, bars: list[Bar]) -> datetime | None:
        visible = [bar for bar in sorted(bars, key=lambda bar: bar.open_time) if bar.is_final]
        if not visible or self.find_gaps(visible):
            return None
        return max(bar.available_at for bar in visible)
