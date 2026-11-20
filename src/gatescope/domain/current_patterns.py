"""Read-only filtering and descriptive counts for current structure checkpoints.

Confidence here is the detector's existing geometry score, never historical win
probability. No filter changes candidate recognition, persistence, or execution.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


PHASES = ("FORMING", "FORMED", "CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED")


class InvalidCurrentPatternFilter(ValueError):
    """A caller filter is invalid, distinct from a historical-data error."""


def validate_confidence_filter(
    confidence_min: float | None,
    confidence_max: float | None,
    unscored_only: bool,
) -> None:
    for value in (confidence_min, confidence_max):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1
        ):
            raise InvalidCurrentPatternFilter("置信度范围必须是 0 到 1 之间的有限数值")
    if confidence_min is not None and confidence_max is not None and confidence_min > confidence_max:
        raise InvalidCurrentPatternFilter("置信度下限不能高于上限")
    if unscored_only and (confidence_min is not None or confidence_max is not None):
        raise InvalidCurrentPatternFilter("仅未评分与置信度范围不能同时设置")


def confidence_score(latest: dict[str, Any]) -> float | None:
    value = latest.get("confidence")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1):
        return None
    return float(value)


def record_pattern(record: dict[str, Any]) -> str:
    for value in (record.get("pattern"), record["latest"].get("pattern")):
        if isinstance(value, str) and value:
            return value
    return "unknown"


def filter_current_records(
    records: Iterable[dict[str, Any]],
    *,
    phase: str | None = None,
    family: str | None = None,
    query: str = "",
    pattern: str | None = None,
    confidence_min: float | None = None,
    confidence_max: float | None = None,
    unscored_only: bool = False,
) -> list[dict[str, Any]]:
    """Filter all candidates before the caller applies sorting and pagination."""
    validate_confidence_filter(confidence_min, confidence_max, unscored_only)
    needle = query.strip().casefold()
    filtered = []
    bounded = confidence_min is not None or confidence_max is not None
    for record in records:
        latest = record["latest"]
        if phase and record["phase"] != phase:
            continue
        if family and (family == "wave") != bool(latest.get("experimental")):
            continue
        if needle not in f'{latest.get("name", "")} {latest.get("group", "")}'.casefold():
            continue
        if pattern is not None and record_pattern(record) != pattern:
            continue
        score = confidence_score(latest)
        if unscored_only and score is not None:
            continue
        if bounded and (
            score is None
            or (confidence_min is not None and score < confidence_min)
            or (confidence_max is not None and score > confidence_max)
        ):
            continue
        filtered.append(record)
    return filtered


def summarize_current_records(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Describe every filtered candidate, without database/history lookups."""
    total = scored_count = 0
    directions = {"bullish": 0, "bearish": 0, "neutral": 0}
    phase_counts = dict.fromkeys(PHASES, 0)
    patterns: dict[str, dict[str, Any]] = {}
    for record in records:
        latest = record["latest"]
        pattern = record_pattern(record)
        score = confidence_score(latest)
        phase = record["phase"]
        direction = latest.get("signal_direction")
        direction = direction if direction in ("bullish", "bearish") else "neutral"
        total += 1
        scored_count += score is not None
        directions[direction] += 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if pattern not in patterns:
            patterns[pattern] = {
                "pattern": pattern,
                "name": latest.get("name") or pattern,
                "family": "wave" if latest.get("experimental") else "chart",
                "group": latest.get("group") or "未分类",
                "count": 0,
                "share": 0.,
                "directions": {"bullish": 0, "bearish": 0, "neutral": 0},
                "score_min": None,
                "score_max": None,
            }
        item = patterns[pattern]
        item["count"] += 1
        item["directions"][direction] += 1
        if score is not None:
            item["score_min"] = min(score, item["score_min"]) if item["score_min"] is not None else score
            item["score_max"] = max(score, item["score_max"]) if item["score_max"] is not None else score
    for item in patterns.values():
        item["share"] = item["count"] / total
    return {
        "total": total,
        "scored_count": scored_count,
        "unscored_count": total - scored_count,
        "directions": directions,
        "phase_counts": phase_counts,
        "patterns": sorted(patterns.values(), key=lambda item: (-item["count"], item["pattern"])),
    }
