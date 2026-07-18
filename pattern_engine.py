#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heuristic chart-pattern detection engine.

The engine intentionally detects chart formations only. It does not include
single/multi-candlestick patterns. Results are probabilistic heuristics and are
returned with confidence and confirmation flags.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np
import pandas as pd


PATTERN_CATALOG: list[dict[str, Any]] = [
    {"id": "double_top", "name": "双顶", "group": "反转形态", "direction": "bearish"},
    {"id": "double_bottom", "name": "双底", "group": "反转形态", "direction": "bullish"},
    {"id": "triple_top", "name": "三顶", "group": "反转形态", "direction": "bearish"},
    {"id": "triple_bottom", "name": "三底", "group": "反转形态", "direction": "bullish"},
    {"id": "head_shoulders", "name": "头肩顶", "group": "反转形态", "direction": "bearish"},
    {"id": "inverse_head_shoulders", "name": "倒头肩", "group": "反转形态", "direction": "bullish"},
    {"id": "cup_handle", "name": "杯柄", "group": "反转形态", "direction": "bullish"},
    {"id": "inverse_cup_handle", "name": "反转杯柄", "group": "反转形态", "direction": "bearish"},
    {"id": "bull_flag", "name": "看涨旗形", "group": "持续形态", "direction": "bullish"},
    {"id": "bear_flag", "name": "看跌旗形", "group": "持续形态", "direction": "bearish"},
    {"id": "bull_pennant", "name": "看涨三角旗", "group": "持续形态", "direction": "bullish"},
    {"id": "bear_pennant", "name": "看跌三角旗", "group": "持续形态", "direction": "bearish"},
    {"id": "ascending_triangle", "name": "上升三角形", "group": "整理形态", "direction": "bullish"},
    {"id": "descending_triangle", "name": "下降三角形", "group": "整理形态", "direction": "bearish"},
    {"id": "symmetrical_triangle", "name": "对称三角形", "group": "整理形态", "direction": "neutral"},
    {"id": "rectangle", "name": "矩形整理", "group": "整理形态", "direction": "neutral"},
    {"id": "rising_wedge", "name": "上升楔形", "group": "趋势形态", "direction": "bearish"},
    {"id": "falling_wedge", "name": "下降楔形", "group": "趋势形态", "direction": "bullish"},
    {"id": "uptrend_channel", "name": "上升通道", "group": "趋势形态", "direction": "bullish"},
    {"id": "downtrend_channel", "name": "下降通道", "group": "趋势形态", "direction": "bearish"},
    {"id": "elliott_impulse_bull", "name": "艾略特上升五浪", "group": "波浪理论", "direction": "bullish", "experimental": True},
    {"id": "elliott_impulse_bear", "name": "艾略特下降五浪", "group": "波浪理论", "direction": "bearish", "experimental": True},
    {"id": "elliott_correction_bull", "name": "ABC下跌修正完成", "group": "波浪理论", "direction": "bullish", "experimental": True},
    {"id": "elliott_correction_bear", "name": "ABC上涨修正完成", "group": "波浪理论", "direction": "bearish", "experimental": True},
    {"id": "elliott_cycle_bull", "name": "上升五浪+ABC完整周期", "group": "波浪理论", "direction": "bullish", "experimental": True},
    {"id": "elliott_cycle_bear", "name": "下降五浪+ABC完整周期", "group": "波浪理论", "direction": "bearish", "experimental": True},
]

CATALOG_BY_ID = {item["id"]: item for item in PATTERN_CATALOG}


@dataclass(frozen=True)
class Pivot:
    index: int
    kind: str
    price: float


@dataclass
class ScanContext:
    df: pd.DataFrame
    highs: list[Pivot]
    lows: list[Pivot]
    atr: np.ndarray
    median_price: float


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if abs(b) > 1e-12 else default


def linear_fit(points: Sequence[Pivot]) -> tuple[float, float, float]:
    if len(points) < 2:
        return 0.0, points[0].price if points else 0.0, 0.0
    x = np.array([p.index for p in points], dtype=float)
    y = np.array([p.price for p in points], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - safe_div(ss_res, ss_tot, 0.0) if ss_tot > 0 else 1.0
    return float(slope), float(intercept), clamp(r2)


def point(ts: int, price: float, label: str = "") -> dict[str, Any]:
    return {"time": int(ts), "price": float(price), "label": label}


def line(p1: dict[str, Any], p2: dict[str, Any], style: str = "solid") -> dict[str, Any]:
    return {"from": p1, "to": p2, "style": style}


def make_event(
    ctx: ScanContext,
    pattern_id: str,
    start_index: int,
    end_index: int,
    confidence: float,
    confirmed: bool,
    points: Sequence[dict[str, Any]],
    lines: Optional[Sequence[dict[str, Any]]] = None,
    region: Optional[dict[str, Any]] = None,
    note: str = "",
    signal_direction: Optional[str] = None,
) -> dict[str, Any]:
    meta = CATALOG_BY_ID[pattern_id]
    start_index = max(0, min(start_index, len(ctx.df) - 1))
    end_index = max(start_index, min(end_index, len(ctx.df) - 1))
    start_ts = int(ctx.df.iloc[start_index]["time"])
    end_ts = int(ctx.df.iloc[end_index]["time"])
    raw_id = f"{pattern_id}:{start_ts}:{end_ts}:" + ":".join(
        f"{int(p['time'])}-{round(float(p['price']), 8)}" for p in points
    )
    event_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]
    return {
        "id": event_id,
        "pattern": pattern_id,
        "name": meta["name"],
        "group": meta["group"],
        "direction": meta["direction"],
        "signal_direction": signal_direction or meta["direction"],
        "experimental": bool(meta.get("experimental")),
        "confidence": round(clamp(confidence), 4),
        "confirmed": bool(confirmed),
        "start_time": start_ts,
        "end_time": end_ts,
        "points": list(points),
        "lines": list(lines or []),
        "region": region,
        "note": note,
    }


def normalize_frame(candles: Sequence[dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(candles).rename(columns={"ts": "time"})
    needed = ["time", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df:
            df[col] = 0.0
    df = df[needed].copy()
    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().drop_duplicates("time", keep="last").sort_values("time")
    df = df[(df["high"] >= df["low"]) & (df["close"] > 0)]
    return df.reset_index(drop=True)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    if df.empty:
        return np.array([], dtype=float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    previous = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    return pd.Series(tr).rolling(period, min_periods=1).mean().to_numpy(float)


def find_pivots(df: pd.DataFrame, order: Optional[int] = None) -> tuple[list[Pivot], list[Pivot]]:
    n = len(df)
    if n < 7:
        return [], []
    if order is None:
        order = max(2, min(8, int(round(n / 180))))
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    high_pivots: list[Pivot] = []
    low_pivots: list[Pivot] = []
    for i in range(order, n - order):
        hw = highs[i - order : i + order + 1]
        lw = lows[i - order : i + order + 1]
        if highs[i] >= float(np.max(hw)) and int(np.argmax(hw)) == order:
            high_pivots.append(Pivot(i, "high", float(highs[i])))
        if lows[i] <= float(np.min(lw)) and int(np.argmin(lw)) == order:
            low_pivots.append(Pivot(i, "low", float(lows[i])))
    return high_pivots, low_pivots


def extrema_between(pivots: Sequence[Pivot], left: int, right: int) -> list[Pivot]:
    return [p for p in pivots if left < p.index < right]


def closes_after(ctx: ScanContext, index: int, bars: int = 25) -> np.ndarray:
    end = min(len(ctx.df), index + bars + 1)
    return ctx.df["close"].to_numpy(float)[index + 1 : end]


def detect_double_patterns(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    is_top = pattern_id == "double_top"
    primary = ctx.highs if is_top else ctx.lows
    opposite = ctx.lows if is_top else ctx.highs
    events: list[dict[str, Any]] = []
    for first, second in zip(primary, primary[1:]):
        gap = second.index - first.index
        if gap < 6 or gap > 120:
            continue
        middle_candidates = extrema_between(opposite, first.index, second.index)
        if not middle_candidates:
            continue
        middle = min(middle_candidates, key=lambda p: p.price) if is_top else max(middle_candidates, key=lambda p: p.price)
        mean_level = (first.price + second.price) / 2
        similarity = abs(first.price - second.price) / max(mean_level, 1e-12)
        depth = (mean_level - middle.price) / mean_level if is_top else (middle.price - mean_level) / mean_level
        atr_ratio = abs(mean_level - middle.price) / max(ctx.atr[middle.index], mean_level * 0.002)
        if similarity > 0.045 or depth < 0.012 or atr_ratio < 1.2:
            continue
        after = closes_after(ctx, second.index, 30)
        if is_top:
            hits = np.where(after < middle.price)[0]
        else:
            hits = np.where(after > middle.price)[0]
        confirmed = len(hits) > 0
        end_index = second.index + int(hits[0]) + 1 if confirmed else second.index
        similarity_score = 1 - similarity / 0.045
        depth_score = clamp((depth - 0.012) / 0.10)
        confidence = 0.45 * similarity_score + 0.35 * depth_score + 0.20 * float(confirmed)
        pts = [
            point(int(ctx.df.iloc[first.index].time), first.price, "1"),
            point(int(ctx.df.iloc[middle.index].time), middle.price, "N"),
            point(int(ctx.df.iloc[second.index].time), second.price, "2"),
        ]
        events.append(
            make_event(
                ctx,
                pattern_id,
                first.index,
                end_index,
                confidence,
                confirmed,
                pts,
                [line(pts[0], pts[1]), line(pts[1], pts[2]), line(pts[1], point(int(ctx.df.iloc[end_index].time), middle.price), "dashed")],
                note="已跌破/突破颈线" if confirmed else "等待颈线确认",
            )
        )
    return events


def detect_triple_patterns(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    is_top = pattern_id == "triple_top"
    primary = ctx.highs if is_top else ctx.lows
    opposite = ctx.lows if is_top else ctx.highs
    events: list[dict[str, Any]] = []
    for i in range(len(primary) - 2):
        p1, p2, p3 = primary[i : i + 3]
        if p3.index - p1.index < 14 or p3.index - p1.index > 180:
            continue
        levels = np.array([p1.price, p2.price, p3.price])
        mean_level = float(levels.mean())
        dispersion = float((levels.max() - levels.min()) / max(mean_level, 1e-12))
        if dispersion > 0.055:
            continue
        m1s = extrema_between(opposite, p1.index, p2.index)
        m2s = extrema_between(opposite, p2.index, p3.index)
        if not m1s or not m2s:
            continue
        m1 = min(m1s, key=lambda p: p.price) if is_top else max(m1s, key=lambda p: p.price)
        m2 = min(m2s, key=lambda p: p.price) if is_top else max(m2s, key=lambda p: p.price)
        neckline = min(m1.price, m2.price) if is_top else max(m1.price, m2.price)
        depth = (mean_level - neckline) / mean_level if is_top else (neckline - mean_level) / mean_level
        if depth < 0.014:
            continue
        after = closes_after(ctx, p3.index, 35)
        hits = np.where(after < neckline)[0] if is_top else np.where(after > neckline)[0]
        confirmed = len(hits) > 0
        end_index = p3.index + int(hits[0]) + 1 if confirmed else p3.index
        confidence = 0.4 * (1 - dispersion / 0.055) + 0.4 * clamp(depth / 0.10) + 0.2 * float(confirmed)
        pivots = [p1, m1, p2, m2, p3]
        pts = [point(int(ctx.df.iloc[p.index].time), p.price, str(j + 1) if p.kind == primary[0].kind else "N") for j, p in enumerate(pivots)]
        lines = [line(pts[j], pts[j + 1]) for j in range(len(pts) - 1)]
        lines.append(line(point(int(ctx.df.iloc[m1.index].time), neckline), point(int(ctx.df.iloc[end_index].time), neckline), "dashed"))
        events.append(make_event(ctx, pattern_id, p1.index, end_index, confidence, confirmed, pts, lines, note="三次测试关键价位"))
    return events


def detect_head_shoulders(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    inverse = pattern_id == "inverse_head_shoulders"
    shoulders = ctx.lows if inverse else ctx.highs
    neck_pivots = ctx.highs if inverse else ctx.lows
    events: list[dict[str, Any]] = []
    for i in range(len(shoulders) - 2):
        left, head, right = shoulders[i : i + 3]
        if right.index - left.index < 16 or right.index - left.index > 220:
            continue
        if inverse:
            head_dominance = min(left.price, right.price) - head.price
            shoulder_mean = (left.price + right.price) / 2
        else:
            head_dominance = head.price - max(left.price, right.price)
            shoulder_mean = (left.price + right.price) / 2
        dominance_ratio = head_dominance / max(shoulder_mean, 1e-12)
        shoulder_diff = abs(left.price - right.price) / max(shoulder_mean, 1e-12)
        if dominance_ratio < 0.012 or shoulder_diff > 0.065:
            continue
        between1 = extrema_between(neck_pivots, left.index, head.index)
        between2 = extrema_between(neck_pivots, head.index, right.index)
        if not between1 or not between2:
            continue
        n1 = max(between1, key=lambda p: p.price) if inverse else min(between1, key=lambda p: p.price)
        n2 = max(between2, key=lambda p: p.price) if inverse else min(between2, key=lambda p: p.price)
        neck_slope = safe_div(n2.price - n1.price, n2.index - n1.index)
        after = closes_after(ctx, right.index, 40)
        hits: list[int] = []
        for j, value in enumerate(after, start=1):
            neck_value = n2.price + neck_slope * (right.index + j - n2.index)
            if (inverse and value > neck_value) or ((not inverse) and value < neck_value):
                hits.append(j)
                break
        confirmed = bool(hits)
        end_index = right.index + hits[0] if confirmed else right.index
        confidence = (
            0.35 * clamp(dominance_ratio / 0.08)
            + 0.30 * (1 - shoulder_diff / 0.065)
            + 0.15 * clamp(1 - abs(n1.price - n2.price) / max(shoulder_mean * 0.08, 1e-12))
            + 0.20 * float(confirmed)
        )
        pivots = [left, n1, head, n2, right]
        labels = ["LS", "N1", "H", "N2", "RS"]
        pts = [point(int(ctx.df.iloc[p.index].time), p.price, labels[j]) for j, p in enumerate(pivots)]
        lines = [line(pts[j], pts[j + 1]) for j in range(len(pts) - 1)]
        neck_end_price = n2.price + neck_slope * (end_index - n2.index)
        lines.append(line(pts[1], point(int(ctx.df.iloc[end_index].time), neck_end_price, "neck"), "dashed"))
        events.append(make_event(ctx, pattern_id, left.index, end_index, confidence, confirmed, pts, lines, note="颈线突破确认" if confirmed else "等待颈线突破"))
    return events


def window_pivots(pivots: Sequence[Pivot], start: int, end: int, max_points: int = 6) -> list[Pivot]:
    selected = [p for p in pivots if start <= p.index <= end]
    return selected[-max_points:]


def boundary_event(
    ctx: ScanContext,
    pattern_id: str,
    start: int,
    end: int,
    highs: Sequence[Pivot],
    lows: Sequence[Pivot],
    confidence: float,
    confirmed: bool,
    note: str,
    signal_direction: Optional[str] = None,
) -> dict[str, Any]:
    hs, hi, _ = linear_fit(highs)
    ls, li, _ = linear_fit(lows)
    h1 = point(int(ctx.df.iloc[start].time), hs * start + hi, "U")
    h2 = point(int(ctx.df.iloc[end].time), hs * end + hi, "U")
    l1 = point(int(ctx.df.iloc[start].time), ls * start + li, "L")
    l2 = point(int(ctx.df.iloc[end].time), ls * end + li, "L")
    pts = [
        point(int(ctx.df.iloc[p.index].time), p.price, "H") for p in highs
    ] + [point(int(ctx.df.iloc[p.index].time), p.price, "L") for p in lows]
    region = {
        "start_time": int(ctx.df.iloc[start].time),
        "end_time": int(ctx.df.iloc[end].time),
        "top_start": h1["price"],
        "top_end": h2["price"],
        "bottom_start": l1["price"],
        "bottom_end": l2["price"],
    }
    return make_event(
        ctx, pattern_id, start, end, confidence, confirmed, pts,
        [line(h1, h2), line(l1, l2)], region, note,
        signal_direction=signal_direction,
    )


def detect_boundaries(ctx: ScanContext, selected: set[str]) -> list[dict[str, Any]]:
    wanted = selected.intersection(
        {
            "ascending_triangle", "descending_triangle", "symmetrical_triangle", "rectangle",
            "rising_wedge", "falling_wedge", "uptrend_channel", "downtrend_channel",
        }
    )
    if not wanted:
        return []
    n = len(ctx.df)
    events: list[dict[str, Any]] = []
    candidate_ends = sorted(set(list(range(60, n, max(6, n // 150 or 6))) + [n - 1]))
    for end in candidate_ends:
        for length in (45, 70, 100, 140):
            start = end - length + 1
            if start < 0:
                continue
            highs = window_pivots(ctx.highs, start, end, 6)
            lows = window_pivots(ctx.lows, start, end, 6)
            if len(highs) < 3 or len(lows) < 3:
                continue
            hs, hi, hr2 = linear_fit(highs)
            ls, li, lr2 = linear_fit(lows)
            base = max(float(ctx.df["close"].iloc[start:end + 1].median()), 1e-12)
            hs_n = hs / base
            ls_n = ls / base
            upper_start = hs * start + hi
            upper_end = hs * end + hi
            lower_start = ls * start + li
            lower_end = ls * end + li
            width_start = upper_start - lower_start
            width_end = upper_end - lower_end
            if width_start <= 0 or width_end <= 0:
                continue
            convergence = 1 - width_end / width_start
            fit = (hr2 + lr2) / 2
            current_close = float(ctx.df.iloc[end].close)
            confirmed_up = current_close > upper_end
            confirmed_down = current_close < lower_end
            span = end - start
            flat_threshold = 0.00012
            slope_threshold = 0.00010

            pattern_id = ""
            confirmed = False
            score = 0.0
            note = ""
            signal_direction: Optional[str] = None
            if abs(hs_n) <= flat_threshold and ls_n > slope_threshold and convergence > 0.20:
                pattern_id = "ascending_triangle"
                confirmed = confirmed_up
                score = 0.38 * fit + 0.32 * clamp(convergence / 0.7) + 0.18 * clamp(ls_n * span / 0.08) + 0.12 * float(confirmed)
                note = "水平阻力与抬高低点"
            elif hs_n < -slope_threshold and abs(ls_n) <= flat_threshold and convergence > 0.20:
                pattern_id = "descending_triangle"
                confirmed = confirmed_down
                score = 0.38 * fit + 0.32 * clamp(convergence / 0.7) + 0.18 * clamp(abs(hs_n) * span / 0.08) + 0.12 * float(confirmed)
                note = "下降高点与水平支撑"
            elif hs_n < -slope_threshold and ls_n > slope_threshold and convergence > 0.22:
                pattern_id = "symmetrical_triangle"
                confirmed = confirmed_up or confirmed_down
                score = 0.40 * fit + 0.38 * clamp(convergence / 0.75) + 0.10 * clamp((abs(hs_n) + abs(ls_n)) * span / 0.12) + 0.12 * float(confirmed)
                note = "上下边界收敛"
                signal_direction = "bullish" if confirmed_up else "bearish" if confirmed_down else "neutral"
            elif abs(hs_n) <= flat_threshold and abs(ls_n) <= flat_threshold and abs(width_end / width_start - 1) < 0.25:
                pattern_id = "rectangle"
                confirmed = confirmed_up or confirmed_down
                touch_score = clamp((len(highs) + len(lows) - 6) / 6)
                score = 0.48 * fit + 0.25 * touch_score + 0.15 * clamp(width_start / base / 0.12) + 0.12 * float(confirmed)
                note = "近似水平箱体"
                signal_direction = "bullish" if confirmed_up else "bearish" if confirmed_down else "neutral"
            elif hs_n > slope_threshold and ls_n > slope_threshold and ls_n > hs_n * 1.12 and convergence > 0.18:
                pattern_id = "rising_wedge"
                confirmed = confirmed_down
                score = 0.42 * fit + 0.34 * clamp(convergence / 0.65) + 0.12 * clamp((ls_n - hs_n) * span / 0.05) + 0.12 * float(confirmed)
                note = "两条上升边界逐渐收敛"
            elif hs_n < -slope_threshold and ls_n < -slope_threshold and hs_n < ls_n * 1.12 and convergence > 0.18:
                pattern_id = "falling_wedge"
                confirmed = confirmed_up
                score = 0.42 * fit + 0.34 * clamp(convergence / 0.65) + 0.12 * clamp(abs(hs_n - ls_n) * span / 0.05) + 0.12 * float(confirmed)
                note = "两条下降边界逐渐收敛"
            elif hs_n > slope_threshold and ls_n > slope_threshold and abs(hs_n - ls_n) <= max(abs(hs_n), abs(ls_n)) * 0.35 and abs(width_end / width_start - 1) < 0.35:
                pattern_id = "uptrend_channel"
                confirmed = False
                score = 0.55 * fit + 0.25 * clamp((hs_n + ls_n) * span / 0.12) + 0.20 * clamp(1 - abs(width_end / width_start - 1))
                note = "平行上升通道"
            elif hs_n < -slope_threshold and ls_n < -slope_threshold and abs(hs_n - ls_n) <= max(abs(hs_n), abs(ls_n)) * 0.35 and abs(width_end / width_start - 1) < 0.35:
                pattern_id = "downtrend_channel"
                confirmed = False
                score = 0.55 * fit + 0.25 * clamp((abs(hs_n) + abs(ls_n)) * span / 0.12) + 0.20 * clamp(1 - abs(width_end / width_start - 1))
                note = "平行下降通道"

            if pattern_id and pattern_id in wanted and score >= 0.48:
                events.append(boundary_event(
                    ctx, pattern_id, start, end, highs, lows, score, confirmed, note,
                    signal_direction=signal_direction,
                ))
                break
    return events


def detect_flags(ctx: ScanContext, selected: set[str]) -> list[dict[str, Any]]:
    wanted = selected.intersection({"bull_flag", "bear_flag", "bull_pennant", "bear_pennant"})
    if not wanted:
        return []
    close = ctx.df["close"].to_numpy(float)
    high = ctx.df["high"].to_numpy(float)
    low = ctx.df["low"].to_numpy(float)
    n = len(ctx.df)
    events: list[dict[str, Any]] = []
    for end in sorted(set(list(range(35, n, 5)) + [n - 1])):
        for cons_len in (10, 14, 20, 26):
            cons_start = end - cons_len + 1
            if cons_start < 15:
                continue
            pole_start = max(0, cons_start - min(24, max(10, cons_len)))
            pole_move = close[cons_start - 1] - close[pole_start]
            pole_pct = pole_move / max(close[pole_start], 1e-12)
            pole_atr = abs(pole_move) / max(float(np.mean(ctx.atr[pole_start:cons_start])), close[pole_start] * 0.002)
            if abs(pole_pct) < 0.045 or pole_atr < 3.0:
                continue
            highs = [p for p in ctx.highs if cons_start <= p.index <= end]
            lows = [p for p in ctx.lows if cons_start <= p.index <= end]
            if len(highs) < 2 or len(lows) < 2:
                continue
            hs, hi, hr2 = linear_fit(highs[-4:])
            ls, li, lr2 = linear_fit(lows[-4:])
            cons_range = float(np.max(high[cons_start:end + 1]) - np.min(low[cons_start:end + 1]))
            if cons_range > abs(pole_move) * 0.65:
                continue
            upper_start, upper_end = hs * cons_start + hi, hs * end + hi
            lower_start, lower_end = ls * cons_start + li, ls * end + li
            width_start = upper_start - lower_start
            width_end = upper_end - lower_end
            fit = (hr2 + lr2) / 2
            is_bull = pole_move > 0
            parallel = abs(hs - ls) <= max(abs(hs), abs(ls), ctx.median_price * 1e-5) * 0.55
            converging = width_start > 0 and width_end > 0 and width_end < width_start * 0.72
            pattern_id = ""
            if is_bull and parallel and hs <= ctx.median_price * 0.0002 and ls <= ctx.median_price * 0.0002:
                pattern_id = "bull_flag"
            elif (not is_bull) and parallel and hs >= -ctx.median_price * 0.0002 and ls >= -ctx.median_price * 0.0002:
                pattern_id = "bear_flag"
            elif is_bull and converging and hs < 0 < ls:
                pattern_id = "bull_pennant"
            elif (not is_bull) and converging and hs < 0 < ls:
                pattern_id = "bear_pennant"
            if pattern_id not in wanted:
                continue
            breakout_up = close[end] > upper_end
            breakout_down = close[end] < lower_end
            confirmed = breakout_up if is_bull else breakout_down
            compression = clamp(1 - cons_range / max(abs(pole_move), 1e-12))
            score = 0.35 * clamp(abs(pole_pct) / 0.18) + 0.25 * clamp(pole_atr / 8) + 0.25 * fit + 0.10 * compression + 0.05 * float(confirmed)
            pole_p1 = point(int(ctx.df.iloc[pole_start].time), close[pole_start], "pole")
            pole_p2 = point(int(ctx.df.iloc[cons_start - 1].time), close[cons_start - 1], "pole")
            h1 = point(int(ctx.df.iloc[cons_start].time), upper_start, "U")
            h2 = point(int(ctx.df.iloc[end].time), upper_end, "U")
            l1 = point(int(ctx.df.iloc[cons_start].time), lower_start, "L")
            l2 = point(int(ctx.df.iloc[end].time), lower_end, "L")
            pts = [pole_p1, pole_p2] + [point(int(ctx.df.iloc[p.index].time), p.price) for p in highs[-4:] + lows[-4:]]
            lines = [line(pole_p1, pole_p2), line(h1, h2), line(l1, l2)]
            events.append(make_event(ctx, pattern_id, pole_start, end, score, confirmed, pts, lines, note="旗杆后缩量整理"))
            break
    return events


def detect_cup_handle(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    inverse = pattern_id == "inverse_cup_handle"
    close = ctx.df["close"].to_numpy(float)
    n = len(close)
    events: list[dict[str, Any]] = []
    for end in sorted(set(list(range(70, n, 10)) + [n - 1])):
        for length in (60, 90, 130, 180, 240):
            start = end - length + 1
            if start < 0:
                continue
            window = close[start : end + 1]
            cup_end_local = int(length * 0.78)
            cup = window[:cup_end_local]
            handle = window[cup_end_local - 1 :]
            rim_band = max(3, int(length * 0.08))
            left_level = float(np.mean(cup[:rim_band]))
            right_slice_start = max(rim_band, cup_end_local - rim_band * 2)
            right_segment = cup[right_slice_start:cup_end_local]
            right_local = right_slice_start + (int(np.argmin(right_segment)) if inverse else int(np.argmax(right_segment)))
            right_level = float(cup[right_local])
            extreme_local = int(np.argmax(cup)) if inverse else int(np.argmin(cup))
            extreme = float(cup[extreme_local])
            rim = (left_level + right_level) / 2
            similarity = abs(left_level - right_level) / max(abs(rim), 1e-12)
            depth = (extreme - rim) / rim if inverse else (rim - extreme) / rim
            center = extreme_local / max(cup_end_local - 1, 1)
            if similarity > 0.07 or depth < 0.045 or not (0.22 <= center <= 0.72):
                continue
            if inverse:
                handle_extreme_local = int(np.argmax(handle))
                handle_pullback = float(handle[handle_extreme_local] - right_level)
                confirmed = window[-1] < min(left_level, right_level)
            else:
                handle_extreme_local = int(np.argmin(handle))
                handle_pullback = float(right_level - handle[handle_extreme_local])
                confirmed = window[-1] > max(left_level, right_level)
            if handle_pullback < 0 or handle_pullback > abs(depth * rim) * 0.62:
                continue
            shape_score = 1 - similarity / 0.07
            center_score = 1 - abs(center - 0.47) / 0.30
            handle_score = 1 - handle_pullback / max(abs(depth * rim) * 0.62, 1e-12)
            confidence = 0.32 * shape_score + 0.28 * clamp(depth / 0.18) + 0.20 * clamp(center_score) + 0.12 * clamp(handle_score) + 0.08 * float(confirmed)
            left_idx = start + rim_band // 2
            extreme_idx = start + extreme_local
            right_idx = start + right_local
            handle_idx = start + cup_end_local - 1 + handle_extreme_local
            pts = [
                point(int(ctx.df.iloc[left_idx].time), left_level, "L"),
                point(int(ctx.df.iloc[extreme_idx].time), extreme, "B" if not inverse else "T"),
                point(int(ctx.df.iloc[right_idx].time), right_level, "R"),
                point(int(ctx.df.iloc[handle_idx].time), float(close[handle_idx]), "H"),
                point(int(ctx.df.iloc[end].time), float(close[end]), "E"),
            ]
            lines = [line(pts[j], pts[j + 1]) for j in range(len(pts) - 1)]
            events.append(make_event(ctx, pattern_id, start, end, confidence, confirmed, pts, lines, note="U形主体与较浅手柄"))
            break
    return events


def alternating_pivots(ctx: ScanContext) -> list[Pivot]:
    all_pivots = sorted(ctx.highs + ctx.lows, key=lambda p: p.index)
    compressed: list[Pivot] = []
    for p in all_pivots:
        if not compressed or compressed[-1].kind != p.kind:
            compressed.append(p)
            continue
        last = compressed[-1]
        if (p.kind == "high" and p.price > last.price) or (p.kind == "low" and p.price < last.price):
            compressed[-1] = p
    return compressed


def detect_elliott(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    bullish = pattern_id == "elliott_impulse_bull"
    pivots = alternating_pivots(ctx)
    events: list[dict[str, Any]] = []
    expected = ["low", "high", "low", "high", "low", "high"] if bullish else ["high", "low", "high", "low", "high", "low"]
    for i in range(len(pivots) - 5):
        seq = pivots[i : i + 6]
        if [p.kind for p in seq] != expected:
            continue
        if seq[-1].index - seq[0].index < 18 or seq[-1].index - seq[0].index > 260:
            continue
        p = [x.price for x in seq]
        if bullish:
            w1, w2, w3, w4, w5 = p[1] - p[0], p[1] - p[2], p[3] - p[2], p[3] - p[4], p[5] - p[4]
            rules = [w1 > 0, p[2] > p[0], p[3] > p[1], p[4] > p[1], p[5] > p[3], w3 >= min(w1, w5) * 0.75]
        else:
            w1, w2, w3, w4, w5 = p[0] - p[1], p[2] - p[1], p[2] - p[3], p[4] - p[3], p[4] - p[5]
            rules = [w1 > 0, p[2] < p[0], p[3] < p[1], p[4] < p[1], p[5] < p[3], w3 >= min(w1, w5) * 0.75]
        if not all(rules):
            continue
        retrace2 = safe_div(w2, w1)
        retrace4 = safe_div(w4, w3)
        if not (0.15 <= retrace2 <= 0.88 and 0.12 <= retrace4 <= 0.75):
            continue
        duration_balance = min(
            seq[3].index - seq[2].index,
            seq[1].index - seq[0].index,
            seq[5].index - seq[4].index,
        ) / max(
            seq[3].index - seq[2].index,
            seq[1].index - seq[0].index,
            seq[5].index - seq[4].index,
            1,
        )
        confidence = 0.34 * clamp(w3 / max(w1, w5, 1e-12) / 1.8) + 0.26 * clamp(1 - abs(retrace2 - 0.5)) + 0.20 * clamp(1 - abs(retrace4 - 0.38)) + 0.20 * duration_balance
        pts = [point(int(ctx.df.iloc[pv.index].time), pv.price, str(j)) for j, pv in enumerate(seq)]
        lines = [line(pts[j], pts[j + 1]) for j in range(5)]
        events.append(make_event(ctx, pattern_id, seq[0].index, seq[-1].index, confidence, True, pts, lines, note="启发式五浪识别，建议人工复核"))
    return events




def _future_confirmation_index(
    ctx: ScanContext,
    pivot_index: int,
    trigger_price: float,
    bullish: bool,
    bars: int = 18,
) -> Optional[int]:
    end = min(len(ctx.df), pivot_index + bars + 1)
    closes = ctx.df["close"].to_numpy(float)
    for idx in range(pivot_index + 1, end):
        if (bullish and closes[idx] > trigger_price) or ((not bullish) and closes[idx] < trigger_price):
            return idx
    return None


def detect_elliott_correction(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    """识别简化的 A-B-C 锯齿修正，并在 C 浪后给出反向信号。"""
    bullish = pattern_id == "elliott_correction_bull"
    pivots = alternating_pivots(ctx)
    expected = ["high", "low", "high", "low"] if bullish else ["low", "high", "low", "high"]
    events: list[dict[str, Any]] = []
    for i in range(len(pivots) - 3):
        seq = pivots[i : i + 4]
        if [p.kind for p in seq] != expected:
            continue
        span = seq[-1].index - seq[0].index
        if span < 10 or span > 180:
            continue
        p0, pa, pb, pc = [p.price for p in seq]
        if bullish:
            wave_a = p0 - pa
            wave_b = pb - pa
            wave_c = pb - pc
            structural = p0 > pb > pa and pc < pb and pc <= pa * 1.015
        else:
            wave_a = pa - p0
            wave_b = pa - pb
            wave_c = pc - pb
            structural = p0 < pb < pa and pc > pb and pc >= pa * 0.985
        if not structural or wave_a <= 0 or wave_b <= 0 or wave_c <= 0:
            continue
        a_pct = wave_a / max(abs(p0), 1e-12)
        retrace_b = safe_div(wave_b, wave_a)
        extension_c = safe_div(wave_c, wave_a)
        if a_pct < 0.025 or not (0.22 <= retrace_b <= 0.88) or not (0.55 <= extension_c <= 2.0):
            continue
        duration_a = seq[1].index - seq[0].index
        duration_b = seq[2].index - seq[1].index
        duration_c = seq[3].index - seq[2].index
        duration_balance = min(duration_a, duration_b, duration_c) / max(duration_a, duration_b, duration_c, 1)
        trigger = pc + 0.236 * (pb - pc)
        confirm_idx = _future_confirmation_index(ctx, seq[3].index, trigger, bullish=bullish)
        confirmed = confirm_idx is not None
        end_idx = confirm_idx if confirm_idx is not None else seq[3].index
        ratio_score = 0.5 * clamp(1 - abs(retrace_b - 0.5) / 0.5) + 0.5 * clamp(1 - abs(extension_c - 1.0) / 1.0)
        confidence = 0.34 * clamp(a_pct / 0.12) + 0.30 * ratio_score + 0.18 * duration_balance + 0.18 * float(confirmed)
        labels = ["0", "A", "B", "C"]
        pts = [point(int(ctx.df.iloc[pv.index].time), pv.price, labels[j]) for j, pv in enumerate(seq)]
        lines = [line(pts[j], pts[j + 1]) for j in range(3)]
        if confirmed:
            confirm_point = point(int(ctx.df.iloc[end_idx].time), float(ctx.df.iloc[end_idx].close), "✓")
            lines.append(line(pts[-1], confirm_point, "dashed"))
            pts.append(confirm_point)
        events.append(make_event(
            ctx, pattern_id, seq[0].index, end_idx, confidence, confirmed, pts, lines,
            note="ABC修正后出现反向确认" if confirmed else "ABC修正可能完成，等待反向确认",
        ))
    return events


def _valid_impulse_prices(prices: Sequence[float], bullish: bool) -> bool:
    if len(prices) != 6:
        return False
    p = list(prices)
    if bullish:
        w1, w2, w3, w4, w5 = p[1] - p[0], p[1] - p[2], p[3] - p[2], p[3] - p[4], p[5] - p[4]
        rules = [w1 > 0, p[2] > p[0], p[3] > p[1], p[4] > p[1], p[5] > p[3], w3 >= min(w1, w5) * 0.75]
    else:
        w1, w2, w3, w4, w5 = p[0] - p[1], p[2] - p[1], p[2] - p[3], p[4] - p[3], p[4] - p[5]
        rules = [w1 > 0, p[2] < p[0], p[3] < p[1], p[4] < p[1], p[5] < p[3], w3 >= min(w1, w5) * 0.75]
    return all(rules) and 0.15 <= safe_div(w2, w1) <= 0.88 and 0.12 <= safe_div(w4, w3) <= 0.75


def detect_elliott_cycle(ctx: ScanContext, pattern_id: str) -> list[dict[str, Any]]:
    """识别 1-5 推动浪后接 A-B-C 修正的完整八浪周期。"""
    bullish = pattern_id == "elliott_cycle_bull"
    pivots = alternating_pivots(ctx)
    expected = (
        ["low", "high", "low", "high", "low", "high", "low", "high", "low"]
        if bullish else
        ["high", "low", "high", "low", "high", "low", "high", "low", "high"]
    )
    events: list[dict[str, Any]] = []
    for i in range(len(pivots) - 8):
        seq = pivots[i : i + 9]
        if [p.kind for p in seq] != expected:
            continue
        span = seq[-1].index - seq[0].index
        if span < 35 or span > 420:
            continue
        prices = [p.price for p in seq]
        if not _valid_impulse_prices(prices[:6], bullish):
            continue
        p5, pa, pb, pc = prices[5], prices[6], prices[7], prices[8]
        impulse_size = abs(p5 - prices[0])
        if bullish:
            structural = p5 > pb > pa and pc < pb and pc <= pa * 1.02 and pc > prices[0]
            wave_a, wave_b, wave_c = p5 - pa, pb - pa, pb - pc
        else:
            structural = p5 < pb < pa and pc > pb and pc >= pa * 0.98 and pc < prices[0]
            wave_a, wave_b, wave_c = pa - p5, pa - pb, pc - pb
        if not structural or min(wave_a, wave_b, wave_c) <= 0:
            continue
        retrace_total = abs(p5 - pc) / max(impulse_size, 1e-12)
        b_ratio = safe_div(wave_b, wave_a)
        c_ratio = safe_div(wave_c, wave_a)
        if not (0.18 <= retrace_total <= 0.88 and 0.20 <= b_ratio <= 0.90 and 0.50 <= c_ratio <= 2.10):
            continue
        trigger = pc + 0.236 * (pb - pc)
        confirm_idx = _future_confirmation_index(ctx, seq[8].index, trigger, bullish=bullish, bars=24)
        confirmed = confirm_idx is not None
        end_idx = confirm_idx if confirm_idx is not None else seq[8].index
        ratio_score = (
            clamp(1 - abs(retrace_total - 0.5) / 0.5)
            + clamp(1 - abs(b_ratio - 0.5) / 0.5)
            + clamp(1 - abs(c_ratio - 1.0) / 1.0)
        ) / 3
        confidence = 0.44 * ratio_score + 0.24 * clamp(impulse_size / max(abs(prices[0]) * 0.25, 1e-12)) + 0.16 * float(confirmed) + 0.16
        labels = ["0", "1", "2", "3", "4", "5", "A", "B", "C"]
        pts = [point(int(ctx.df.iloc[pv.index].time), pv.price, labels[j]) for j, pv in enumerate(seq)]
        lines = [line(pts[j], pts[j + 1]) for j in range(8)]
        if confirmed:
            confirm_point = point(int(ctx.df.iloc[end_idx].time), float(ctx.df.iloc[end_idx].close), "✓")
            lines.append(line(pts[-1], confirm_point, "dashed"))
            pts.append(confirm_point)
        events.append(make_event(
            ctx, pattern_id, seq[0].index, end_idx, confidence, confirmed, pts, lines,
            note="完整1-5/A-B-C周期后出现反向确认" if confirmed else "完整1-5/A-B-C周期，等待新周期确认",
        ))
    return events


def _event_direction(event: dict[str, Any]) -> str:
    direction = str(event.get("signal_direction") or event.get("direction") or "neutral")
    return direction if direction in {"bullish", "bearish"} else "neutral"


def _wilson_interval(wins: int, samples: int, z: float = 1.96) -> tuple[float, float]:
    if samples <= 0:
        return 0.0, 0.0
    p = wins / samples
    denominator = 1 + z * z / samples
    center = (p + z * z / (2 * samples)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * samples)) / samples) / denominator
    return clamp(center - margin), clamp(center + margin)


def _aggregate_outcomes(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    samples = len(rows)
    wins = sum(1 for row in rows if row["win"])
    if not samples:
        return {
            "samples": 0, "wins": 0, "win_rate": None,
            "win_rate_ci_low": None, "win_rate_ci_high": None,
            "avg_signed_return": None, "median_signed_return": None,
            "avg_mfe": None, "avg_mae": None, "avg_confidence": None,
            "calibration_gap": None, "sample_quality": "none",
        }
    ci_low, ci_high = _wilson_interval(wins, samples)
    signed = np.array([row["signed_return"] for row in rows], dtype=float)
    mfe = np.array([row["mfe"] for row in rows], dtype=float)
    mae = np.array([row["mae"] for row in rows], dtype=float)
    confidence = np.array([row["confidence"] for row in rows], dtype=float)
    win_rate = wins / samples
    quality = "high" if samples >= 50 else "medium" if samples >= 20 else "low"
    return {
        "samples": samples,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "win_rate_ci_low": round(ci_low, 4),
        "win_rate_ci_high": round(ci_high, 4),
        "avg_signed_return": round(float(np.mean(signed)), 6),
        "median_signed_return": round(float(np.median(signed)), 6),
        "avg_mfe": round(float(np.mean(mfe)), 6),
        "avg_mae": round(float(np.mean(mae)), 6),
        "avg_confidence": round(float(np.mean(confidence)), 4),
        "calibration_gap": round(float(np.mean(confidence)) - win_rate, 4),
        "sample_quality": quality,
    }


def calculate_historical_performance(
    df: pd.DataFrame,
    events: Sequence[dict[str, Any]],
    horizons: Sequence[int] = (5, 10, 20, 50),
) -> dict[str, Any]:
    """按形态结束/确认后的未来 N 根K线，计算方向一致率与收益分布。"""
    if df.empty:
        return {"horizons": list(horizons), "by_horizon": {}, "current_signals": {}}
    time_to_index = {int(ts): idx for idx, ts in enumerate(df["time"].to_numpy(int))}
    closes = df["close"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)

    directional_events = [event for event in events if _event_direction(event) != "neutral"]
    total_events = len(events)
    direction_summary: dict[str, Any] = {}
    weighted_denominator = sum(float(event.get("confidence", 0)) for event in directional_events)
    weighted_net = sum(
        float(event.get("confidence", 0)) * (1 if _event_direction(event) == "bullish" else -1)
        for event in directional_events
    )
    for direction in ("bullish", "bearish", "neutral"):
        subset = [event for event in events if _event_direction(event) == direction]
        direction_summary[direction] = {
            "count": len(subset),
            "share": round(len(subset) / total_events, 4) if total_events else 0.0,
            "avg_confidence": round(float(np.mean([event.get("confidence", 0) for event in subset])), 4) if subset else None,
        }
    current_signals = {
        "total": total_events,
        "bullish": direction_summary["bullish"],
        "bearish": direction_summary["bearish"],
        "neutral": direction_summary["neutral"],
        "weighted_net_bias": round(weighted_net / weighted_denominator, 4) if weighted_denominator else 0.0,
    }

    by_horizon: dict[str, Any] = {}
    for horizon in sorted({int(h) for h in horizons if int(h) > 0}):
        outcomes: list[dict[str, Any]] = []
        for event in directional_events:
            index = time_to_index.get(int(event["end_time"]))
            if index is None or index + horizon >= len(df):
                continue
            entry = closes[index]
            if entry <= 0:
                continue
            direction = _event_direction(event)
            future_return = closes[index + horizon] / entry - 1.0
            window_high = float(np.max(highs[index + 1 : index + horizon + 1]))
            window_low = float(np.min(lows[index + 1 : index + horizon + 1]))
            if direction == "bullish":
                signed_return = future_return
                mfe = window_high / entry - 1.0
                mae = window_low / entry - 1.0
            else:
                signed_return = -future_return
                mfe = entry / max(window_low, 1e-12) - 1.0
                mae = entry / max(window_high, 1e-12) - 1.0
            outcomes.append({
                "pattern": event["pattern"],
                "name": event["name"],
                "direction": direction,
                "confidence": float(event.get("confidence", 0)),
                "confirmed": bool(event.get("confirmed")),
                "signed_return": float(signed_return),
                "raw_return": float(future_return),
                "mfe": float(mfe),
                "mae": float(mae),
                "win": bool(signed_return > 0),
            })
        by_pattern = []
        for pattern_id in sorted({row["pattern"] for row in outcomes}):
            subset = [row for row in outcomes if row["pattern"] == pattern_id]
            aggregate = _aggregate_outcomes(subset)
            meta = CATALOG_BY_ID.get(pattern_id, {})
            aggregate.update({
                "pattern": pattern_id,
                "name": meta.get("name", pattern_id),
                "direction": subset[0]["direction"] if subset else meta.get("direction", "neutral"),
                "experimental": bool(meta.get("experimental")),
            })
            by_pattern.append(aggregate)
        by_pattern.sort(key=lambda row: (row["samples"], row["win_rate"] or 0), reverse=True)

        confidence_buckets = []
        bucket_edges = [(0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.01)]
        for low, high in bucket_edges:
            subset = [row for row in outcomes if low <= row["confidence"] < high]
            aggregate = _aggregate_outcomes(subset)
            aggregate.update({"label": f"{int(low * 100)}-{int(min(high, 1.0) * 100)}%", "low": low, "high": min(high, 1.0)})
            confidence_buckets.append(aggregate)

        by_horizon[str(horizon)] = {
            "overall": _aggregate_outcomes(outcomes),
            "bullish": _aggregate_outcomes([row for row in outcomes if row["direction"] == "bullish"]),
            "bearish": _aggregate_outcomes([row for row in outcomes if row["direction"] == "bearish"]),
            "by_pattern": by_pattern,
            "confidence_buckets": confidence_buckets,
        }
    return {
        "horizons": sorted(int(h) for h in by_horizon),
        "default_horizon": 20 if "20" in by_horizon else (sorted(int(h) for h in by_horizon)[0] if by_horizon else None),
        "current_signals": current_signals,
        "by_horizon": by_horizon,
        "methodology": {
            "entry": "形态结束或突破确认K线的收盘价",
            "success": "未来N根K线收盘方向与形态预期方向一致",
            "neutral": "未确认突破方向的中性形态不计入胜率",
            "warning": "属于当前标的、周期和样本区间内的历史样本回测；重叠形态并非独立样本，不代表未来收益。",
        },
    }


def _limit_events_per_pattern(events: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(event["pattern"], []).append(event)
    result: list[dict[str, Any]] = []
    for group in grouped.values():
        result.extend(sorted(group, key=lambda e: (e["end_time"], e["confidence"]), reverse=True)[:limit])
    return sorted(result, key=lambda e: (e["end_time"], e["confidence"]), reverse=True)


def deduplicate(events: Sequence[dict[str, Any]], max_events_per_pattern: int = 20) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(events, key=lambda e: (e["pattern"], e["end_time"], e["confidence"])):
        group = grouped.setdefault(event["pattern"], [])
        duplicate = False
        for previous in group[-4:]:
            span = max(1, event["end_time"] - event["start_time"])
            if abs(event["end_time"] - previous["end_time"]) <= span * 0.12:
                duplicate = True
                if event["confidence"] > previous["confidence"]:
                    group[group.index(previous)] = event
                break
        if not duplicate:
            group.append(event)
    result: list[dict[str, Any]] = []
    for group in grouped.values():
        result.extend(sorted(group, key=lambda e: (e["end_time"], e["confidence"]))[-max_events_per_pattern:])
    return sorted(result, key=lambda e: (e["end_time"], e["confidence"]), reverse=True)


def scan_patterns(
    candles: Sequence[dict[str, Any]],
    selected_patterns: Optional[Sequence[str]] = None,
    min_confidence: float = 0.55,
    confirmed_only: bool = False,
    max_bars: int = 1800,
    max_events_per_pattern: int = 20,
    include_statistics: bool = False,
    performance_horizons: Sequence[int] = (5, 10, 20, 50),
) -> dict[str, Any]:
    df = normalize_frame(candles)
    if len(df) > max_bars:
        df = df.iloc[-max_bars:].reset_index(drop=True)
    selected = set(selected_patterns or CATALOG_BY_ID.keys()).intersection(CATALOG_BY_ID)
    if len(df) < 20 or not selected:
        empty = {"events": [], "bars_scanned": len(df), "selected": sorted(selected)}
        if include_statistics:
            empty["performance"] = calculate_historical_performance(df, [], performance_horizons)
        return empty

    highs, lows = find_pivots(df)
    ctx = ScanContext(
        df=df,
        highs=highs,
        lows=lows,
        atr=calculate_atr(df),
        median_price=float(df["close"].median()),
    )
    events: list[dict[str, Any]] = []

    if "double_top" in selected:
        events.extend(detect_double_patterns(ctx, "double_top"))
    if "double_bottom" in selected:
        events.extend(detect_double_patterns(ctx, "double_bottom"))
    if "triple_top" in selected:
        events.extend(detect_triple_patterns(ctx, "triple_top"))
    if "triple_bottom" in selected:
        events.extend(detect_triple_patterns(ctx, "triple_bottom"))
    if "head_shoulders" in selected:
        events.extend(detect_head_shoulders(ctx, "head_shoulders"))
    if "inverse_head_shoulders" in selected:
        events.extend(detect_head_shoulders(ctx, "inverse_head_shoulders"))
    if "cup_handle" in selected:
        events.extend(detect_cup_handle(ctx, "cup_handle"))
    if "inverse_cup_handle" in selected:
        events.extend(detect_cup_handle(ctx, "inverse_cup_handle"))
    events.extend(detect_boundaries(ctx, selected))
    events.extend(detect_flags(ctx, selected))
    if "elliott_impulse_bull" in selected:
        events.extend(detect_elliott(ctx, "elliott_impulse_bull"))
    if "elliott_impulse_bear" in selected:
        events.extend(detect_elliott(ctx, "elliott_impulse_bear"))
    if "elliott_correction_bull" in selected:
        events.extend(detect_elliott_correction(ctx, "elliott_correction_bull"))
    if "elliott_correction_bear" in selected:
        events.extend(detect_elliott_correction(ctx, "elliott_correction_bear"))
    if "elliott_cycle_bull" in selected:
        events.extend(detect_elliott_cycle(ctx, "elliott_cycle_bull"))
    if "elliott_cycle_bear" in selected:
        events.extend(detect_elliott_cycle(ctx, "elliott_cycle_bear"))

    history_limit = max(max_events_per_pattern, 160) if include_statistics else max_events_per_pattern
    all_events = deduplicate(events, max_events_per_pattern=history_limit)
    all_events = [
        event for event in all_events
        if event["confidence"] >= min_confidence and (event["confirmed"] or not confirmed_only)
    ]
    display_events = _limit_events_per_pattern(all_events, max_events_per_pattern)
    latest_time = int(df.iloc[-1].time)
    result = {
        "events": display_events,
        "bars_scanned": len(df),
        "pivot_highs": len(highs),
        "pivot_lows": len(lows),
        "selected": sorted(selected),
        "latest_time": latest_time,
        "historical_event_count": len(all_events),
        "disclaimer": "图表形态由启发式算法识别，历史方向一致率属于样本内回测，不等同于未来胜率，也不构成交易建议。",
    }
    if include_statistics:
        result["performance"] = calculate_historical_performance(df, all_events, performance_horizons)
    return result
