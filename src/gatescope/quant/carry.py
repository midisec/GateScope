"""Fully funded long-spot/short-perpetual research simulator, never an order port."""

import bisect
from datetime import datetime, timezone

from gatescope.quant.backtest import metrics
from gatescope.quant.performance import daily_performance, performance_screen

STEP = 28800000
DAY = 86400000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def carry_backtest(data, *, family, start, end, cost_multiplier=1):
    if family not in ("calendar", "payback", "basis"):
        raise ValueError("Unsupported carry family")
    spot, perp, mark, funding = (data[k]["rows"] for k in ("spot", "perp", "mark", "funding"))
    times = [int(x[0]) for x in spot]
    if (
        times != [int(x[0]) for x in perp]
        or times != [int(x[0]) for x in mark]
        or any(b - a != STEP for a, b in zip(times, times[1:], strict=False))
    ):
        raise ValueError("Paired prices must be continuous, synchronous, from one venue")
    begin, finish = bisect.bisect_left(times, start), bisect.bisect_left(times, end)
    if begin < 63 or finish - begin < 100:
        raise ValueError("Insufficient carry history or warmup")
    ft = [int(x["fundingTime"]) for x in funding]
    if ft != sorted(set(ft)):
        raise ValueError("Funding settlements must be unique and ordered")
    cash, cooldown = 10000.0, 0
    position: dict | None = None
    fees_spot, fees_perp, slip = 0.001 * cost_multiplier, 0.0005 * cost_multiplier, 0.0003 * cost_multiplier
    holding_days = 14 if family == "basis" else 42
    curve = [dict(time=iso(start), equity=cash, initial=True)]
    trades = []
    proxies = 0
    margin_failure = False

    def close(s, p, t, reason):
        nonlocal cash, position, cooldown
        assert position is not None
        q = position["quantity"]
        s_exit, p_exit = s * (1 - slip), p * (1 + slip)
        gross = q * (s_exit - position["spot_entry"]) + q * (position["perp_entry"] - p_exit)
        exit_fee = q * (s_exit * fees_spot + p_exit * fees_perp)
        net = gross + position["funding_income"] - position["entry_fee"] - exit_fee
        cash += gross + position["funding_income"] - exit_fee
        trades.append(
            {
                **position,
                "exit_time": iso(t),
                "exit_reason": reason,
                "spot_exit": s_exit,
                "perp_exit": p_exit,
                "net_pnl": net,
                "net_return": net / position["entry_nav"],
                "fees": position["entry_fee"] + exit_fee,
                "funding": -position["funding_income"],
            }
        )
        position = None
        cooldown = t + DAY

    for i in range(begin, finish):
        ts = times[i]
        s, p, m = spot[i], perp[i], mark[i]
        s_open, p_open, s_close, p_close = map(float, (s[1], p[1], s[4], p[4]))
        # Timed unwind at next open precedes that timestamp's funding settlement.
        if position is not None and ts - position["entry_ms"] >= holding_days * DAY:
            close(s_open, p_open, ts, "SCHEDULED_UNWIND")
        if position is None and ts >= cooldown and i < finish - 1:
            left, right = bisect.bisect_left(ft, ts - 21 * DAY), bisect.bisect_left(ft, ts)
            known = funding[left:right]
            daily_rate = sum(float(x["fundingRate"]) for x in known) / 21
            previous_basis = float(perp[i - 1][4]) / float(spot[i - 1][4]) - 1
            eligible = len(known) >= 40 and daily_rate > 0
            if family == "payback":
                eligible = eligible and daily_rate * holding_days > 0.0042 * 1.5
            if family == "basis":
                eligible = eligible and previous_basis > 0.001
            if eligible:
                # 20% cash spot and 80% available cash collateral; no borrowed
                # spot, no leverage increase to manufacture annual returns.
                q = cash * 0.20 / s_open
                s_entry, p_entry = s_open * (1 + slip), p_open * (1 - slip)
                entry_fee = q * (s_entry * fees_spot + p_entry * fees_perp)
                position = dict(
                    entry_time=iso(ts),
                    entry_ms=ts,
                    entry_nav=cash,
                    quantity=q,
                    spot_entry=s_entry,
                    perp_entry=p_entry,
                    entry_fee=entry_fee,
                    funding_income=0.0,
                    funding_count=0,
                    known_daily_funding=daily_rate,
                    known_basis=previous_basis,
                    cash_collateral=cash - q * s_entry - entry_fee,
                )
                cash -= entry_fee
        if position is not None:
            for index in range(bisect.bisect_left(ft, ts), bisect.bisect_left(ft, ts + STEP)):
                event = funding[index]
                # Skip the opening settlement interval even for millisecond
                # timestamps: do not pretend an at-settlement fill earns funding.
                if int(event["fundingTime"]) < position["entry_ms"] + STEP:
                    continue
                settlement_mark = float(event.get("markPrice") or 0)
                if settlement_mark <= 0:
                    settlement_mark = float(m[1])
                    proxies += 1
                position["funding_income"] += (
                    position["quantity"] * settlement_mark * float(event["fundingRate"])
                )
                position["funding_count"] += 1
            # Worst observed mark within this candle must not exhaust cash
            # collateral; no assumed automatic transfer of spot appreciation.
            margin_need = position["quantity"] * (float(m[2]) - position["perp_entry"]) + 0.01 * position[
                "quantity"
            ] * float(m[2])
            if margin_need > position["cash_collateral"] + min(0, position["funding_income"]):
                margin_failure = True
            q = position["quantity"]
            equity = (
                cash
                + q * (s_close - position["spot_entry"])
                + q * (position["perp_entry"] - p_close)
                + position["funding_income"]
                - q * (s_close * fees_spot + p_close * fees_perp)
            )
            if i == finish - 1:
                close(s_close, p_close, ts + STEP, "SEGMENT_END")
                equity = cash
        else:
            equity = cash
        curve.append(dict(time=iso(ts + STEP), equity=equity))
    result = dict(
        metrics={**metrics(trades, curve, 10000.0), **daily_performance(curve)},
        trades=trades,
        equity_curve=curve,
        family=family,
        holding_days=holding_days,
        cost_multiplier=cost_multiplier,
        venue="BINANCE",
        fees=dict(spot_bps=fees_spot * 10000, perp_bps=fees_perp * 10000, slippage_bps=slip * 10000),
        funding_mark_proxy_count=proxies,
        margin_failure=margin_failure,
        notional_fraction=0.20,
        assumptions=[
            "public historical price revisions, not received-at-time replay",
            "funding history absent mark uses settlement 8h mark OPEN proxy",
            "no borrow; 80% cash collateral, 20% spot, equal base-unit short",
            "8h close MTM does not bound intrabar basis drawdown",
            "transaction costs are assumptions, not account-specific verified fees",
            "no exchange default, ADL, stablecoin depeg, custody loss or outage fill model",
        ],
    )
    result["screening"] = performance_screen(result["metrics"])
    result["screening"].update(
        validation_scope="FULL_SAMPLE_RESEARCH",
        basis="全样本资金费率研究；尚无独立留出样本验证；四项绩效仅供观察；不授予实盘资格",
    )
    if margin_failure:
        result["screening"].update(status="REJECTED", shortlisted=False)
        result["screening"]["reason_codes"].append("COLLATERAL_EXHAUSTION")
    return result
