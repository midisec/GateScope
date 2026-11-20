"""Shared canonical chart serialization for background warming and API reads."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS

def canonical_bar(
    instrument: dict[str, Any], instrument_id: str, timeframe: str, row: dict[str, Any],
) -> Bar:
    opened = datetime.fromtimestamp(int(row["ts"]), tz=timezone.utc)
    closed = opened + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    updated = datetime.fromtimestamp(int(row["updated_at"]), tz=timezone.utc)
    quality_flags = row.get("quality_flags")
    if not isinstance(quality_flags, (list, tuple, set, frozenset)):
        quality_flags = ["AVAILABLE_AT_UNKNOWN", "LEGACY_FINALITY_ASSUMED", "LEGACY_SOURCE"]
    return Bar(
        venue=str(row.get("venue") or "GATE"),
        market=str(row.get("market") or instrument["market"]).upper(), instrument_id=instrument_id,
        timeframe=timeframe, open_time=opened, close_time=closed,
        open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])), is_final=bool(row.get("is_final", True)),
        revision=int(row.get("revision", 1)),
        available_at=max(closed, updated),
        quality_flags=frozenset(str(flag) for flag in quality_flags),
    )


def bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "venue": bar.venue, "market": bar.market, "instrument_id": bar.instrument_id,
        "timeframe": bar.timeframe, "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(), "open": str(bar.open), "high": str(bar.high),
        "low": str(bar.low), "close": str(bar.close), "volume": str(bar.volume),
        "is_final": bar.is_final, "revision": bar.revision,
        "available_at": bar.available_at.isoformat(), "payload_hash": bar.payload_hash,
        "quality_flags": sorted(bar.quality_flags),
        "live_eligible": bar.live_eligible_at(datetime.now(timezone.utc)),
    }
