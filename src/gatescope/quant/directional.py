"""Causal, single-position indicator/pattern/wave research. No execution ports.

Fractals become available only after three right-hand candles have CLOSED.
Wave 3/5 are hypotheses about an unfinished wave, never hindsight relabeling.
"""

from dataclasses import asdict, dataclass
from itertools import product
import bisect
import hashlib

from gatescope.indicators.builtins import EMAValue, RSI
from gatescope.quant.backtest import StrategySpec, features, metrics
from gatescope.quant.performance import daily_performance, performance_screen

VERSION = "directional-structures-1.0.0"
FAMILIES = {
    "rsi2_trend": ("RSI2 趋势回踩", "指标"),
    "macd_cross": ("MACD 零轴动量交叉", "指标"),
    "boll_reentry": ("布林回归与 RSI 过滤", "指标"),
    "squeeze_breakout": ("波动收缩后通道突破", "指标"),
    "engulf_pullback": ("趋势回踩吞没", "形态"),
    "double_break": ("双底 / 双顶颈线突破", "形态"),
    "head_shoulders": ("头肩反转颈线突破", "形态"),
    "triangle_break": ("收敛三角突破", "形态"),
    "flag_break": ("旗形整理后延续", "形态"),
    "wave3": ("二浪回撤后的三浪候选", "波浪"),
    "wave5": ("四浪回撤后的五浪候选", "波浪"),
    "abc_turn": ("ABC 修正后的反转", "波浪"),
}


@dataclass(frozen=True)
class Rule:
    family: str
    direction: str
    regime: str
    exit_style: str

    def payload(self):
        return asdict(self)


def rules():
    return [
        Rule(*v)
        for v in product(FAMILIES, ("LONG", "BOTH"), ("ANY", "TREND"), ("QUICK", "SWING", "MEASURED"))
    ]


def append_pivot(pivots, bars, index, order=3):
    center = index - order
    if center < order:
        return
    window = bars[center - order : index + 1]
    h, pivot_low = float(bars[center].high), float(bars[center].low)
    high = all(h > float(b.high) for j, b in enumerate(window) if j != order)
    low = all(pivot_low < float(b.low) for j, b in enumerate(window) if j != order)
    if high == low:  # An outside candle has unknown high/low ordering: abstain.
        return
    pivot = dict(index=center, kind=1 if high else -1, price=h if high else pivot_low, known_index=index)
    if pivots and pivots[-1]["kind"] == pivot["kind"]:
        if pivot["kind"] * (pivot["price"] - pivots[-1]["price"]) > 0:
            pivots[-1] = pivot
    else:
        pivots.append(pivot)
    if len(pivots) > 10:
        del pivots[:-10]


def geometry(pivots, close, atr, index):
    """Return currently observable geometries with immutable anchor snapshots."""
    found: list[dict] = []
    if not pivots or index - pivots[-1]["index"] > 20:
        return found

    def emit(family, side, points, stop, target):
        if side * (close - stop) <= 0.25 * atr or side * (target - close) <= 0.25 * atr:
            return
        found.append(
            dict(
                family=family,
                side=side,
                stop=stop,
                projected_target=target,
                anchors=[dict(p) for p in points],
                known_index=index,
                evidence_id=hashlib.sha256(
                    (family + ":" + ":".join(str(p["index"]) for p in points)).encode()
                ).hexdigest(),
            )
        )

    for side in (1, -1):
        if len(pivots) >= 3:
            ps = pivots[-3:]
            a, b, c = [p["price"] * side for p in ps]
            if [p["kind"] * side for p in ps] == [-1, 1, -1] and 6 <= ps[-1]["index"] - ps[0]["index"] <= 120:
                length = b - a
                retracement = (b - c) / length if length > 0 else -1
                if (
                    length >= 3 * atr
                    and a < c < b
                    and 0.382 <= retracement <= 0.786
                    and close * side > c + 0.5 * atr
                ):
                    emit("wave3", side, ps, (c - 0.25 * atr) * side, (c + 1.618 * length) * side)
                if abs(a - c) <= 0.75 * atr and b - max(a, c) >= 2 * atr and close * side > b + 0.10 * atr:
                    emit("double_break", side, ps, (c - 0.25 * atr) * side, (b + (b - min(a, c))) * side)
        if len(pivots) >= 4:
            ps = pivots[-4:]
            a, b, c, d = [p["price"] * side for p in ps]
            if [p["kind"] * side for p in ps] == [1, -1, 1, -1]:
                length = a - b
                if (
                    length >= 3 * atr
                    and a > c > b > d
                    and 0.382 <= (c - b) / length <= 0.886
                    and 0.618 <= (c - d) / length <= 1.618
                    and close * side > d + 0.5 * atr
                ):
                    emit("abc_turn", side, ps, (d - 0.25 * atr) * side, c * side)
        if len(pivots) >= 5:
            ps = pivots[-5:]
            a, b, c, d, e = [p["price"] * side for p in ps]
            if [p["kind"] * side for p in ps] == [-1, 1, -1, 1, -1]:
                w1, w3 = b - a, d - c
                if (
                    w1 >= 3 * atr
                    and a < c < b < e < d
                    and 0.382 <= (b - c) / w1 <= 0.786
                    and 0.236 <= (d - e) / w3 <= 0.618
                ):
                    target = e + min(w1, 0.618 * w3)
                    if target > d and close * side > e + 0.5 * atr:
                        emit("wave5", side, ps, (e - 0.25 * atr) * side, target * side)
                if c < min(a, e) - 0.75 * atr and abs(a - e) <= atr and abs(b - d) <= atr:
                    slope = (d - b) / (ps[3]["index"] - ps[1]["index"])
                    neck = d + slope * (index - ps[3]["index"])
                    if neck - c >= 2 * atr and close * side > neck + 0.1 * atr:
                        emit("head_shoulders", side, ps, (e - 0.25 * atr) * side, (neck + neck - c) * side)
    if len(pivots) >= 6:
        ps = pivots[-6:]
        highs, lows = ([p for p in ps if p["kind"] == sign] for sign in (1, -1))
        hs = (highs[-1]["price"] - highs[0]["price"]) / (highs[-1]["index"] - highs[0]["index"])
        ls = (lows[-1]["price"] - lows[0]["price"]) / (lows[-1]["index"] - lows[0]["index"])
        top = highs[-1]["price"] + hs * (index - highs[-1]["index"])
        bottom = lows[-1]["price"] + ls * (index - lows[-1]["index"])
        height = highs[0]["price"] - lows[0]["price"]
        regular = all(
            abs(p["price"] - (highs[0]["price"] + hs * (p["index"] - highs[0]["index"]))) < 0.6 * atr
            for p in highs
        )
        regular &= all(
            abs(p["price"] - (lows[0]["price"] + ls * (p["index"] - lows[0]["index"]))) < 0.6 * atr
            for p in lows
        )
        if regular and hs <= 0 <= ls and height > 3 * atr and 0.25 * height < top - bottom < 0.85 * height:
            if close > top + 0.1 * atr:
                emit("triangle_break", 1, ps, bottom - 0.25 * atr, top + height)
            elif close < bottom - 0.1 * atr:
                emit("triangle_break", -1, ps, top + 0.25 * atr, bottom - height)
    return found


def signal_book(bars):
    points = features(bars, StrategySpec(slow_period=200, availability_mode="HISTORICAL_CLOSE"))
    fast, slow, macd_signal, rsi2 = EMAValue(12), EMAValue(26), EMAValue(9), RSI({"period": 2})
    pivots: list[dict] = []
    widths: list[float] = []
    books: dict[str, list] = {family: [] for family in FAMILIES}
    seen: dict[str, set] = {family: set() for family in FAMILIES}
    previous_histogram, previous_rsi2 = None, None
    for i, (bar, point) in enumerate(zip(bars, points, strict=False)):
        close, opened, high, low = map(float, (bar.close, bar.open, bar.high, bar.low))
        f, s = fast.push(close), slow.push(close)
        m = f - s if f is not None and s is not None else None
        ms = macd_signal.push(m) if m is not None else None
        histogram = m - ms if m is not None and ms is not None else None
        r2 = rsi2.update(bar, {})["rsi"]
        append_pivot(pivots, bars, i)
        v = point["values"]
        width = (v["boll.upper"] - v["boll.lower"]) / close if v.get("boll.upper") is not None else 0
        events: list[dict] = []
        if (i >= 201 and point["ready"] and previous_rsi2 is not None and r2 is not None
                and previous_histogram is not None and histogram is not None and m is not None):
            atr, rsi, ema, trend = v["atr.atr"], v["rsi.rsi"], v["fast.ema"], v["slow.ema"]
            prev = bars[i - 1]
            pv = points[i - 1]["values"]
            pc, po = float(prev.close), float(prev.open)
            votes = {}
            votes["rsi2_trend"] = (
                1
                if close > trend and previous_rsi2 < 10 <= r2
                else -1
                if close < trend and previous_rsi2 > 90 >= r2
                else 0
            )
            votes["macd_cross"] = (
                1
                if previous_histogram <= 0 < histogram and m > 0
                else -1
                if previous_histogram >= 0 > histogram and m < 0
                else 0
            )
            votes["boll_reentry"] = (
                1
                if pc < pv["boll.lower"] and close > v["boll.lower"] and rsi < 45
                else -1
                if pc > pv["boll.upper"] and close < v["boll.upper"] and rsi > 55
                else 0
            )
            prior_widths = widths[-120:]
            squeeze = sum(w <= widths[-1] for w in prior_widths) <= len(prior_widths) * 0.25
            votes["squeeze_breakout"] = (
                (1 if close > v["channel.high"] else -1 if close < v["channel.low"] else 0)
                if squeeze and width > widths[-1] and float(bar.volume) > v["volume.prior_mean"] * 1.2
                else 0
            )
            votes["engulf_pullback"] = (
                1
                if pc < po
                and close > opened
                and close >= po
                and opened <= pc
                and low < ema < close
                and rsi < 60
                else -1
                if pc > po
                and close < opened
                and close <= po
                and opened >= pc
                and high > ema > close
                and rsi > 40
                else 0
            )
            for family, side in votes.items():
                if side:
                    events.append(
                        dict(
                            family=family,
                            side=side,
                            stop=None,
                            projected_target=None,
                            known_index=i,
                            anchors=[],
                            evidence_id=f"{family}:{i}",
                        )
                    )
            events.extend(geometry(pivots, close, atr, i))
            # A known pole followed by an 8-bar controlled countertrend flag.
            pole = float(bars[i - 9].close) - float(bars[i - 25].close)
            recent = bars[i - 8 : i]
            ceiling, floor = max(float(b.high) for b in recent), min(float(b.low) for b in recent)
            if abs(pole) > 5 * atr and ceiling - floor < abs(pole) * 0.618:
                side = (
                    1
                    if pole > 0 and close > ceiling and floor > float(bars[i - 9].close) - 0.5 * pole
                    else -1
                    if pole < 0 and close < floor and ceiling < float(bars[i - 9].close) - 0.5 * pole
                    else 0
                )
                if side:
                    events.append(
                        dict(
                            family="flag_break",
                            side=side,
                            stop=(floor - 0.25 * atr if side == 1 else ceiling + 0.25 * atr),
                            projected_target=close + side * abs(pole),
                            known_index=i,
                            anchors=[],
                            evidence_id=f"flag:{i - 9}",
                        )
                    )
        by_family: dict[str, dict] = {}
        for event in events:
            family = event["family"]
            if event["evidence_id"] not in seen[family]:
                seen[family].add(event["evidence_id"])
                if family in by_family and by_family[family]["side"] != event["side"]:
                    by_family[family] = {"side": 0}
                else:
                    by_family[family] = event
        for family in FAMILIES:
            chosen = by_family.get(family)
            books[family].append(chosen if chosen and chosen["side"] else None)
        widths.append(width)
        previous_histogram, previous_rsi2 = histogram, r2
    return points, books


def funding_series(bars, settlements):
    stamps = [int(b.open_time.timestamp() * 1000) for b in bars]
    step = int((bars[0].close_time - bars[0].open_time).total_seconds() * 1000)
    result = [0.0] * len(bars)
    for event in settlements:
        t = int(event["fundingTime"])
        i = bisect.bisect_right(stamps, t) - 1
        if i < 0 or t >= stamps[i] + step:
            continue
        mark = float(event.get("markPrice") or bars[i].open)
        result[i] += mark * float(event["fundingRate"])
    return result


def simulate_directional(
    bars,
    points,
    signals,
    rule,
    start,
    end,
    funding,
    *,
    cost=1,
    detailed=False,
    daily_curve=False,
    capture_mtm=False,
):
    if (
        rule.family not in FAMILIES
        or rule.direction not in ("LONG", "BOTH")
        or rule.exit_style not in ("QUICK", "SWING", "MEASURED")
        or rule.regime not in ("ANY", "TREND")
    ):
        raise ValueError("Unsupported fixed research rule")
    if cost not in (1, 2) or not 0 <= start < end <= len(bars):
        raise ValueError("Invalid research window/cost")
    fee, slip = 0.0005 * cost, 0.0005 * cost
    cash, position, pending, peak, mdd = 10000.0, None, None, 10000.0, 0.0
    trades = []
    mtm = []
    curve = [dict(time=bars[start].open_time.isoformat(), equity=cash, initial=True)]
    rejected = 0
    max_hold = 24 if rule.exit_style == "QUICK" else 72
    for i in range(start, end):
        bar = bars[i]
        opened, high, low, closed = map(float, (bar.open, bar.high, bar.low, bar.close))
        if position is None and pending is not None and i < end - 1:
            signal, si = pending
            side, atr = signal["side"], points[si]["values"]["atr.atr"]
            entry = opened * (1 + side * slip)
            stop = (
                signal["stop"]
                if signal["stop"] is not None
                else entry - side * atr * (2 if rule.exit_style == "QUICK" else 3)
            )
            distance = side * (entry - stop)
            target = (
                signal["projected_target"]
                if rule.exit_style == "MEASURED" and signal["projected_target"] is not None
                else entry + side * distance * (0.8 if rule.exit_style == "QUICK" else 1.4)
            )
            if cash > 0 and 0.25 * atr < distance < 6 * atr and side * (target - entry) > 0.25 * distance:
                q = min(cash * 0.005 / distance, cash / (entry * (1 + fee)))
                ef = q * entry * fee
                position = dict(
                    side=side,
                    entry=entry,
                    stop=stop,
                    target=target,
                    quantity=q,
                    entry_fee=ef,
                    funding=0.0,
                    entry_time=bar.open_time.isoformat(),
                    entry_index=i,
                    signal_index=si,
                    signal_time=bars[si].close_time.isoformat(),
                    signal=signal,
                )
                cash -= ef
            else:
                rejected += 1
            pending = None
        if position is not None:
            p = position
            side, q = p["side"], p["quantity"]
            price, reason = None, None
            if side * (opened - p["stop"]) <= 0:
                price, reason = opened, "STOP_GAP"
            elif side * (opened - p["target"]) >= 0:
                price, reason = p["target"], "TARGET"
            elif low <= p["stop"] if side == 1 else high >= p["stop"]:
                price, reason = p["stop"], "STOP"
            elif high >= p["target"] if side == 1 else low <= p["target"]:
                price, reason = p["target"], "TARGET"
            elif i - p["entry_index"] + 1 >= max_hold:
                price, reason = closed, "TIME_EXIT"
            elif i == end - 1:
                price, reason = closed, "SEGMENT_END"
            settlement = side * q * funding[i]
            # Unknown intrabar fill/settlement sequence: charge debit, don't
            # claim credit on entry or exit bars. All other actual fees signed.
            if i == p["entry_index"] or price is not None:
                settlement = max(0.0, settlement)
            p["funding"] += settlement
            if price is not None:
                exit_price = price * (1 - side * slip)
                exit_fee = q * exit_price * fee
                gross = side * q * (exit_price - p["entry"])
                net = gross - p["entry_fee"] - exit_fee - p["funding"]
                cash += gross - exit_fee - p["funding"]
                trade = {k: v for k, v in p.items() if detailed or k not in ("signal",)}
                trade.update(
                    exit_time=bar.close_time.isoformat(),
                    exit_bar_open_time=bar.open_time.isoformat(),
                    exit_time_basis="INTRABAR_OHLC"
                    if reason in ("STOP", "STOP_GAP", "TARGET")
                    else "BAR_CLOSE",
                    exit_price=exit_price,
                    exit_reason=reason,
                    net_pnl=net,
                    net_return=net / (q * p["entry"]),
                    fees=p["entry_fee"] + exit_fee,
                    holding_bars=i - p["entry_index"] + 1,
                )
                if detailed:
                    trade["indicators"] = points[p["signal_index"]]["values"]
                trades.append(trade)
                position = None
                equity = cash
            else:
                equity = cash + side * q * (closed - p["entry"]) - p["funding"] - q * closed * fee
        else:
            equity = cash
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)
        if capture_mtm:
            mtm.append(equity)
        if (detailed and not daily_curve) or bar.close_time.hour == 0 or i == end - 1:
            curve.append(dict(time=bar.close_time.isoformat(), equity=equity))
        event = signals[i]
        if position is None and event is not None:
            side = event["side"]
            trend = points[i]["values"]["fast.ema"] - points[i]["values"]["slow.ema"]
            if (rule.direction == "BOTH" or side == 1) and (rule.regime == "ANY" or side * trend > 0):
                pending = event, i
    m = {**metrics(trades, curve, 10000.0), **daily_performance(curve), "max_drawdown": mdd}
    screening = performance_screen(m)
    screening.update(
        validation_scope="CALLER_DEFINED_RESEARCH_SEGMENT",
        basis="因果单仓位方向研究；计入双边成本与历史资金费；绩效仅供观察，不按四项指标淘汰；并非实盘认证",
    )
    result = dict(
        metrics=m,
        trades=trades,
        equity_curve=curve,
        screening=screening,
        risk_rejections=rejected,
        rule=rule.payload(),
        engine_version=VERSION,
        cost_multiplier=cost,
        hedged=False,
        deployable=False,
    )
    if capture_mtm:
        result["_mtm"] = mtm
    return result
