"""Bounded prefix-only pattern replay and explicit rule combination semantics."""
from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any, Callable

from gatescope.domain.market import Bar
from gatescope.indicators.patterns_v2 import CAUSAL_DETECTOR_VERSION, _series_id, _validated_event

PATTERN_WINDOW = 480
PIVOT_ORDER = 4


def rule_catalog() -> list[dict[str, Any]]:
    from pattern_engine import PATTERN_CATALOG
    return [
        {"id": "pullback", "name": "趋势回踩再入", "group": "指标策略", "direction": "neutral"},
        {"id": "volume_breakout", "name": "放量通道突破", "group": "指标策略", "direction": "neutral"},
        {"id": "momentum", "name": "波动过滤动量", "group": "指标策略", "direction": "neutral"},
        {"id": "trend", "name": "趋势跟随", "group": "指标策略", "direction": "neutral"},
        {"id": "breakout", "name": "通道突破", "group": "指标策略", "direction": "neutral"},
        {"id": "reversion", "name": "震荡回归", "group": "指标策略", "direction": "neutral"},
        *[{**p, "id": f"pattern:{p['id']}"} for p in PATTERN_CATALOG if not p.get("experimental")],
    ]


def combine_rules(votes: dict[str, int], selected: list[str], mode: str, direction: str) -> tuple[int, list[str], bool]:
    """Opposing votes always abstain, including when the account is long/short only."""
    active = {v for key, v in votes.items() if key in selected and v}
    conflict = len(active) > 1
    if conflict or not active or (mode == "ALL" and any(not votes.get(key) for key in selected)):
        return 0, [], conflict
    side = next(iter(active))
    if (side > 0 and direction == "SHORT") or (side < 0 and direction == "LONG"):
        return 0, [], False
    return side, [key for key in selected if votes.get(key) == side], False


class PatternReplay:
    """Do not scan the full future dataset, replay old confirmations, or retain an unbounded ledger."""
    def __init__(self, patterns: list[str], minimum: float, scanner: Callable[..., dict] | None = None):
        self.scanner = scanner
        self.patterns, self.minimum = patterns, minimum
        from gatescope.quant.pattern_study import WINDOW
        self.bars: deque[dict] = deque(maxlen=WINDOW if scanner is None else PATTERN_WINDOW)
        self.seen: dict[str, int] = {}
        self.lifecycle: dict[str, Any] = {}

    def append(self, bar: Bar, index: int, available: str) -> list[dict[str, Any]]:
        self.bars.append({"ts": int(bar.open_time.timestamp()), "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": float(bar.close), "volume": float(bar.volume)})
        if self.scanner is None:
            if len(self.bars)<20:
                return []  # Same warm-up and first absolute index as persisted replay.
            from gatescope.domain.market import TIMEFRAME_SECONDS
            from gatescope.quant.structure_detection import VERSION
            from gatescope.quant.structure_lifecycle import TERMINAL, observe
            from gatescope.quant.pattern_study import WINDOW
            self.lifecycle.setdefault("step_seconds", TIMEFRAME_SECONDS[bar.timeframe])
            transitions = observe(list(self.bars), len(self.bars) - 1, self.lifecycle,
                snapshot_at=available, source_mode="RESEARCH", index_offset=index-len(self.bars)+1)
            self.lifecycle["facts"] = []
            self.lifecycle["records"] = {key: record for key, record in self.lifecycle["records"].items()
                if record["phase"] not in TERMINAL or record["last_index"] >= index-WINDOW}
            self.lifecycle["sampling"] = {key: identifier for key, identifier in self.lifecycle.get("sampling", {}).items()
                if identifier in self.lifecycle["records"]}
            return [{**transition["record"]["latest"], "observed_index": index,
                "available_at": available, "detector_version": VERSION,
                "lifecycle_record": transition["record"]}
                for transition in transitions if transition["phase"] == "CONFIRMED"
                and transition["from_phase"] != "CONFIRMED"
                and transition["record"]["pattern"] in self.patterns
                and transition["record"]["latest"].get("confidence", 0) >= self.minimum]
        if len(self.bars) < 20:
            return []
        earliest = self.bars[0]["ts"]
        self.seen = {key: start for key, start in self.seen.items() if start >= earliest}
        result = self.scanner(list(self.bars), selected_patterns=self.patterns, min_confidence=self.minimum,
            confirmed_only=True, max_bars=PATTERN_WINDOW, max_events_per_pattern=8, pivot_order=PIVOT_ORDER,
            include_statistics=False)
        emitted = []
        for raw in result["events"]:
            event = _validated_event(raw, bar.open_time)
            if event["pattern"] not in self.patterns or not event["confirmed"] or event["confidence"] < self.minimum:
                continue
            identity = _series_id(event, bar.instrument_id, bar.timeframe)
            if identity in self.seen:
                continue
            self.seen[identity] = int(event["start_time"])
            payload = {**event, "observed_time": bar.open_time.isoformat(), "available_at": available,
                "observed_index": index, "series_id": identity, "detector_version": CAUSAL_DETECTOR_VERSION}
            payload["evidence_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            emitted.append(payload)
        return emitted
