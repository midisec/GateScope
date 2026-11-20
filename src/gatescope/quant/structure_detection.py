"""Versioned, volatility-normalized proposals; lifecycle owns confirmation.

V1 defaults/golden output remain untouched. Lines are analytical guides, not
fabricated candle pivots. Frozen levels are never silently fitted to future bars.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from gatescope.quant.pattern_study import WINDOW, PIVOT_ORDER, digest, forming_waves, strict_impulse

VERSION = "structure-study-3.3"
MIN_SCORE = .55
BUFFER_ATR = .15
CONFIRM_CLOSES = 2
BONUSES = {"double_top": .20, "double_bottom": .20, "triple_top": .20, "triple_bottom": .20,
    "head_shoulders": .20, "inverse_head_shoulders": .20, "cup_handle": .08, "inverse_cup_handle": .08,
    "bull_flag": .05, "bear_flag": .05, "bull_pennant": .05, "bear_pennant": .05,
    "elliott_correction_bull": .18, "elliott_correction_bear": .18, "elliott_cycle_bull": .16, "elliott_cycle_bear": .16}


def closed_input_error(candles: list[dict]) -> str | None:
    """Validate, never sort/drop/repair the prefix used as causal evidence.

    Canonical callers omit is_final because they already select closed bars.
    An explicit non-final flag must never be silently normalized away.
    """
    def finite(value):
        try:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            return False

    previous = None
    for bar in candles:
        if not isinstance(bar, dict):
            return "INVALID_CANDLE"
        ts = bar.get("ts")
        if not finite(ts) or ts < 0 or int(ts) != ts or (previous is not None and ts <= previous):
            return "INVALID_CANDLE_ORDER"
        previous = ts
        if "is_final" in bar and bar["is_final"] is not True:
            return "UNCONFIRMED_CANDLE"
        if any(not finite(bar.get(k)) or bar[k] <= 0 for k in ("open", "high", "low", "close")):
            return "INVALID_OHLC"
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            return "INVALID_OHLC"
        if "volume" in bar and (not finite(bar["volume"]) or bar["volume"] < 0):
            return "INVALID_VOLUME"
    return None


def atr(candles: list[dict]) -> float:
    rows = candles[-15:]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(rows, rows[1:], strict=False)]
    return max(sum(ranges) / max(1, len(ranges)), rows[-1]["close"] * 1e-8)


def known_points(event: dict) -> list[dict]:
    return [p for p in event["points"] if p.get("label") not in {"✓", "2?", "E"}]


def identity(event: dict) -> str:
    points = known_points(event)
    key = event["pattern"]
    if key in {"double_top", "double_bottom"}:
        points = points[:2]
    elif key.startswith("elliott_cycle"):
        points = points[:6]
    elif key.startswith("elliott"):
        points = points[:3]
    elif event.get("region") or key.endswith(("flag", "pennant")):
        points = sorted(points, key=lambda p: (p["time"], p["price"]))[:3]
    # Preserve full floating price precision, unlike V1's 8-decimal identity rounding.
    return digest([VERSION, key, [[p["time"], p["price"]] for p in points]])


def bucket(event: dict) -> str:
    score = event.get("confidence")
    return "UNSCORED" if score is None else "HIGH" if score >= .85 else "MID" if score >= .70 else "LOW"


def value_at(edge: dict, ts: int) -> float:
    a, b = edge["from"], edge["to"]
    return a["price"] if b["time"] == a["time"] else a["price"] + (b["price"] - a["price"]) * (ts - a["time"]) / (b["time"] - a["time"])


def edge(a: dict, b: dict, role: str, style: str = "dashed") -> dict:
    return {"from": a, "to": b, "style": style, "role": role}


def horizontal(event: dict, price: float, role: str, now: int) -> dict:
    return edge({"time": event["start_time"], "price": price, "label": ""}, {"time": now, "price": price, "label": ""}, role)


def refine(raw: dict, candles: list[dict], *, diagnostics: dict | None = None, atr_by_time: dict | None = None,
           _input_checked: bool = False) -> dict | None:
    def reject(reason):
        if diagnostics is not None:
            reasons = diagnostics.setdefault("rejected", {})
            reasons[reason] = reasons.get(reason, 0) + 1
        return None
    if not candles:
        return reject("INVALID_CANDLE")
    if not _input_checked and (error := closed_input_error(candles)):
        return reject(error)
    e = copy.deepcopy(raw)
    now, volatility = candles[-1]["ts"], atr(candles)
    by_time = {b["ts"]: b for b in candles}
    key = e["pattern"]
    e["phase"] = "FORMING" if raw.get("phase") == "FORMING" else "FORMED"
    e["confirmed"] = False  # No backward-dated V1 breakout may confirm a new candidate.
    e["points"] = [p for p in e["points"] if p.get("label") != "✓"]
    e["lines"] = [line for line in e["lines"] if line["from"].get("label") != "✓" and line["to"].get("label") != "✓"]
    if not e["points"] or any(p["time"] not in by_time or not math.isfinite(p["price"]) or p["price"] <= 0 for p in e["points"]):
        return reject("INVALID_ANCHOR")
    points = known_points(e)
    # Sequential waves cannot establish both a high and low in the same candle:
    # OHLC does not reveal their intrabar order. Do not fabricate vertical legs.
    if key.startswith("elliott") and any(a["time"] >= b["time"] for a,b in zip(points,points[1:], strict=False)):
        return reject("AMBIGUOUS_PIVOT_ORDER")
    if key.startswith("elliott_correction") and e["phase"]=="FORMING":
        # A is already known: its admission rule must match the complete ABC detector.
        a_atr = formation_atr(candles, points[1]["time"], atr_by_time)
        if abs(points[1]["price"]-points[0]["price"])<1.5*a_atr:
            return reject("ABC_AMPLITUDE")
    # Only admit newly knowable pivot structures. Boundaries/cups are evaluated NOW.
    if not e.get("region") and not key.endswith(("flag", "pennant")) and "cup_handle" not in key:
        last = max(p["time"] for p in points)
        if last < candles[max(0, len(candles) - PIVOT_ORDER - 9)]["ts"]:
            return reject("STALE_PIVOT")
    if key.startswith(("elliott_impulse", "elliott_cycle")):
        impulse = points[:6]
        if not strict_impulse([p["price"] for p in impulse], key.endswith("bull"), guidelines=False):
            return reject("IMPULSE_RULE")
    for p in e["points"]:
        b = by_time[p["time"]]
        if "cup_handle" in key:
            # Legacy left rim was an average placed at an unrelated candle.
            p["price"], p["basis"] = b["close"], "CLOSE"
        else:
            nearest = min(("HIGH", "LOW", "CLOSE"), key=lambda k: abs(b[k.lower()] - p["price"]))
            if abs(b[nearest.lower()] - p["price"]) > max(volatility * 1e-6, abs(p["price"]) * 1e-10):
                return reject("ANCHOR_OFF_OHLC")
            p["basis"] = nearest
    points = known_points(e)
    from gatescope.quant.structure_rules import audit
    error, e["recognition"] = audit(e, candles, volatility)
    if error:
        return reject(error)
    e["recognition"]["checks"].extend(["真实 OHLC 锚点", "已收盘输入契约与 OHLC 一致性"])
    if "cup_handle" in key:
        e["group"] = "持续 / 反转"
    if e["phase"] != "FORMING":
        bonus = BONUSES.get(key, 0 if key.startswith(("elliott_impulse", "uptrend_channel", "downtrend_channel")) or key.endswith("breakout_retest") else .12)
        e["confidence"] = max(0., min(1., (raw["confidence"] - bonus * bool(raw.get("confirmed"))) / (1 - bonus)))
    if max(p["price"] for p in points) - min(p["price"] for p in points) < 1.5 * volatility:
        return reject("AMPLITUDE")
    direction = raw.get("signal_direction") or raw["direction"]
    if key.startswith("elliott_impulse") and e["phase"] == "FORMED":
        direction = "bearish" if key.endswith("bull") else "bullish"
    if key in {"symmetrical_triangle", "rectangle"} or e["phase"] == "FORMING":
        direction = "neutral"
    e["signal_direction"] = direction
    for line in e["lines"]:
        line["role"] = "boundary" if e.get("region") or line["from"].get("label") in {"U", "L"} else "leg"
        if line["role"] == "leg":
            line["style"] = "dashed" if line["to"].get("label") == "2?" else "solid"
    levels: dict[str, Any] = {"buffer": BUFFER_ATR * volatility, "atr": volatility, "closes": CONFIRM_CLOSES,
        "expiry_bars": 144 if key.startswith("elliott_cycle") else 96 if key.startswith("elliott") else 48}
    if e.get("region") or key.endswith(("flag", "pennant")):
        boundaries = [x for x in e["lines"] if x["from"].get("label") in {"U", "L"}]
        uppers = [line for line in boundaries if line["from"].get("label") == "U"]
        lowers = [line for line in boundaries if line["from"].get("label") == "L"]
        if len(uppers) != 1 or len(lowers) != 1:
            return reject("BOUNDARIES_MISSING")
        upper, lower = uppers[0], lowers[0]
        # Sorting by price swapped already-crossed boundaries and hid the error.
        left = max(upper["from"]["time"], lower["from"]["time"])
        if any(value_at(upper,t) <= value_at(lower,t) for t in (left,now)):
            return reject("BOUNDARIES_CROSSED")
        residuals = []
        for p in points:
            if p.get("label") == "pole":
                continue
            boundary = upper if p["basis"] == "HIGH" else lower
            residuals.append(abs(p["price"] - value_at(boundary, p["time"])))
        rms = math.sqrt(sum(x*x for x in residuals) / max(1,len(residuals)))
        if value_at(upper, now) <= value_at(lower, now) or rms > .8 * volatility:
            return reject("BOUNDARY_RESIDUAL")
        e["recognition"]["checks"].append("边界触点残差通过 ATR 校验")
        e["confidence"] = .55*max(0,1-rms/(.8*volatility)) + .25*min(1,max(0,len(residuals)-4)/6) + .20*min(1,(value_at(upper,now)-value_at(lower,now))/(6*volatility))
        levels.update(upper=upper, lower=lower)
        if raw["direction"] != "neutral":
            levels["invalid_edge"] = lower if raw["direction"] == "bullish" else upper
            levels["invalid_side"] = -1 if raw["direction"] == "bullish" else 1
    else:
        if key in {"double_top", "double_bottom", "triple_top", "triple_bottom", "head_shoulders", "inverse_head_shoulders"}:
            necks = [p for p in points if str(p.get("label", "")).startswith("N")]
            if not necks:
                return reject("NECKLINE_MISSING")
            a, b = necks[0], necks[-1]
            neck = edge(a, b, "neckline")
            if a["time"] == b["time"]:
                neck = horizontal(e, a["price"], "neckline", now)
            else:
                neck = edge({"time": e["start_time"], "price": value_at(neck, e["start_time"]), "label": ""},
                    {"time": now, "price": value_at(neck, now), "label": ""}, "neckline")
            e["lines"] = [line for line in e["lines"] if line["style"] != "dashed" or line["to"].get("label") == "2?"] + [neck]
            levels["trigger"] = neck
        elif "cup_handle" in key:
            # The bowl is a rounded analytical guide through the real L/B/R
            # anchors, not a V-shaped pair of straight segments or new pivots.
            e["lines"] = [{**edge(a,b,"curve","solid"),"curve":{"vertex":points[1]}}
                for a,b in zip(points[:2],points[1:3], strict=True)]
            e["lines"] += [edge(a,b,"leg","solid") for a,b in zip(e["points"][2:],e["points"][3:], strict=False)]
            rim = max(points[0]["price"], points[2]["price"]) if raw["direction"] == "bullish" else min(points[0]["price"], points[2]["price"])
            levels["trigger"] = horizontal(e, rim, "neckline", now)
            e["lines"].append(levels["trigger"])
        elif key.endswith("breakout_retest"):
            sign = 1 if key.startswith("bull") else -1
            levels["trigger"] = horizontal(e, e["retest_level"]+sign*.5*e["retest_atr"], "confirmation", now)
            e["lines"].append(horizontal(e, e["retest_level"], "neckline", now))
            e["lines"].append(levels["trigger"])
        elif key.startswith("elliott") and e["phase"] == "FORMED":
            levels["trigger"] = horizontal(e, points[-1]["price"] + .236 * (points[-2]["price"] - points[-1]["price"]), "confirmation", now)
            e["lines"].append(levels["trigger"])
        # Invalidity is a prespecified model rule, not "the scanner stopped returning it".
        if key.startswith("elliott_impulse") and e["phase"] == "FORMING":
            bull = key.endswith("bull")
            price = points[1]["price"] if len(points) >= 4 else points[0]["price"]
            levels.update(invalid_price=price, invalid_side=-1 if bull else 1, hard=True)
            if len(points) == 5:
                wave1 = abs(points[1]["price"] - points[0]["price"])
                wave3 = abs(points[3]["price"] - points[2]["price"])
                if wave3 < wave1:
                    # Equal wave-3/5 lengths are permitted; only a longer fifth
                    # makes wave 3 the shortest under this fixed count.
                    sign = 1 if bull else -1
                    levels["hard_limits"] = [{"price": points[4]["price"] + sign * wave3,
                        "side": sign, "inclusive": False, "rule": "WAVE3_NOT_SHORTEST"}]
        else:
            side = 1 if (raw["direction"] == "bearish") else -1
            if key.startswith("elliott_impulse"):
                side = 1 if key.endswith("bull") else -1
            if key.startswith("elliott") and e["phase"] == "FORMING":
                price = points[5]["price"] if key.startswith("elliott_cycle") else points[0]["price"]
                side = 1 if key.endswith("bull") else -1
            else:
                price = max(p["price"] for p in points) if side > 0 else min(p["price"] for p in points)
            levels.update(invalid_price=price, invalid_side=side)
            if key.startswith("elliott_cycle") and e["phase"] == "FORMING":
                # This catalog models a cycle whose correction retains its 0
                # origin. It is not a rule excluding all other Elliott counts.
                levels["hard_limits"] = [{"price": points[0]["price"],
                    "side": -1 if key.endswith("bull") else 1,
                    "inclusive": True, "rule": "CYCLE_RETAINS_ORIGIN"}]
        if key.endswith("breakout_retest"):
            sign = 1 if key.startswith("bull") else -1
            levels.update(invalid_price=e["retest_level"]-sign*.5*e["retest_atr"], invalid_side=-sign, expiry_bars=24)
    e["levels"] = levels
    for limit in levels.get("hard_limits", []):
        # Render the same frozen boundary consumed by lifecycle, not a new pivot.
        if math.isfinite(limit["price"]) and limit["price"] > 0:
            e["lines"].append(horizontal(e, limit["price"], "invalidation", now))
    if levels.get("hard_limits"):
        e["recognition"]["checks"].append("固定数浪路径的附加失效边界；保留其他独立候选")
    if key in {"double_top","double_bottom"}:
        neck=next(p for p in points if str(p.get("label","")).startswith("N"))
        levels["formation_atr"]=formation_atr(candles,neck["time"],atr_by_time)
        if e["phase"]=="FORMING":
            # Use the same admissible peak tolerance as the completed detector.
            # Invalidating at the FIRST peak alone rejected valid unequal double tops.
            levels["invalid_price"]=points[0]["price"]+levels["invalid_side"]*.75*levels["formation_atr"]
    # Geometry admission and aesthetic fit are separate. A valid but asymmetric
    # structure remains a candidate; a score is neither success nor probability.
    e["quality"] = "UNSCORED" if e["confidence"] is None else "STANDARD" if e["confidence"] >= MIN_SCORE else "LOW_FIT"
    if any(not math.isfinite(p["price"]) or p["price"] <= 0 for line in e["lines"] for p in (line["from"], line["to"])):
        return reject("INVALID_GUIDE")
    e["id"] = identity(e)
    e["score_bucket"] = bucket(e)
    e["end_time"] = now
    e["version"] = VERSION
    return e


def formation_atr(candles: list[dict], ts: int, values: dict | None = None) -> float:
    if values is not None:
        return values[ts]
    from pattern_engine import calculate_atr, normalize_frame
    df = normalize_frame(candles)
    return float(calculate_atr(df)[next(i for i,t in enumerate(df.time) if t == ts)])


def _canonical_boundary_windows(events: list[dict]) -> list[dict]:
    """Choose one display window for the exact same fitted boundary anchors.

    The scanner's 100/140-bar windows can contain the same last six highs and
    lows. Their regression is mathematically identical, but evaluating its
    residuals through different line endpoints introduces floating-point noise.
    Do not let that noise choose a different start candle after a price-unit or
    BLAS change. Retain the widest already-admitted window for those exact
    anchors; genuinely different anchors still compete under the usual rules.
    No prices, confidence values, or geometry admission thresholds are rounded.
    """
    representatives: dict[tuple, dict] = {}
    for index, event in enumerate(events):
        if event.get("region"):
            anchors = tuple(sorted((p["time"], p["price"], p.get("basis", ""), p.get("label", ""))
                                   for p in known_points(event)))
            key = ("boundary", event["id"], event["phase"], event["end_time"],
                   event["signal_direction"], anchors)
        else:
            key = ("other", index)
        previous = representatives.get(key)
        if previous is None or event["start_time"] < previous["start_time"]:
            representatives[key] = event
    return list(representatives.values())


def proposals(candles: list[dict], *, diagnostics: dict | None = None) -> list[dict]:
    from pattern_engine import scan_patterns, calculate_atr, normalize_frame
    if len(candles) < 20:
        return []
    candles = candles[-WINDOW:]
    if error := closed_input_error(candles):
        if diagnostics is not None:
            diagnostics["input_error"] = error
        raise ValueError(f"STRUCTURE_{error}")
    raw = scan_patterns(candles, min_confidence=0, max_bars=WINDOW, max_events_per_pattern=160,
        pivot_order=PIVOT_ORDER, latest_only=True, precision_mode=True, preserve_candidates=True, include_statistics=False)["events"]
    raw.extend(forming_waves(candles, precision_mode=True, preserve_candidates=True))
    from gatescope.quant.structure_rules import retest_proposals
    raw.extend(retest_proposals(candles))
    df = normalize_frame(candles)
    atr_values = {int(t):float(v) for t,v in zip(df.time,calculate_atr(df), strict=True)}
    events = [e for r in raw if (e := refine(r, candles, diagnostics=diagnostics, atr_by_time=atr_values,
                                        _input_checked=True)) is not None]
    # Prefer the most advanced known geometry for a stable candidate identity.
    candidates = _canonical_boundary_windows(events)
    candidates.sort(key=lambda e: (e["phase"] == "FORMED", len(known_points(e)), e.get("confidence") or 0))
    unique = list({e["id"]: e for e in candidates}.values())
    if diagnostics is not None:
        diagnostics.update(raw_count=len(raw), geometry_valid=len(events), identity_duplicates=len(events)-len(unique), candidate_count=len(unique))
    return unique
