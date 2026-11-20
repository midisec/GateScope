"""Causal, append-only facts around the frozen 26-pattern V1 detector.

V1 remains unchanged for compatibility. This adapter fixes pivot order and only
runs the detector on the prefix visible at the current watermark. A future bar
may supersede a fact, but can never mutate or delete an earlier fact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from gatescope.domain.market import Bar, TIMEFRAME_SECONDS, utc

PatternScanner = Callable[..., dict[str, Any]]
PATTERN_FACT_SCHEMA_VERSION = 1
CAUSAL_DETECTOR_VERSION = "2.0.0"
LEGACY_DETECTOR_VERSION = "1.0.0"


@dataclass(frozen=True)
class PatternEventFact:
    fact_id: str
    series_id: str
    revision: int
    supersedes_fact_id: str | None
    schema_version: int
    detector_version: str
    legacy_detector_version: str
    pivot_order: int
    instrument_id: str
    timeframe: str
    observed_through: datetime
    available_at: datetime
    input_digest: str
    input_quality_flags: tuple[str, ...]
    live_eligible: bool
    payload_hash: str
    payload_json: str

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise RuntimeError("stored pattern payload is invalid")
        return value


class CausalPatternLedger:
    def __init__(
        self,
        *,
        scanner: PatternScanner | None = None,
        selected_patterns: Sequence[str] | None = None,
        min_confidence: float = 0.55,
        confirmed_only: bool = False,
        pivot_order: int = 4,
        max_bars: int = 1_800,
        max_events_per_pattern: int = 160,
    ) -> None:
        if not 2 <= pivot_order <= 8:
            raise ValueError("pivot_order must be between 2 and 8")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        if max_bars < 20 or max_events_per_pattern < 1:
            raise ValueError("pattern history limits are invalid")
        if scanner is None:
            from pattern_engine import scan_patterns

            scanner = scan_patterns
        self.scanner = scanner
        self.selected_patterns = tuple(selected_patterns) if selected_patterns else None
        self.min_confidence = min_confidence
        self.confirmed_only = confirmed_only
        self.pivot_order = pivot_order
        self.max_bars = max_bars
        self.max_events_per_pattern = max_events_per_pattern
        self._bars: list[Bar] = []
        self._facts: list[PatternEventFact] = []
        self._latest: dict[str, PatternEventFact] = {}
        self._series: tuple[str, str] | None = None

    def append(self, bar: Bar, *, watermark: datetime | None = None) -> tuple[PatternEventFact, ...]:
        self._validate_bar(bar, watermark)
        self._bars.append(bar)
        retained = self._bars[-self.max_bars :]
        available_at = max(item.available_at for item in retained)
        candles = [
            {
                "ts": int(item.open_time.timestamp()),
                "open": float(item.open),
                "high": float(item.high),
                "low": float(item.low),
                "close": float(item.close),
                "volume": float(item.volume),
            }
            for item in retained
        ]
        result = self.scanner(
            candles,
            selected_patterns=self.selected_patterns,
            min_confidence=self.min_confidence,
            confirmed_only=self.confirmed_only,
            max_bars=self.max_bars,
            max_events_per_pattern=self.max_events_per_pattern,
            pivot_order=self.pivot_order,
        )
        events = result.get("events")
        if not isinstance(events, list):
            raise ValueError("pattern detector returned an invalid events collection")
        input_digest = _input_digest(retained)
        quality_flags = tuple(sorted({flag for item in retained for flag in item.quality_flags}))
        live_eligible = all(item.live_eligible_at(available_at) for item in retained)
        emitted: list[PatternEventFact] = []
        for raw_event in events:
            event = _validated_event(raw_event, bar.open_time)
            payload_json = _canonical_json(event)
            payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
            series_id = _series_id(event, bar.instrument_id, bar.timeframe)
            previous = self._latest.get(series_id)
            if previous is not None and previous.payload_hash == payload_hash:
                continue
            revision = previous.revision + 1 if previous else 1
            fact_id = hashlib.sha256(f"{series_id}:{revision}:{payload_hash}".encode()).hexdigest()
            fact = PatternEventFact(
                fact_id=fact_id,
                series_id=series_id,
                revision=revision,
                supersedes_fact_id=previous.fact_id if previous else None,
                schema_version=PATTERN_FACT_SCHEMA_VERSION,
                detector_version=CAUSAL_DETECTOR_VERSION,
                legacy_detector_version=LEGACY_DETECTOR_VERSION,
                pivot_order=self.pivot_order,
                instrument_id=bar.instrument_id,
                timeframe=bar.timeframe,
                observed_through=bar.close_time,
                available_at=available_at,
                input_digest=input_digest,
                input_quality_flags=quality_flags,
                live_eligible=live_eligible,
                payload_hash=payload_hash,
                payload_json=payload_json,
            )
            self._facts.append(fact)
            self._latest[series_id] = fact
            emitted.append(fact)
        return tuple(emitted)

    def facts(self) -> tuple[PatternEventFact, ...]:
        return tuple(self._facts)

    def current(self, *, as_of: datetime | None = None) -> tuple[PatternEventFact, ...]:
        if as_of is None:
            values = self._latest.values()
        else:
            watermark = utc(as_of)
            latest: dict[str, PatternEventFact] = {}
            for fact in self._facts:
                if fact.available_at <= watermark:
                    latest[fact.series_id] = fact
            values = latest.values()
        return tuple(sorted(values, key=lambda fact: (fact.available_at, fact.series_id)))

    def _validate_bar(self, bar: Bar, watermark: datetime | None) -> None:
        if not bar.is_final:
            raise ValueError("patterns only consume final bars")
        if watermark is not None and not bar.visible_at(utc(watermark)):
            raise ValueError("bar is not available at the requested watermark")
        series = bar.instrument_id, bar.timeframe
        if self._series is not None and series != self._series:
            raise ValueError("one pattern ledger can consume only one instrument/timeframe")
        if self._bars:
            previous = self._bars[-1]
            step = timedelta(seconds=TIMEFRAME_SECONDS[bar.timeframe])
            if bar.open_time != previous.open_time + step:
                raise ValueError("pattern input must be contiguous and revision-free; rebuild from facts")
        self._series = series


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("pattern event is not canonical JSON") from exc


def _validated_event(value: object, latest_open_time: datetime) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("pattern detector emitted a non-object event")
    required = {"pattern", "start_time", "end_time", "points", "confidence", "confirmed"}
    missing = required - set(value)
    if missing:
        raise ValueError(f"pattern event missing fields: {', '.join(sorted(missing))}")
    latest_epoch = int(latest_open_time.timestamp())
    timestamps = [int(value["start_time"]), int(value["end_time"])]
    points = value["points"]
    if not isinstance(points, list):
        raise ValueError("pattern event points must be a list")
    for point in points:
        if not isinstance(point, dict) or "time" not in point:
            raise ValueError("pattern event point is invalid")
        timestamps.append(int(point["time"]))
    timestamps.extend(_nested_timestamps(value))
    if any(timestamp > latest_epoch for timestamp in timestamps):
        raise ValueError("pattern detector attempted to emit a future observation")
    confidence = float(value["confidence"])
    if not 0 <= confidence <= 1:
        raise ValueError("pattern confidence must be between 0 and 1")
    return dict(value)


def _nested_timestamps(value: object) -> list[int]:
    timestamps: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"time", "start_time", "end_time"} and isinstance(item, (int, float)):
                timestamps.append(int(item))
            else:
                timestamps.extend(_nested_timestamps(item))
    elif isinstance(value, list):
        for item in value:
            timestamps.extend(_nested_timestamps(item))
    return timestamps


def _series_id(event: Mapping[str, Any], instrument_id: str, timeframe: str) -> str:
    structural_points = []
    points = event.get("points", [])
    if isinstance(points, list):
        for point in points:
            if isinstance(point, dict) and str(point.get("label", "")) != "✓":
                structural_points.append(
                    {
                        "label": str(point.get("label", "")),
                        "price": round(float(point["price"]), 8),
                        "time": int(point["time"]),
                    }
                )
    identity = {
        "instrument_id": instrument_id,
        "pattern": str(event["pattern"]),
        "points": structural_points,
        "start_time": int(event["start_time"]),
        "timeframe": timeframe,
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _input_digest(bars: Sequence[Bar]) -> str:
    digest = hashlib.sha256()
    for bar in bars:
        digest.update(bar.payload_hash.encode())
    return digest.hexdigest()
