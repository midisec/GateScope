"""Causal, conditional price zones. A price touch is NOT a formed structure.

Fibonacci ratios are hypotheses, not mathematical guarantees. Adaptive targets
use only earlier independently sampled, completed next legs, bounded to the
last 256 examples. Confirmation probabilities include failures separately.
"""

from __future__ import annotations

import copy
import math

from gatescope.quant.pattern_study import digest
from gatescope.quant.structure_detection import known_points, value_at


def quantile(values, q):
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = int(at)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] * (hi - at) + values[hi] * (at - lo) if hi != lo else values[lo]


def guided_interval(hard_lower, hard_upper, guide_lower, guide_upper, center):
    """Reference ratios guide a target; they do not make a valid wave impossible."""
    lower, upper = max(hard_lower, guide_lower), min(hard_upper, guide_upper)
    if lower <= upper:
        return lower, upper, min(upper, max(lower, center)), False
    # No customary ratio fits. If structure imposes no upper bound, use one
    # reference-band width beyond the required breakout; it is an observation
    # window, NOT an invented hard cap on an extended fifth wave.
    upper = hard_upper if math.isfinite(hard_upper) else hard_lower + (guide_upper - guide_lower)
    return hard_lower, upper, hard_lower + (upper - hard_lower) / 2, True


def forecast(event, state, issued_at, deadline, last_close):
    pts = known_points(event)
    p = [x["price"] for x in pts]
    key = event["pattern"]
    levels = event["levels"]
    atr = levels.get("atr", levels.get("buffer", 0.1) / 0.15)
    sign = 1 if key.endswith("bull") else -1
    spec: tuple[float, float, float, float, float, str, int | None] | None = None
    condition = "形态需通过几何校验和右侧 4 根枢轴确认；触价不等于形成"
    structural_fallback = False
    # Guides use candle OPEN coordinates, just like lifecycle.resolve_rule.
    # issued_at is the CLOSE/availability time, not an extra candle to extrapolate.
    reference_time = event["end_time"]
    if event["phase"] == "FORMING" and len(p) >= 2:
        if key in {"double_top", "double_bottom"}:
            # A second extremum near the first; never use the provisional 2? close as a pivot.
            spec = (p[0], levels.get("formation_atr", atr), 0.0, -0.75, 0.75, "第二拐点候选区", 2)
        elif key.startswith("elliott_impulse"):
            w1 = abs(p[1] - p[0])
            if len(p) == 3:
                lower = max(1.0, sign * (p[1] - p[2]) / w1 + 1e-6)
                spec = (p[2], sign * w1, 1.618, lower, 2.618, "下一段：第 3 浪", 3)
                condition = "第 3 浪须越过第 1 浪终点；仍需第 4、5 浪及右侧枢轴确认"
            elif len(p) == 4:
                w3 = abs(p[3] - p[2])
                lower, upper, center, structural_fallback = guided_interval(
                    math.nextafter(0.0, 1.0),
                    math.nextafter(sign * (p[3] - p[1]) / w3, 0.0),
                    0.236,
                    0.5,
                    0.382,
                )
                spec = (p[3], -sign * w3, center, lower, upper, "下一段：第 4 浪", 4)
                condition = "第 4 浪不得进入第 1 浪价格区；还需第 5 浪和枢轴确认"
            elif len(p) == 5:
                w3 = abs(p[3] - p[2])
                lower, upper, center, structural_fallback = guided_interval(
                    math.nextafter(sign * (p[3] - p[4]) / w1, math.inf),
                    w3 / w1 if w3 < w1 else math.inf,
                    0.618,
                    1.618,
                    1.0,
                )
                spec = (p[4], sign * w1, center, lower, upper, "第 5 浪完成候选区", 5)
                condition = "第 5 浪须越过第 3 浪，且第 3 浪不得最短；再等待右侧 4 根确认"
                if structural_fallback and w3 >= w1:
                    condition += "；本次数浪没有第 5 浪硬上限，区间仅取突破位之外一个第 1 浪长度作观察参考"
        elif key.startswith(("elliott_correction", "elliott_cycle")):
            abc = p[5:] if key.startswith("elliott_cycle") else p
            if len(abc) == 2:
                spec = (abc[1], sign * abs(abc[1] - abc[0]), 0.618, 0.382, 0.85, "下一段：B 浪", len(p))
                condition = "B 浪反弹不得越过修正起点；仍需 C 浪及枢轴确认"
            elif len(abc) == 3:
                length = abs(abc[1] - abc[0])
                lower = max(0.618, sign * (abc[2] - abc[1]) / length)
                upper = 1.618
                if key.startswith("elliott_cycle"):
                    upper = min(upper, sign * (abc[2] - p[0]) / length - 1e-6)
                spec = (abc[2], -sign * length, 1.0, lower, upper, "C 浪完成候选区", len(p))
                condition = "C 浪至少回到 A 浪极值附近，整体比例通过校验后再等待右侧 4 根确认"
    elif event["phase"] in {"FORMED", "CONFIRMED"} and p:
        condition = "结构已形成；方向未定前不发布单侧目标，确认仍按连续收盘与边界规则判定"
        side = {"bullish": 1, "bearish": -1}.get(event.get("signal_direction"), 0)
        if side and "trigger" in levels:
            trigger = value_at(levels["trigger"], reference_time)
            if key.startswith("elliott"):
                spec = (p[-1], side * abs(p[-2] - p[-1]), 0.618, 0.382, 1.0, "确认后的条件目标", None)
            else:
                if key in {"head_shoulders", "inverse_head_shoulders"}:
                    head = pts[2]  # Geometry audit fixes the head's position; labels are presentation.
                    height = abs(head["price"] - value_at(levels["trigger"], head["time"]))
                else:
                    height = max(p) - min(p)
                spec = (trigger, side * height, 1.0, 0.618, 1.0, "确认后的量度目标", None)
            condition = "只有连续两根收盘通过确认线及 ATR 缓冲后，此价格目标才具备前提"
        elif side and "upper" in levels:
            top = value_at(levels["upper"], reference_time)
            bottom = value_at(levels["lower"], reference_time)
            spec = (
                top if side > 0 else bottom,
                side * (top - bottom),
                1.0,
                0.618,
                1.0,
                "边界突破后的量度目标",
                None,
            )
            condition = "先连续两根收盘突破对应边界及 ATR 缓冲；收敛边界交叉则失效"
    triggers = []
    direction = {"bullish": 1, "bearish": -1}.get(event.get("signal_direction"), 0)
    # Forming double tops/bottoms have neutral trading direction but a known neckline side.
    if key in {"double_top", "double_bottom"}:
        direction = -1 if key == "double_top" else 1
    if "trigger" in levels and direction:
        triggers = [
            {
                "side": direction,
                "price": value_at(levels["trigger"], reference_time) + direction * levels["buffer"],
                "edge": levels["trigger"],
            }
        ]
    elif "upper" in levels:
        triggers = [
            {
                "side": s,
                "price": value_at(levels[k], reference_time) + s * levels["buffer"],
                "edge": levels[k],
            }
            for s, k in ((1, "upper"), (-1, "lower"))
            if not direction or s == direction
        ]
    result = {
        "version": "structure-forecast-2",
        "issued_at": issued_at,
        "deadline_time": deadline,
        "basis_end": event["end_time"],
        "confirmation_basis_time": reference_time,
        "guideline_fit": "STRUCTURAL_FALLBACK" if structural_fallback else "REFERENCE_RANGE",
        "kind": "NEXT_PIVOT" if event["phase"] == "FORMING" else "CONDITIONAL_MOVE",
        "condition": condition,
        "confirmation": triggers,
        "confirmation_closes": levels["closes"],
        "source": "THEORY_ONLY",
        "history_count": 0,
        "target": None,
        "status": "UNAVAILABLE",
        "unavailable_reason": "当前结构无唯一的单侧目标",
    }
    if structural_fallback:
        result["condition"] += "；常见斐波那契区间不适用，本次仅发布结构约束内的候选区，不代表命中概率"
    if spec:
        origin, scale, center, lower, upper, label, index = spec
        if scale and lower <= upper:
            cell = f"{key}:{event['phase']}:{len(p)}"
            history = [
                r
                for r in state.get("forecast_training", {}).get(cell, [])
                if r[0] < issued_at
                and r[2] != event["id"]
                and lower <= r[1] <= upper
                and r[0] >= issued_at - 365 * 86400
            ]
            center = min(upper, max(lower, center))
            theory = center
            if len(history) >= 20:
                # Shrink the empirical median toward the theory; never violate hard constraints.
                weight = min(0.75, len(history) / (len(history) + 40))
                center = (1 - weight) * center + weight * quantile([r[1] for r in history], 0.5)
                result["source"] = "PRIOR_COMPLETED_LEGS_SHRUNK"
            values = sorted([origin + scale * lower, origin + scale * upper])
            point = origin + scale * center
            # Ratio arithmetic can round a strict endpoint back onto the forbidden
            # wave-1/3 price. Keep the published interval on the admissible side.
            if key.startswith("elliott_impulse") and event["phase"] == "FORMING" and len(p) in {4, 5}:
                if len(p) == 4:
                    strict_low, strict_high = sorted((p[1], p[3]))
                    values = [
                        max(values[0], math.nextafter(strict_low, math.inf)),
                        min(values[1], math.nextafter(strict_high, -math.inf)),
                    ]
                elif sign > 0:
                    values[0] = max(values[0], math.nextafter(p[3], math.inf))
                else:
                    values[1] = min(values[1], math.nextafter(p[3], -math.inf))
                point = min(values[1], max(values[0], point))
            if values[0] <= point <= values[1] and all(math.isfinite(v) and v > 0 for v in [*values, point]):
                result.update(
                    target={
                        "price": point,
                        "lower": values[0],
                        "upper": values[1],
                        "label": label,
                        "theory_price": origin + scale * theory,
                        "origin": origin,
                        "scale": scale,
                        "index": index,
                        "cell": cell,
                    },
                    history_count=len(history),
                    history_cutoff=max((r[0] for r in history), default=None),
                    status="ALREADY_IN_ZONE" if values[0] <= last_close <= values[1] else "PENDING",
                    unavailable_reason=None,
                )
        else:
            result["unavailable_reason"] = "理论比例区间与硬性波浪约束冲突，暂不发布点位"
    result["id"] = digest([event["id"], issued_at, result])
    return result


def issue(record, state, ts, last_close):
    current = forecast(record["latest"], state, ts, record["deadline_time"], last_close)
    record["latest"]["forecast"] = current
    if "forecast" not in record["first"]:
        record["first"]["forecast"] = copy.deepcopy(current)
        record["forecast_outcome"] = {
            "status": ("PENDING" if current["kind"] == "NEXT_PIVOT" else "CONDITIONAL_UNSCORED")
            if current["target"]
            else "UNAVAILABLE"
        }
    record["latest"]["initial_forecast"] = copy.deepcopy(record["first"]["forecast"])
    record["latest"]["forecast_outcome"] = copy.deepcopy(record["forecast_outcome"])


def assess(record, bar, ts, decision=None):
    """Only AFTER issuance; a same-bar touch/invalidation has unknown ordering."""
    original = record.get("first", {}).get("forecast")
    outcome = record.get("forecast_outcome", {})
    if (
        not original
        or not original.get("target")
        or outcome.get("status") != "PENDING"
        or bar["ts"] < original["issued_at"]
    ):
        return False
    target = original["target"]
    touched = bar["low"] <= target["price"] <= bar["high"]
    if decision and decision[0] == "CENSORED":
        status = "CENSORED"
    elif touched and decision and decision[0] == "INVALIDATED":
        status = "AMBIGUOUS"
    elif touched:
        status = "TOUCHED"
    elif decision:
        status = "NOT_REACHED"
    else:
        return False
    record["forecast_outcome"] = {"status": status, "observed_time": ts, "target_price": target["price"]}
    record["latest"]["forecast_outcome"] = copy.deepcopy(record["forecast_outcome"])
    return True


def learn_formed_leg(record, new_event, state, ts):
    # Each previously issued next-leg forecast gets its own causal observation.
    # Learning only the first one would leave wave-4/5 calibration permanently empty.
    original = record["latest"].get("forecast", {})
    target = original.get("target")
    learned = record.setdefault("learned_forecasts", [])
    if not target or target["index"] is None or original["id"] in learned:
        return
    points = known_points(new_event)
    i = target["index"]
    if len(points) <= i:
        return
    learned.append(original["id"])
    actual = points[i]
    record["formation_observation"] = {
        "price": actual["price"],
        "pivot_time": actual["time"],
        "known_at": ts,
        "predicted_before_pivot": actual["time"] >= original["issued_at"],
    }
    if actual["time"] < original["issued_at"] or not record["stages"].get("FORMING", {}).get("sampled"):
        return
    ratio = (actual["price"] - target["origin"]) / target["scale"]
    state.setdefault("pending_forecast_training", []).append([target["cell"], [ts, ratio, record["id"]]])
