"""Frozen pattern-derived observation plans, never executable order instructions.

Callers supply the original lifecycle event and the candle that closed when that
event was recorded. This module does not fetch current prices, rerun a detector,
infer a win rate, or authorize trading. Invalidation follows the model's original
close/wick rule; it is not an exchange-native protective stop.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping

VERSION = "pattern-trade-notice-1"
_PHASES = {"FORMING": "形成中", "FORMED": "已形成，等待突破", "CONFIRMED": "已确认"}
_REASONS = {
    "PHASE_NOT_ACTIONABLE": "当前阶段不提供做单观察计划",
    "DIRECTION_UNAVAILABLE": "交易方向尚未确定",
    "REFERENCE_UNAVAILABLE": "缺少该事件对应的已收盘价格",
    "REFERENCE_TIME_INVALID": "参考 K 线时间不属于该事件的历史区间",
    "CONFIRMATION_UNAVAILABLE": "确认条件不完整",
    "STOP_UNAVAILABLE": "缺少有效结构失效线",
    "STOP_DIRECTION_INVALID": "失效线不在风险方向",
    "TARGET_UNAVAILABLE": "暂无冻结的条件目标",
    "TARGET_NOT_PROFIT_SIDE": "目标已无有利价差，不追价",
    "FORECAST_TIME_INVALID": "目标并非事件发生时可用的预测",
    "RISK_REWARD_UNAVAILABLE": "无法可靠计算价差盈亏比",
}


def _number(value: Any, *, positive: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and (not positive or number > 0) else None


def _timestamp(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and 0 <= number <= 253402300799 and number.is_integer() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _label(value: Any, maximum: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:maximum] or None


def _edge_price(edge: Any, timestamp: int | None) -> float | None:
    """Same projection as structure_detection.value_at, with finite validation."""
    if timestamp is None:
        return None
    line = _mapping(edge)
    first, last = _mapping(line.get("from")), _mapping(line.get("to"))
    start, end = _timestamp(first.get("time")), _timestamp(last.get("time"))
    first_price, last_price = _number(first.get("price"), positive=True), _number(last.get("price"), positive=True)
    if start is None or end is None or first_price is None or last_price is None:
        return None
    if end < start or end > timestamp or (end == start and first_price != last_price):
        return None
    projected = first_price if end == start else first_price + (last_price - first_price) * (timestamp - start) / (end - start)
    return _number(projected, positive=True)


def _confirmation(levels: Mapping[str, Any], timestamp: int | None, direction: int) -> dict[str, Any] | None:
    buffer = _number(levels.get("buffer"))
    closes = _number(levels.get("closes"))
    if buffer is None or buffer < 0 or closes is None or not closes.is_integer() or not 1 <= closes <= 100:
        return None
    boundaries = [(direction, "trigger")] if direction and "trigger" in levels else [
        (side, key) for side, key in ((1, "upper"), (-1, "lower")) if not direction or side == direction
    ]
    prices = []
    for side, key in boundaries:
        projected = _edge_price(levels.get(key), timestamp)
        price = _number(projected + side * buffer, positive=True) if projected is not None else None
        if price is not None:
            prices.append({"side": side, "price": price, "comparison": "CLOSE_ABOVE" if side > 0 else "CLOSE_BELOW"})
    return {"prices": prices, "closes": int(closes), "projected_at": timestamp} if prices else None


def build_pattern_trade_notice(event: Mapping[str, Any], last_bar: Mapping[str, Any]) -> dict[str, Any]:
    """Build JSON-safe immutable references from a frozen event and its close.

    ``last_bar`` must have ``ts`` (bar-open epoch seconds) and ``close``. The
    caller must select the actual event candle, not the most recently available
    candle. READY only means the *observation* has coherent price references.
    """
    event, last_bar = _mapping(event), _mapping(last_bar)
    phase = _label(event.get("phase")) or "UNKNOWN"
    direction_name = _label(event.get("signal_direction")) or "neutral"
    direction = {"bullish": 1, "bearish": -1}.get(direction_name, 0)
    levels, forecast = _mapping(event.get("levels")), _mapping(event.get("forecast"))
    timestamp, observed_at = _timestamp(last_bar.get("ts")), _timestamp(event.get("observed_time"))
    close = _number(last_bar.get("close"), positive=True)
    score = _number(event.get("confidence"))
    score = score if score is not None and 0 <= score <= 1 else None
    reasons = []
    if phase not in ("FORMED", "CONFIRMED"):
        reasons.append("PHASE_NOT_ACTIONABLE")
    if not direction:
        reasons.append("DIRECTION_UNAVAILABLE")
    if close is None or timestamp is None:
        reasons.append("REFERENCE_UNAVAILABLE")
    if observed_at is None or (timestamp is not None and timestamp >= observed_at):
        reasons.append("REFERENCE_TIME_INVALID")
    confirmation = _confirmation(levels, timestamp, direction)
    if confirmation is None:
        reasons.append("CONFIRMATION_UNAVAILABLE")

    invalid_side = _number(levels.get("invalid_side"))
    hard = levels.get("hard") is True
    buffer = 0.0 if hard else _number(levels.get("buffer"))
    base = _edge_price(levels.get("invalid_edge"), timestamp) if "invalid_edge" in levels else _number(levels.get("invalid_price"), positive=True)
    stop: dict[str, Any] | None = None
    if invalid_side not in (-1.0, 1.0) or base is None or buffer is None or buffer < 0:
        reasons.append("STOP_UNAVAILABLE")
    else:
        price = _number(base + invalid_side * buffer, positive=True)
        if price is None:
            reasons.append("STOP_UNAVAILABLE")
        elif not direction or invalid_side != -direction or close is None or direction * (close - price) <= 0:
            reasons.append("STOP_DIRECTION_INVALID")
        else:
            stop = {
                "price": price,
                "boundary_price": base,
                "mode": "WICK_INVALIDATION" if hard else "CLOSE_INVALIDATION",
                "side": int(invalid_side),
                "projected_at": timestamp,
                "exchange_order_placed": False,
            }

    target: dict[str, Any] | None = None
    raw_target = _mapping(forecast.get("target"))
    target_price = _number(raw_target.get("price"), positive=True)
    lower = _number(raw_target.get("lower", target_price), positive=True)
    upper = _number(raw_target.get("upper", target_price), positive=True)
    issued_at = _timestamp(forecast.get("issued_at"))
    if forecast.get("kind") != "CONDITIONAL_MOVE" or target_price is None or lower is None or upper is None or not lower <= target_price <= upper:
        reasons.append("TARGET_UNAVAILABLE")
    elif issued_at is None or observed_at is None or issued_at > observed_at:
        reasons.append("FORECAST_TIME_INVALID")
    elif not direction or close is None or direction * (target_price - close) <= 0:
        reasons.append("TARGET_NOT_PROFIT_SIDE")
    else:
        target = {
            "price": target_price,
            "lower": lower,
            "upper": upper,
            "kind": "CONDITIONAL_MOVE",
            "issued_at": issued_at,
            "source": _label(forecast.get("source")),
        }

    ratio = None
    if phase == "CONFIRMED" and not reasons and close is not None and stop and target:
        ratio = _number(abs(target["price"] - close) / abs(close - stop["price"]), positive=True)
        if ratio is None:
            reasons.append("RISK_REWARD_UNAVAILABLE")
    status = "WAIT_CONFIRMATION" if phase == "FORMED" else "READY" if phase == "CONFIRMED" and not reasons else "UNAVAILABLE"
    return {
        "version": VERSION,
        "status": status,
        "phase": phase,
        "pattern": _label(event.get("pattern")),
        "name": _label(event.get("name")) or _label(event.get("pattern")) or "未标注形态",
        "direction": direction_name if direction else "neutral",
        "observed_at": observed_at,
        "reference_bar_time": timestamp,
        "entry": {
            "price": close,
            "basis": "CONFIRMATION_CLOSE" if phase == "CONFIRMED" else "OBSERVATION_CLOSE",
            "executable": False,
        },
        "confirmation": confirmation,
        "stop": stop,
        "target": target,
        "reward_risk_ratio": ratio,
        "reward_risk_basis": "PRICE_DISTANCE_ONLY_BEFORE_COSTS_NOT_AN_ADMISSION_GATE",
        "geometry_score": score,
        "geometry_is_probability": False,
        "reason_codes": reasons,
        "evidence": {
            "candidate_id": _label(event.get("id")),
            "context_id": _label(event.get("context_id")),
            "evidence_id": _label(event.get("evidence_id")),
            "detector_version": _label(event.get("detector_version")),
            "forecast_id": _label(forecast.get("id")),
        },
        "auto_order": False,
    }


def render_pattern_trade_notice(notice: Mapping[str, Any]) -> str:
    """Short plain-text Telegram body; no tokens, HTML or order confirmation."""
    phase = str(notice.get("phase", "UNKNOWN"))
    direction = {"bullish": "偏多", "bearish": "偏空"}.get(str(notice.get("direction")), "方向待定")
    lines = [
        f"{_label(notice.get('name'), 48) or '形态'} · {_PHASES.get(phase, '暂不可用')} · {direction}",
        "做单观察 · 尚未下单",
    ]
    entry = _mapping(notice.get("entry"))
    price = _number(entry.get("price"), positive=True)
    if price is not None:
        label = "入场参考（确认收盘，非成交价）" if phase == "CONFIRMED" else "观察收盘（非入场信号）"
        lines.append(f"{label} {price:.8g}")
    confirmation = _mapping(notice.get("confirmation"))
    if phase == "FORMED" and confirmation:
        for item in confirmation.get("prices", []):
            side = "高于" if item["side"] > 0 else "低于"
            lines.append(f"确认条件：连续 {confirmation['closes']} 根收盘{side} {item['price']:.8g}")
        lines.append("斜线按各根 K 线时刻延伸；当前仍须等待突破确认。")
    stop = _mapping(notice.get("stop"))
    if stop:
        mode = "触价失效" if stop.get("mode") == "WICK_INVALIDATION" else "收盘失效"
        lines.append(f"止损参考（结构{mode}） {stop['price']:.8g}")
    target = _mapping(notice.get("target"))
    if target:
        lines.append(f"条件目标 {target['price']:.8g} · 目标区 {target['lower']:.8g}–{target['upper']:.8g}")
    ratio = _number(notice.get("reward_risk_ratio"), positive=True)
    if ratio is not None:
        lines.append(f"价差盈亏比 {ratio:.2f}:1（未扣费用，不是准入门槛）")
    score = _number(notice.get("geometry_score"))
    if score is not None and 0 <= score <= 1:
        lines.append(f"几何匹配 {score * 100:.0f} 分（非胜率 / 概率）")
    reasons = [_REASONS[reason] for reason in notice.get("reason_codes", []) if reason in _REASONS]
    if reasons:
        lines.append("待核对：" + "；".join(reasons[:3]))
    lines.append("点位冻结于本次事件；失效线不等于已挂止损，实际保护单需另行设置。")
    return "\n".join(lines)
