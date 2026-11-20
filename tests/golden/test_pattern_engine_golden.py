from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from pattern_engine import PATTERN_CATALOG, _wilson_interval, scan_patterns

GOLDEN_DIR = Path(__file__).resolve().parent


def normalize_geometry(value):
    # BLAS implementations differ in the last bits of fitted line coordinates.
    # Keep identifiers, times, decisions and all other fields exactly as emitted.
    if isinstance(value, list):
        return [normalize_geometry(item) for item in value]
    if isinstance(value, dict):
        return {
            key: float(format(item, ".12g"))
            if key in {"price", "top_start", "top_end", "bottom_start", "bottom_end"} and isinstance(item, float)
            else normalize_geometry(item)
            for key, item in value.items()
        }
    return value


def deterministic_candles() -> list[dict]:
    candles = []
    for index in range(480):
        close = 100 + 0.018 * index + 4.2 * math.sin(index / 11.0) + 1.1 * math.sin(index / 3.7)
        open_ = close - 0.35 * math.sin(index / 2.3)
        high = max(open_, close) + 0.55 + 0.1 * math.sin(index)
        low = min(open_, close) - 0.55 - 0.1 * math.cos(index)
        candles.append(
            {
                "ts": 1_700_000_000 + index * 900,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": 1000 + 50 * math.sin(index / 5),
            }
        )
    return candles


def test_pattern_catalog_is_frozen() -> None:
    golden = json.loads((GOLDEN_DIR / "pattern_catalog_v1.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in PATTERN_CATALOG] == golden["catalog_ids"]
    assert len(PATTERN_CATALOG) == 26


def test_pattern_output_is_repeatable() -> None:
    golden = json.loads((GOLDEN_DIR / "pattern_engine_v1.json").read_text(encoding="utf-8"))
    result = scan_patterns(
        deterministic_candles(),
        min_confidence=0,
        confirmed_only=False,
        max_bars=500,
        max_events_per_pattern=20,
        include_statistics=True,
        performance_horizons=(5, 10, 20, 50),
    )
    raw = json.dumps(normalize_geometry(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert hashlib.sha256(raw).hexdigest() == golden["sha256_geometry_12_digits"]
    assert len(result["events"]) == golden["event_count"]
    assert Counter(event["pattern"] for event in result["events"]) == golden["counts_by_pattern"]


def test_statistics_golden() -> None:
    golden = json.loads((GOLDEN_DIR / "statistics_v1.json").read_text(encoding="utf-8"))
    actual = _wilson_interval(7, 10)
    assert [round(value, 6) for value in actual] == golden["wilson_7_of_10_95"]

