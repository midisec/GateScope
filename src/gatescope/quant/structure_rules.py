"""Auditable chart definitions, not estimates of trading profitability.

Hard geometry and price order are separate from aesthetic/Fibonacci guidelines.
No score cutoff, volume assertion or unsupported wave subdivision is invented.
"""
from __future__ import annotations

import math

EXTRA_PATTERNS = [
    {"id": "bull_breakout_retest", "name": "阻力突破回踩", "group": "突破回测", "direction": "bullish"},
    {"id": "bear_breakout_retest", "name": "支撑跌破反抽", "group": "突破回测", "direction": "bearish"},
]
REVERSALS = {"double_top", "double_bottom", "triple_top", "triple_bottom", "head_shoulders", "inverse_head_shoulders"}


def audit(event, candles, volatility):
    """Return (hard rejection, visible audit). Never filter on contextual fit."""
    key = event["pattern"]
    pts = [p for p in event["points"] if p.get("label") not in {"E", "2?", "✓"}]
    prices = [p["price"] for p in pts]
    # refine() owns OHLC/source-finality and touch-residual checks. Do not claim
    # those were performed by this geometry-only function or its direct callers.
    facts = {"rules_version": "geometry-3.3", "checks": [], "warnings": []}
    if not event.get("region") and not key.endswith(("flag", "pennant")):
        if any(a["time"] >= b["time"] for a, b in zip(pts, pts[1:], strict=False)):
            return "ANCHOR_ORDER", facts
        facts["checks"].append("锚点严格按时间排序")
    if key in REVERSALS:
        expected = (2 if event.get("phase") == "FORMING" else 3) if key.startswith("double") else 5
        if len(pts) != expected:
            return "REVERSAL_ANCHOR_COUNT", facts
        # Labels determine which observed turns refine() uses for the neckline.
        # Checking price alternation alone allows a head to be used as a neck.
        if any(str(point.get("label", "")).startswith("N") != bool(index % 2)
               for index, point in enumerate(pts)):
            return "NECKLINE_ANCHOR_ORDER", facts
        sign = -1 if key.endswith("bottom") or key == "inverse_head_shoulders" else 1
        p = [v * sign for v in prices]
        if any((p[i] <= p[i+1] if i % 2 == 0 else p[i] >= p[i+1]) for i in range(len(p)-1)):
            return "REVERSAL_LEG_ORDER", facts
        facts["checks"].append("拐点数量、峰谷交替与颈线锚点位置")
        if key in {"head_shoulders", "inverse_head_shoulders"} and p[2] <= max(p[0], p[4]):
            return "HEAD_NOT_DOMINANT", facts
        if key in {"head_shoulders", "inverse_head_shoulders"}:
            facts["checks"].append("头部超出两肩")
        # Geometry can exist inside a range; label context rather than erase it.
        start = next(i for i, b in enumerate(candles) if b["ts"] == pts[0]["time"])
        lookback = min(40, max(10, (len(candles)-start)//2))
        prior = candles[max(0, start-lookback):start+1]
        move = sign*(prior[-1]["close"]-prior[0]["close"])/volatility
        facts["prior_trend"] = {"bars": len(prior), "directional_atr": move,
            "status": "ALIGNED" if len(prior)>=10 and move>=1.5 else "INSUFFICIENT" if len(prior)<10 else "NOT_ALIGNED"}
        if facts["prior_trend"]["status"] != "ALIGNED":
            facts["warnings"].append("前置趋势不足：保留几何候选，不视为已验证反转")
    elif "cup_handle" in key:
        if len(pts) != 4:
            return "CUP_ANCHORS", facts
        sign = -1 if key.startswith("inverse") else 1
        left, bottom, right, handle = [v*sign for v in prices]
        depth = (left+right)/2-bottom
        right_depth = right-bottom
        # The handle descends from the RIGHT rim, so its upper-half rule must
        # use that same rim-to-bottom depth. An average of unequal rims wrongly
        # admits too-deep handles or excludes otherwise valid asymmetric cups.
        if depth <= 0 or min(left, right) <= bottom or not 0 < right-handle <= .5*right_depth:
            return "CUP_HANDLE_DEPTH", facts
        body = [b for b in candles if pts[0]["time"] <= b["ts"] <= pts[2]["time"]]
        handle_body = [b for b in candles if pts[2]["time"] <= b["ts"]]
        # Cups are intentionally close-based, matching the detector/refine()
        # anchors. Wicks alone do not invalidate their close-based bowl.
        # ULP tolerance addresses arithmetic only, not extra ATR/fit cutoffs.
        epsilon = 8*math.ulp(max(abs(value) for value in prices))
        if not body or abs(bottom-min(sign*b["close"] for b in body)) > epsilon:
            return "CUP_BODY_EXTREME", facts
        if not handle_body or abs(handle-min(sign*b["close"] for b in handle_body)) > epsilon:
            return "CUP_HANDLE_EXTREME", facts
        # Compare a rounded bowl to a straight-sided V on the SAME anchors.
        # Low dwell alone is not grounds for deleting an asymmetric/noisy cup.
        dwell = sum(sign*b["close"] <= bottom+.2*depth for b in body)/max(1,len(body))
        errors=[]
        for power in (1,2):
            residuals=[]
            for b in body:
                side=pts[0] if b["ts"]<=pts[1]["time"] else pts[2]
                x=abs((b["ts"]-pts[1]["time"])/(side["time"]-pts[1]["time"]))
                predicted=bottom+(side["price"]*sign-bottom)*x**power
                residuals.append(((sign*b["close"]-predicted)/depth)**2)
            errors.append(sum(residuals)/max(1,len(body)))
        if len(body)<16 or (dwell<.25 and errors[0]<.04 and errors[0]<.6*errors[1]):
            return "CUP_NOT_ROUNDED", facts
        facts["metrics"] = {"bottom_dwell_fraction": dwell, "handle_depth_ratio": (right-handle)/right_depth,
            "handle_depth_basis": "RIGHT_RIM_TO_BOTTOM",
            "v_normalized_mse":errors[0],"bowl_normalized_mse":errors[1]}
        facts["checks"].extend(["杯体与柄的真实收盘极值", "杯体与 V 型对照校验", "柄位于右杯缘至杯底的上半区"])
        if dwell<.25 or errors[1]>.08:
            facts["warnings"].append("圆弧不典型：保留候选，需结合历史样本评估")
        facts["warnings"].append("杯柄可为持续或反转，不能仅凭名称判定")
    elif key.startswith("elliott"):
        facts["checks"].append("标准非斜三角推动浪硬约束" if not key.startswith("elliott_correction") else "简化锯齿 ABC 价格顺序")
        facts["warnings"].append("未验证低一级 5-3 子浪；是波浪候选，不是唯一数浪")
    elif event.get("region") or key.endswith(("flag", "pennant")):
        bounds = [line for line in event["lines"] if line["from"].get("label") in {"U","L"}]
        uppers = [line for line in bounds if line["from"].get("label") == "U"]
        lowers = [line for line in bounds if line["from"].get("label") == "L"]
        if len(uppers) != 1 or len(lowers) != 1:
            return "BOUNDARIES_MISSING", facts
        upper,lower=uppers[0],lowers[0]
        endpoints = [line[endpoint] for line in (upper,lower) for endpoint in ("from","to")]
        if any(not math.isfinite(point["price"]) for point in endpoints):
            return "BOUNDARY_GEOMETRY", facts
        timed = ["time" in point for point in endpoints]
        if any(timed):
            if not all(timed) or any(not math.isfinite(point["time"]) for point in endpoints):
                return "BOUNDARY_GEOMETRY", facts
            if (any(line["to"]["time"] <= line["from"]["time"] for line in (upper,lower))
                    or max(upper["from"]["time"],lower["from"]["time"]) >= min(upper["to"]["time"],lower["to"]["time"])
                    or any(point["time"] > candles[-1]["ts"] for point in endpoints)):
                return "BOUNDARY_GEOMETRY", facts
            # Compare both lines at the SAME times, including their projection
            # to the last observed candle used for the subsequent trigger.
            start = max(upper["from"]["time"],lower["from"]["time"])
            end = candles[-1]["ts"]
            def at(line, ts):
                a,b=line["from"],line["to"]
                return a["price"]+(b["price"]-a["price"])*(ts-a["time"])/(b["time"]-a["time"])
            upper_start,lower_start = at(upper,start),at(lower,start)
            upper_end,lower_end = at(upper,end),at(lower,end)
            facts["checks"].append("同一观察区间的边界时序")
        else:
            # Retain the lightweight, timestamp-free diagnostic caller contract,
            # but disclose that this is only an endpoint price comparison. The
            # production refine() path requires times for all line coordinates.
            upper_start,lower_start = upper["from"]["price"],lower["from"]["price"]
            upper_end,lower_end = upper["to"]["price"],lower["to"]["price"]
            facts["warnings"].append("未提供边界时间：仅校验端点价格，未验证时序")
        upper_move,lower_move = upper_end-upper_start,lower_end-lower_start
        width,end_width = upper_start-lower_start,upper_end-lower_end
        if min(width,end_width) <= 0:
            return "BOUNDARIES_CROSSED", facts
        facts["checks"].append("上下边界唯一且不交叉" if all(timed) else "上下端点边界唯一且未交叉")
        if event.get("region"):
            flat=.2*volatility
            sloped=.35*volatility
            valid={
                "ascending_triangle":abs(upper_move)<=flat and lower_move>sloped and end_width<.8*width,
                "descending_triangle":upper_move<-sloped and abs(lower_move)<=flat and end_width<.8*width,
                "symmetrical_triangle":upper_move<-sloped and lower_move>sloped and end_width<.78*width,
                "rectangle":abs(upper_move)<=flat and abs(lower_move)<=flat and abs(end_width/width-1)<.25,
                "rising_wedge":upper_move>sloped and lower_move>1.12*upper_move and end_width<.82*width,
                "falling_wedge":lower_move<-sloped and upper_move<1.12*lower_move and end_width<.82*width,
                "uptrend_channel":upper_move>sloped and lower_move>sloped and abs(upper_move-lower_move)<=.35*max(abs(upper_move),abs(lower_move)) and abs(end_width/width-1)<.35,
                "downtrend_channel":upper_move<-sloped and lower_move<-sloped and abs(upper_move-lower_move)<=.35*max(abs(upper_move),abs(lower_move)) and abs(end_width/width-1)<.35,
            }
            if not valid.get(key,False):
                return "BOUNDARY_CLASSIFICATION", facts
            facts["checks"].append("ATR 归一化边界斜率与形态分类")
    elif key.endswith("breakout_retest"):
        facts["checks"].extend(["突破前多次测试水平位", "连续两次收盘突破后再回测"])
        facts["warnings"].append("新增研究类型，尚未证明优于其他形态")
    return None, facts


def retest_proposals(candles):
    """Two-sided, causal breakout→retest setup, bounded to recent breakouts.

    The horizontal level uses 32 bars ending BEFORE the breakout; two distinct
    wick tests at least four bars apart. The breakout needs two closes. The
    retest is a later OHLC intersection and close on the new support side.
    Lifecycle still requires a subsequent rebound; never backdate confirmation.
    """
    if len(candles) < 40:
        return []
    from gatescope.quant.structure_detection import atr
    result = []
    for b in range(max(32, len(candles)-18), len(candles)-2):
        prior = candles[b-32:b]
        vol = atr(prior)
        for meta, sign in zip(EXTRA_PATTERNS, (1, -1), strict=True):
            field = "high" if sign>0 else "low"
            boundary = max(prior, key=lambda row: sign*row[field])[field]
            touches = [row for row in prior if abs(row[field]-boundary)<=.35*vol]
            if len(touches)<2 or touches[-1]["ts"]-touches[0]["ts"]<4*(candles[1]["ts"]-candles[0]["ts"]):
                continue
            if sign*(candles[b-1]["close"]-boundary)>.15*vol:
                continue
            if not all(sign*(row["close"]-boundary)>.15*vol for row in candles[b:b+2]):
                continue
            # A failed breakout cannot become a fresh retest of that same event.
            after = candles[b+2:]
            if any(sign*(row["close"]-boundary)<-.5*vol for row in after):
                continue
            retest = next((row for row in after if row["low"]<=boundary+.35*vol and row["high"]>=boundary-.35*vol
                           and sign*(row["close"]-boundary)>=0), None)
            if not retest or retest["ts"] != candles[-1]["ts"]:
                continue
            points = [{"time":row["ts"],"price":row[field],"label":"S"} for row in (touches[0], touches[-1])]
            points += [{"time":candles[b+1]["ts"],"price":candles[b+1]["close"],"label":"B"},
                       {"time":retest["ts"],"price":retest["low" if sign>0 else "high"],"label":"R"}]
            result.append({**meta,"pattern":meta["id"],"confidence":.5+.25*min(1,len(touches)/4),
                "phase":"FORMED","confirmed":False,"signal_direction":meta["direction"],
                "start_time":points[0]["time"],"end_time":retest["ts"],"points":points,
                "lines":[{"from":a,"to":b,"style":"solid"} for a,b in zip(points,points[1:],strict=False)],
                "retest_level":boundary,"retest_atr":vol,"note":"水平位突破后首次回测；等待后续收盘确认反弹"})
    return result
