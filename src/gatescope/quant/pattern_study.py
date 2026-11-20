"""Causal structure study, separate from executable strategies and trade evidence.

Only closed-bar prefixes enter the detector. A pivot is not an observation time.
Checkpointed first observations are never replaced by a nicer future geometry.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Callable

from gatescope.indicators.patterns_v2 import _series_id, _validated_event
from gatescope.quant.reporting import win_probability

VERSION = "structure-study-1"
WINDOW = 480
PIVOT_ORDER = 4
MINIMUM = .55
HORIZONS = (5, 10, 20, 50)
ROUND_TRIP_COST = .001  # Fixed research assumption: 10 bp, not actual exchange fees.
MAX_FACTS = 60000


def catalog() -> list[dict]:
    from pattern_engine import PATTERN_CATALOG
    from gatescope.quant.structure_rules import EXTRA_PATTERNS
    return [{**p, "group": "持续 / 反转" if "cup_handle" in p["id"] else p["group"],
             "family": "wave" if p.get("experimental") else "chart"} for p in [*PATTERN_CATALOG, *EXTRA_PATTERNS]]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _finite_number(value: Any, *, positive: bool = False) -> bool:
    # bool is a Real, but never a market price or candle timestamp. Very large
    # Python integers can also overflow math.isfinite's conversion to double.
    try:
        return (isinstance(value, Real) and not isinstance(value, bool)
                and math.isfinite(value) and (float(value) > 0 if positive else float(value) >= 0))
    except (TypeError, ValueError, OverflowError):
        return False


def strict_impulse(prices: list[float], bullish: bool, *, guidelines: bool = True) -> bool:
    """Validate known legs 0..2 through 0..5 of a standard impulse.

    This explicitly excludes diagonals/truncated fifths. A partial result does
    NOT assert that future legs will satisfy the completed third-wave rule.
    ``guidelines`` preserves the legacy aesthetic filter; production precision
    mode disables it. Fibonacci-like retracement ranges are not hard rules.
    """
    if (type(bullish) is not bool or not isinstance(prices, (list, tuple))
            or not 3 <= len(prices) <= 6 or any(not _finite_number(v, positive=True) for v in prices)):
        return False
    p = [v * (1 if bullish else -1) for v in prices]
    if p[1] <= p[0] or not p[0] < p[2] < p[1]:
        return False
    if guidelines and not .15 <= (p[1] - p[2]) / (p[1] - p[0]) <= .88:
        return False
    if len(p) >= 4 and p[3] <= p[1]:
        return False
    if len(p) >= 5 and (not p[1] < p[4] < p[3] or (guidelines and not .12 <= (p[3] - p[4]) / (p[3] - p[2]) <= .75)):
        return False
    return len(p) < 6 or (p[5] > p[3] and p[3] - p[2] >= min(p[1] - p[0], p[5] - p[4]))


def _wave_prefix_valid(candles: list[dict]) -> bool:
    """Do not silently sort/drop input before declaring a causal partial wave.

    normalize_frame repairs arbitrary input for legacy display. This caller also
    uses the original last candle, so repair here could mix two time boundaries.
    Missing is_final remains compatible with the repository's closed-only rows.
    """
    previous = None
    for row in candles:
        if not isinstance(row, dict) or row.get("is_final", True) is not True:
            return False
        timestamp = row.get("ts")
        if (timestamp is None or not _finite_number(timestamp) or int(timestamp) != timestamp
                or (previous is not None and timestamp <= previous)):
            return False
        values = [row[key] if key in row else None for key in ("open", "high", "low", "close")]
        if any(not _finite_number(value, positive=True) for value in values):
            return False
        opened, high, low, close = values
        if opened is None or low is None or high is None or close is None:
            return False
        if not low <= min(opened, close) <= max(opened, close) <= high:
            return False
        # Volume is optional for geometry-only callers, just as in refine().
        # If supplied it must be usable, but absence must not erase valid waves.
        if "volume" in row and not _finite_number(row["volume"]):
            return False
        previous = timestamp
    return True


def adapt_event(raw: dict, candles: list[dict]) -> dict | None:
    event = _validated_event(raw, datetime.fromtimestamp(candles[-1]["ts"], timezone.utc))
    event = {**event, "phase": "CONFIRMED" if event["confirmed"] else "FORMED"}
    event["signal_direction"] = event.get("signal_direction", event["direction"])
    points = [p for p in event["points"] if p.get("label") != "✓"]
    if event["pattern"].startswith(("elliott_impulse", "elliott_cycle")):
        if len(points) < 6 or not strict_impulse([p["price"] for p in points[:6]], event["pattern"].endswith("bull")):
            return None
    if event["pattern"].startswith("elliott_impulse"):
        # A completed upward impulse is NOT a confirmed buy. Study post-5 reversal.
        bullish = event["pattern"].endswith("bull")
        event["signal_direction"] = "bearish" if bullish else "bullish"
        event["confirmed"], event["phase"] = False, "FORMED"
        end = next(i for i, b in enumerate(candles) if b["ts"] == points[-1]["time"])
        trigger = points[-1]["price"] + .236 * (points[-2]["price"] - points[-1]["price"])
        for bar in candles[end + 1:end + 25]:
            if (bullish and bar["close"] < trigger) or (not bullish and bar["close"] > trigger):
                event["confirmed"], event["phase"] = True, "CONFIRMED"
                event["end_time"] = bar["ts"]
                mark = {"time": bar["ts"], "price": bar["close"], "label": "✓"}
                event["points"] = [*points, mark]
                event["lines"] = [*event["lines"], {"from": points[-1], "to": mark, "style": "dashed"}]
                break
        event["note"] = "五浪完成后观察反向修正；末浪回撤 23.6% 为本模型确认阈值"
    event["stage"] = "已确认突破 / 反转" if event["confirmed"] else "结构已形成 · 等待确认"
    if event["pattern"] in {"uptrend_channel", "downtrend_channel"}:
        event["stage"] = "通道已形成 · 无独立突破确认规则"
    return event


def forming_waves(candles: list[dict], *, precision_mode: bool = False, preserve_candidates: bool = False) -> list[dict]:
    """Only known pivot legs; never manufacture a future 5/C price or date."""
    from pattern_engine import ScanContext, alternating_pivots, calculate_atr, find_pivots, normalize_frame
    if len(candles) < 20 or not _wave_prefix_valid(candles):
        return []
    df = normalize_frame(candles)
    highs, lows = find_pivots(df, order=PIVOT_ORDER)
    volatility = calculate_atr(df)
    pivots = alternating_pivots(ScanContext(df, highs, lows, volatility, float(df.close.median())))
    result = []
    # A second top/bottom is provisional until its right-side pivot is known.
    # The final point is today's observed close, explicitly labelled "2?".
    if len(pivots) >= 2:
        first, neck = pivots[-2:]
        close, now = candles[-1]["close"], candles[-1]["ts"]
        top = first.kind == "high"
        depth = abs(first.price - neck.price)
        progress = ((close - neck.price) if top else (neck.price - close)) / depth if depth else 0
        formation_atr = volatility[neck.index] if preserve_candidates else volatility[-1]
        max_progress = 1 + .75 * formation_atr / depth if preserve_candidates and depth else 1.045
        if (6 <= neck.index - first.index <= 120 and neck.index >= len(df) - 40
                and (depth >= 1.5 * formation_atr if precision_mode else depth / first.price >= .012)
                and .4 <= progress <= max_progress):
            meta = next(p for p in catalog() if p["id"] == ("double_top" if top else "double_bottom"))
            pts = [{"time": int(df.iloc[first.index].time), "price": first.price, "label": "1"},
                {"time": int(df.iloc[neck.index].time), "price": neck.price, "label": "N"},
                {"time": now, "price": close, "label": "2?"}]
            result.append({"pattern": meta["id"], "name": meta["name"], "group": meta["group"], "direction": meta["direction"],
                "signal_direction": "neutral", "phase": "FORMING", "confirmed": False, "confidence": None,
                "start_time": pts[0]["time"], "end_time": now, "points": pts,
                "lines": [{"from": pts[0], "to": pts[1], "style": "solid"}, {"from": pts[1], "to": pts[2], "style": "dashed"}],
                "stage": "第二拐点待确认 · 2? 为最新收盘价", "note": "不将暂定第二拐点计入已完成结构统计"})
    for meta in catalog():
        if meta["family"] != "wave":
            continue
        bullish = meta["id"].endswith("bull")
        impulse = meta["id"].startswith("elliott_impulse")
        cycle = meta["id"].startswith("elliott_cycle")
        lengths = (5, 4, 3) if impulse else (8, 7) if cycle else (3,)
        for length in lengths:
            seq = pivots[-length:]
            if len(seq) != length or seq[-1].index < len(df) - 65:
                continue
            # An outside candle may be both a high and a low. OHLC cannot prove
            # their intrabar order; never turn them into two sequential waves.
            if any(a.index >= b.index for a, b in zip(seq, seq[1:], strict=False)):
                continue
            first_kind = "low" if (bullish if impulse or cycle else not bullish) else "high"
            if any(p.kind != (first_kind if i % 2 == 0 else "high" if first_kind == "low" else "low") for i, p in enumerate(seq)):
                continue
            if not 10 <= seq[-1].index - seq[0].index <= (420 if cycle else 260 if impulse else 180):
                continue
            prices = [p.price for p in seq]
            if (impulse or cycle) and not strict_impulse(prices[:6], bullish, guidelines=not precision_mode):
                continue
            if not impulse:
                abc = prices[5:] if cycle else prices
                if cycle and len(abc) == 2:
                    if (abc[1] - abc[0]) * (1 if bullish else -1) >= 0:
                        continue
                elif len(abc) == 3:
                    a, b = abs(abc[1] - abc[0]), abs(abc[2] - abc[1])
                    down = bullish
                    if not (abc[0] > abc[2] > abc[1] if down else abc[0] < abc[2] < abc[1]):
                        continue
                    if not a or not ((0 < b / a < 1) if precision_mode else (.22 <= b / a <= .88)):
                        continue
            labels = list("012345AB") if cycle else list("01234") if impulse else list("0AB")
            points = [{"time": int(df.iloc[p.index].time), "price": p.price, "label": labels[i]} for i, p in enumerate(seq)]
            result.append({"pattern": meta["id"], "name": meta["name"], "group": meta["group"], "direction": meta["direction"],
                "signal_direction": "neutral", "experimental": True, "phase": "FORMING", "confirmed": False,
                "confidence": None, "start_time": points[0]["time"], "end_time": points[-1]["time"], "points": points,
                "lines": [{"from": a, "to": b, "style": "dashed"} for a, b in zip(points, points[1:], strict=False)],
                "stage": f"已识别至 {labels[length - 1]} · 后续浪未形成", "note": "虚线仅连接已观察枢轴，不预测未来路径"})
            if not preserve_candidates:
                break
    return result


def detect(candles: list[dict], *, partial: bool = False, scanner: Callable | None = None) -> list[dict]:
    if len(candles) < 20:
        return []
    if scanner is None:
        from pattern_engine import scan_patterns
        scanner = scan_patterns
    candles = candles[-WINDOW:]
    raw = scanner(candles, min_confidence=MINIMUM, confirmed_only=False, max_bars=WINDOW,
                  max_events_per_pattern=24 if partial else 160, pivot_order=PIVOT_ORDER, include_statistics=False)
    events = [adapted for e in raw["events"] if (adapted := adapt_event(e, candles)) is not None]
    if partial:
        events.extend(forming_waves(candles))
    for event in events:
        event["id"] = _series_id(event, "public", "study") + ":" + event["phase"]
        event["evidence_id"] = digest(event)
    return sorted({e["id"]: e for e in events}.values(), key=lambda e: (e["end_time"], e["phase"] == "CONFIRMED"), reverse=True)


def observe(candles: list[dict], index: int, checkpoint: dict, *, detector: Callable = detect) -> None:
    prefix = candles[max(0, index + 1 - WINDOW):index + 1]
    earliest = prefix[0]["ts"]
    seen = {key: value for key, value in checkpoint.get("seen", {}).items() if value[0] >= earliest}
    facts = checkpoint.setdefault("facts", [])
    for e in detector(prefix):
        if e["phase"] == "FORMING":
            continue
        identity = _series_id(e, "public", "study")
        previous = seen.get(identity, [e["start_time"], 0])
        side = {"bullish": 1, "bearish": -1}.get(e["signal_direction"], 0)
        for phase, bit in (("FORMED", 1), ("CONFIRMED", 2)):
            if previous[1] & bit or (phase == "CONFIRMED" and not e["confirmed"]):
                continue
            facts.append([e["pattern"], phase, index, side, e["evidence_id"]])
            previous[1] |= bit
        seen[identity] = previous
    if len(facts) > MAX_FACTS:
        raise ValueError("PATTERN_STUDY_TOO_DENSE")
    checkpoint["seen"] = seen


def summarize(candles: list[dict], checkpoint: dict, metadata: dict) -> dict:
    grouped: dict = defaultdict(list)
    for fact in checkpoint.get("facts", []):
        grouped[(fact[0], fact[1])].append(fact)
    rows = []
    hypotheses = len(catalog()) * 2 * len(HORIZONS)
    for meta in catalog():
        for phase in ("FORMED", "CONFIRMED"):
            facts = sorted(grouped[(meta["id"], phase)], key=lambda f: (f[2], f[4]))
            for horizon in HORIZONS:
                returns, favorable, adverse, samples = [], [], [], []
                last_exit, pending, overlap, neutral = -1, 0, 0, 0
                for _, _, i, side, evidence in facts:
                    if not side:
                        neutral += 1
                        continue
                    if i + horizon >= len(candles):
                        pending += 1
                        continue
                    if i + 1 <= last_exit:
                        overlap += 1
                        continue
                    future = candles[i + 1:i + horizon + 1]
                    entry, exit_price = future[0]["open"], future[-1]["close"]
                    # Linear notional return for BOTH long and short, no reciprocal shortcut.
                    net = side * (exit_price - entry) / entry - ROUND_TRIP_COST
                    high, low = max(b["high"] for b in future), min(b["low"] for b in future)
                    returns.append(net)
                    favorable.append(max(0., ((high - entry) if side > 0 else (entry - low)) / entry))
                    adverse.append(min(0., ((low - entry) if side > 0 else (entry - high)) / entry))
                    samples.append({"observed_time": candles[i]["ts"] + metadata["step_seconds"], "entry_time": future[0]["ts"],
                        "exit_time": future[-1]["ts"] + metadata["step_seconds"], "side": side, "net_return": net,
                        "evidence_id": evidence})
                    last_exit = i + horizon
                probability = win_probability([{"net_pnl": r} for r in returns], attempts=hypotheses)
                rows.append({"pattern": meta["id"], "phase": phase, "horizon": horizon,
                    "detected": len(facts), "pending": pending, "overlap_excluded": overlap, "neutral_excluded": neutral,
                    "sample_count": len(returns), "win_rate": probability["win_rate"], "interval": probability.get("interval"),
                    "mean_net_return": sum(returns) / len(returns) if returns else None,
                    "mean_mfe": sum(favorable) / len(favorable) if favorable else None,
                    "mean_mae": sum(adverse) / len(adverse) if adverse else None,
                    "recent_samples": samples[-5:]})
    return {"version": VERSION, "catalog": catalog(), "bar_count": len(candles), "from_time": candles[0]["ts"],
        "to_time": candles[-1]["ts"] + metadata["step_seconds"], "window": WINDOW, "pivot_order": PIVOT_ORDER,
        "minimum_score": MINIMUM, "round_trip_cost": ROUND_TRIP_COST, "hypotheses": hypotheses,
        "availability_mode": "HISTORICAL_CLOSE", "metadata": metadata, "rows": rows}
