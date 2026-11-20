"""Shared frozen-plan simulator for replay and notification shadow observations.

No exchange client, order port or admission decision exists in this module.
Returns and costs are fractions of initial notional, not account-currency PnL.
An OHLC bar is consumed only after its close; intrabar ordering is not invented.
"""
from __future__ import annotations

import copy
import math
from collections import Counter
from statistics import NormalDist, median
from typing import Any, Iterable, Mapping, Sequence

from gatescope.application.pattern_trade_notice import build_pattern_trade_notice
from gatescope.quant.artifacts import report_digest
from gatescope.quant.structure_detection import VERSION as DETECTOR_VERSION, value_at
from gatescope.quant.structure_lifecycle import resolve_rule

VERSION = "structure-trade-plan-1"
CONTEXT_VERSION = "causal-market-context-1"
TERMINAL_STATES = frozenset({"TARGET", "STOP", "TIMEOUT", "MISSED", "UNAVAILABLE", "CENSORED"})
SETTLED_STATES = frozenset({"TARGET", "STOP", "TIMEOUT"})
DEFAULT_POLICY = {
    "fee_bps": 5.0, "slippage_bps": 5.0, "funding_bps_per_8h": 1.0,
    "entry_delay_seconds": 0, "max_holding_bars": 48, "max_entry_wait_bars": 2,
    "timeframe_seconds": 3600, "stop_mode": "PRICE_TOUCH",
}


def _finite(value: Any, *, positive: bool = False) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and (not positive or value > 0)


def normalize_policy(policy: Mapping | None = None) -> dict:
    result = {**DEFAULT_POLICY, **(dict(policy) if policy else {})}
    for key in ("fee_bps", "slippage_bps", "funding_bps_per_8h"):
        if not _finite(result[key]) or not 0 <= result[key] <= 100:
            raise ValueError("STRUCTURE_COST_POLICY_INVALID")
        result[key] = float(result[key])
    for key, maximum in (("entry_delay_seconds", 31 * 86400), ("max_holding_bars", 10000),
                         ("max_entry_wait_bars", 1000), ("timeframe_seconds", 31 * 86400)):
        value = result[key]
        if not isinstance(value, int) or isinstance(value, bool) or not (0 if key == "entry_delay_seconds" else 1) <= value <= maximum:
            raise ValueError("STRUCTURE_TIME_POLICY_INVALID")
    if result["stop_mode"] not in ("PRICE_TOUCH", "STRUCTURE_INVALIDATION"):
        raise ValueError("STRUCTURE_STOP_POLICY_INVALID")
    result.update(version=VERSION, funding_basis="CONSERVATIVE_RATE_MODEL_NOT_REALIZED_FUNDING",
                  cost_units="FRACTION_OF_INITIAL_NOTIONAL", ambiguity_policy="STOP_FIRST",
                  auto_order=False)
    return result


def _ema(values: Sequence[float], period: int) -> list[float]:
    result: list[float] = []
    for value in values:
        result.append(value if not result else result[-1] + 2 / (period + 1) * (value - result[-1]))
    return result


def _trend(bars: Sequence[dict]) -> tuple[str, float | None]:
    if len(bars) < 50:
        return "UNKNOWN", None
    closes = [float(b["close"]) for b in bars]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
              for a, b in zip(bars[-15:-1], bars[-14:], strict=True)]
    atr = sum(ranges) / len(ranges)
    fast, slow = _ema(closes, 20), _ema(closes, 50)
    direction = "UP" if fast[-1] - slow[-1] > .25 * atr and fast[-1] > fast[-6] else (
        "DOWN" if slow[-1] - fast[-1] > .25 * atr and fast[-1] < fast[-6] else "RANGE")
    return direction, atr


def market_context(bars: Sequence[dict]) -> dict:
    """A fixed, causal description, never a post-hoc filter or probability.

    Higher-timeframe information uses four COMPLETE aligned source candles.
    The unfinished higher candle is not admitted, even if its close looks useful.
    """
    rows = list(bars[-240:])
    empty = {"version": CONTEXT_VERSION, "trend": "UNKNOWN", "volatility": "UNKNOWN",
             "volume": "UNKNOWN", "as_of": None, "atr": None, "relative_volume": None,
             "higher_timeframe": {"available": False, "trend": "UNKNOWN", "closed_through": None}}
    if len(rows) < 2:
        return empty
    step = rows[-1].get("step_seconds")
    if step is None:
        intervals = [b["ts"] - a["ts"] for a, b in zip(rows[:-1], rows[1:], strict=True)]
        # A missing final candle must not silently double the source timeframe.
        step = Counter(intervals).most_common(1)[0][0]
    if not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        return empty
    # A gap starts a new warm-up; don't bridge it with apparent trend evidence.
    for i in range(len(rows) - 1, 0, -1):
        if rows[i]["ts"] - rows[i - 1]["ts"] != step:
            rows = rows[i:]
            break
    if any(not all(_finite(b.get(k), positive=k != "volume") for k in ("open", "high", "low", "close", "volume")) or b["volume"] < 0 for b in rows):
        return empty
    trend, atr = _trend(rows)
    raw_atrs = [_trend(rows[:i])[1] for i in range(max(50, len(rows) - 60), len(rows))]
    previous_atrs = [a for a in raw_atrs if a is not None and a > 0]
    volatility_ratio = atr / median(previous_atrs) if atr and previous_atrs else None
    volatility = "UNKNOWN" if volatility_ratio is None else "HIGH" if volatility_ratio >= 1.5 else "LOW" if volatility_ratio <= 2 / 3 else "NORMAL"
    previous_volume = [float(b["volume"]) for b in rows[-21:-1]]
    volume_mean = sum(previous_volume) / len(previous_volume) if len(previous_volume) == 20 else 0
    ratio = float(rows[-1]["volume"]) / volume_mean if volume_mean > 0 else None
    volume = "UNKNOWN" if ratio is None else "HIGH" if ratio >= 1.5 else "LOW" if ratio < .7 else "NORMAL"
    groups: dict[int, list[dict]] = {}
    for bar in rows:
        groups.setdefault(bar["ts"] // (4 * step) * (4 * step), []).append(bar)
    higher = []
    for ts, group in sorted(groups.items()):
        if len(group) == 4 and [b["ts"] for b in group] == [ts + i * step for i in range(4)]:
            higher.append({"ts": ts, "open": group[0]["open"], "high": max(b["high"] for b in group),
                           "low": min(b["low"] for b in group), "close": group[-1]["close"],
                           "volume": sum(b["volume"] for b in group)})
    higher_trend, _ = _trend(higher)
    return {**empty, "trend": trend, "volatility": volatility, "volume": volume,
            "as_of": rows[-1]["ts"] + step, "atr": atr, "atr_ratio": volatility_ratio,
            "relative_volume": ratio, "source_bars": len(rows),
            "higher_timeframe": {"available": higher_trend != "UNKNOWN", "trend": higher_trend,
                                 "timeframe_seconds": 4 * step,
                                 "closed_through": higher[-1]["ts"] + 4 * step if higher else None}}


def plan_from_notice(notice: Mapping, policy: Mapping | None = None, event: Mapping | None = None) -> dict:
    """Freeze the same references shown in TG; don't retrieve a later target."""
    policy = normalize_policy(policy)
    event = dict(event or {})
    notice = copy.deepcopy(dict(notice))
    side = {"bullish": 1, "bearish": -1}.get(str(notice.get("direction")), 0)
    stop: Any = (notice.get("stop") or {}).get("price")
    target: Any = (notice.get("target") or {}).get("price")
    reference: Any = (notice.get("entry") or {}).get("price")
    issued = notice.get("observed_at")
    reasons = list(notice.get("reason_codes") or [])
    if not side or not all(_finite(v, positive=True) for v in (stop, target, reference)):
        reasons.append("FROZEN_PLAN_INCOMPLETE")
    elif side * (reference - stop) <= 0 or side * (target - reference) <= 0:
        reasons.append("FROZEN_PLAN_DIRECTION_INVALID")
    if not isinstance(issued, int) or isinstance(issued, bool) or not 0 <= issued <= 253402300799:
        reasons.append("FROZEN_PLAN_TIME_INVALID")
        issued = 0
    phase = notice.get("phase")
    if phase not in ("FORMED", "CONFIRMED"):
        reasons.append("PHASE_NOT_ACTIONABLE")
    if phase == "FORMED" and not event.get("levels"):
        reasons.append("ORIGINAL_CONFIRMATION_RULE_UNAVAILABLE")
    raw_deadline = event.get("deadline_time")
    deadline = raw_deadline if isinstance(raw_deadline, int) and not isinstance(raw_deadline, bool) else issued + policy["max_holding_bars"] * policy["timeframe_seconds"]
    score = notice.get("geometry_score")
    context = copy.deepcopy(event.get("market_context") or {})
    context["score_bucket"] = "UNSCORED" if score is None else "HIGH" if score >= .85 else "MID" if score >= .7 else "LOW"
    higher = (context.get("higher_timeframe") or {}).get("trend", "UNKNOWN")
    context["higher_alignment"] = "UNKNOWN" if higher == "UNKNOWN" else "RANGE" if higher == "RANGE" else (
        "ALIGNED" if (higher == "UP") == (side == 1) else "OPPOSED")
    plan = {"version": VERSION, "detector_version": (notice.get("evidence") or {}).get("detector_version") or event.get("detector_version") or DETECTOR_VERSION,
            "candidate_id": (notice.get("evidence") or {}).get("candidate_id") or event.get("id"),
            "evidence_id": (notice.get("evidence") or {}).get("evidence_id") or event.get("evidence_id"),
            "pattern": notice.get("pattern"), "name": notice.get("name"), "phase": phase,
            "side": side, "signal_time": issued, "deadline_time": deadline,
            "reference_price": reference, "stop_price": stop, "target_price": target,
            "notice": notice, "event": copy.deepcopy(event), "policy": policy, "context": context,
            "status": "UNAVAILABLE" if reasons else "WAIT_CONFIRMATION" if phase == "FORMED" else "READY",
            "reason_codes": list(dict.fromkeys(reasons)), "auto_order": False,
            "assumptions": ["OHLC_SIMULATION_NOT_ACTUAL_FILL", "FUNDING_RATE_MODEL",
                            "PRICE_TOUCH_STOP_NOT_MODEL_CLOSE_INVALIDATION" if policy["stop_mode"] == "PRICE_TOUCH" else "MODEL_INVALIDATION_STOP",
                            "SAME_BAR_STOP_FIRST_UNLESS_OPEN_OR_CLOSE_ORDER_KNOWN", "NORMALIZED_NOTIONAL_NOT_PORTFOLIO_PNL"]}
    plan["digest"] = report_digest(plan)
    return plan


def make_plan(event: Mapping, bar: Mapping, policy: Mapping | None = None) -> dict:
    return plan_from_notice(build_pattern_trade_notice(event, bar), policy, event)


def start_case(plan: Mapping, available_at: int | None = None, source: str = "REPLAY") -> dict:
    plan = copy.deepcopy(dict(plan))
    signal_time = plan["signal_time"]
    available = max(signal_time, available_at if available_at is not None else signal_time)
    state = "UNAVAILABLE" if plan["status"] == "UNAVAILABLE" else "WAITING_CONFIRMATION" if plan["phase"] == "FORMED" else "WAITING_ENTRY"
    eligible = available + plan["policy"]["entry_delay_seconds"]
    reason = ";".join(plan["reason_codes"]) if state == "UNAVAILABLE" else None
    if state != "UNAVAILABLE" and eligible >= plan["deadline_time"]:
        state, reason = "MISSED", "SIGNAL_AVAILABLE_AFTER_DEADLINE"
    return {"version": VERSION, "state": state, "reason": reason,
            "source": source, "plan": plan, "available_at": available, "eligible_at": eligible,
            "entry_time": None, "entry_price": None, "raw_entry_price": None,
            "exit_time": None, "exit_price": None, "raw_exit_price": None,
            "gross_return": None, "raw_gross_return": None, "slippage_return": None, "costs_return": None,
            "net_return": None, "net_r": None, "fees": None, "funding": None,
            "holding_seconds": 0, "mfe": 0.0, "mae": 0.0, "ambiguous": False,
            "last_bar_time": None, "execution_step_seconds": None, "confirm_streak": 0,
            "observation_count": 0, "outcome_time": available if state in TERMINAL_STATES else None,
            "excursion_basis": "FULL_BAR_ENVELOPE_NOT_INTRABAR_PATH", "auto_order": False}


def _finish_without_trade(case: dict, state: str, reason: str, timestamp: int) -> dict:
    case.update(state=state, reason=reason, outcome_time=timestamp)
    return case


def _close(case: dict, state: str, reason: str, raw_exit: float, timestamp: int) -> dict:
    plan, entry = case["plan"], case["entry_price"]
    policy, side = plan["policy"], plan["side"]
    exit_price = raw_exit * (1 - side * policy["slippage_bps"] / 10000)
    held = max(0, timestamp - case["entry_time"])
    funding = policy["funding_bps_per_8h"] / 10000 * held / (8 * 3600)
    fees = policy["fee_bps"] / 10000 * (1 + exit_price / entry)
    gross = side * (exit_price - entry) / entry
    raw_gross = side * (raw_exit - case["raw_entry_price"]) / case["raw_entry_price"]
    net = gross - fees - funding
    risk = abs(entry - plan["stop_price"]) / entry
    case.update(state=state, reason=reason, exit_time=timestamp, outcome_time=timestamp,
                exit_price=exit_price, raw_exit_price=raw_exit, holding_seconds=held,
                gross_return=gross, raw_gross_return=raw_gross, slippage_return=raw_gross - gross,
                costs_return=raw_gross - net, net_return=net, net_r=net / risk if risk else None,
                fees=fees, funding=funding)
    return case


def advance_case(case: Mapping, bar: Mapping) -> dict:
    """Advance an immutable plan using a new CLOSED execution candle.

    Repeated bars are no-ops. A gap after observation starts censors the case;
    absence of data must never turn into an imaginary stop, target or fill.
    """
    result = copy.deepcopy(dict(case))
    if result["state"] in TERMINAL_STATES:
        return result
    plan, policy = result["plan"], result["plan"]["policy"]
    ts, step = bar.get("ts"), bar.get("step_seconds", policy["timeframe_seconds"])
    if not isinstance(ts, int) or isinstance(ts, bool) or not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        raise ValueError("STRUCTURE_EXECUTION_BAR_INVALID")
    end = ts + step
    prices: list[Any] = [bar.get(k) for k in ("open", "high", "low", "close")]
    if not all(_finite(p, positive=True) for p in prices):
        raise ValueError("STRUCTURE_EXECUTION_BAR_INVALID")
    opened, high, low, closed = map(float, prices)
    if low > min(opened, closed) or high < max(opened, closed) or high < low:
        raise ValueError("STRUCTURE_EXECUTION_BAR_INVALID")
    if ts < plan["signal_time"] or (result["last_bar_time"] is not None and ts <= result["last_bar_time"]):
        return result
    if result["execution_step_seconds"] is not None and step != result["execution_step_seconds"]:
        return _finish_without_trade(result, "CENSORED", "EXECUTION_RESOLUTION_CHANGED", end)
    if (policy["stop_mode"] == "STRUCTURE_INVALIDATION" and step != policy["timeframe_seconds"]
            and (plan["notice"].get("stop") or {}).get("mode") != "WICK_INVALIDATION"):
        return _finish_without_trade(result, "CENSORED", "ORIGINAL_CLOSE_TIMEFRAME_REQUIRED", end)
    if result["last_bar_time"] is not None and ts != result["last_bar_time"] + step:
        return _finish_without_trade(result, "CENSORED", "EXECUTION_DATA_GAP", end)
    result.update(last_bar_time=ts, execution_step_seconds=step)
    side, stop, target = plan["side"], plan["stop_price"], plan["target_price"]
    if result["state"] == "WAITING_CONFIRMATION":
        if step != policy["timeframe_seconds"]:
            return _finish_without_trade(result, "CENSORED", "WRONG_CONFIRMATION_TIMEFRAME", end)
        if ts < result["available_at"]:
            return result
        if end > plan["deadline_time"]:
            return _finish_without_trade(result, "MISSED", "CONFIRMATION_DEADLINE", end)
        record = {"latest": copy.deepcopy(plan["event"]), "phase": "FORMED", "stages": {"FORMED": {"index": -1}},
                  "deadline_index": max(0, (plan["deadline_time"] - plan["signal_time"]) // step - 1),
                  "confirm_streak": result["confirm_streak"], "confirm_side": result.get("confirm_side", 0)}
        try:
            decision = resolve_rule(record, dict(bar), result["observation_count"])
        except (KeyError, TypeError, ZeroDivisionError, ValueError):
            return _finish_without_trade(result, "UNAVAILABLE", "ORIGINAL_CONFIRMATION_RULE_UNAVAILABLE", end)
        result.update(confirm_streak=record.get("confirm_streak", 0), confirm_side=record.get("confirm_side", 0),
                      observation_count=result["observation_count"] + 1)
        if decision and decision[0] == "CONFIRMED":
            result.update(state="WAITING_ENTRY", confirmed_at=end,
                          eligible_at=max(end, result["available_at"]) + policy["entry_delay_seconds"])
            if result["eligible_at"] >= plan["deadline_time"]:
                return _finish_without_trade(result, "MISSED", "ENTRY_WINDOW_EXPIRED", end)
        elif decision:
            return _finish_without_trade(result, "MISSED", "PRE_ENTRY_" + decision[1], end)
        return result  # Never fill at the open of the candle that just confirmed.
    if result["state"] == "WAITING_ENTRY":
        if ts < result["eligible_at"]:
            return result
        expiry = min(plan["deadline_time"], result["eligible_at"] + policy["max_entry_wait_bars"] * policy["timeframe_seconds"])
        if ts >= expiry:
            return _finish_without_trade(result, "MISSED", "ENTRY_WINDOW_EXPIRED", end)
        entry = opened * (1 + side * policy["slippage_bps"] / 10000)
        if side * (entry - stop) <= 0 or side * (target - entry) <= 0:
            return _finish_without_trade(result, "MISSED", "GAP_BEYOND_FROZEN_PLAN", end)
        result.update(state="OPEN", entry_time=ts, entry_price=entry, raw_entry_price=opened,
                      holding_deadline=ts + policy["max_holding_bars"] * policy["timeframe_seconds"])
    entry = result["entry_price"]
    touch_stop = policy["stop_mode"] == "PRICE_TOUCH" or (plan["notice"].get("stop") or {}).get("mode") == "WICK_INVALIDATION"
    active_stop = stop
    if policy["stop_mode"] == "STRUCTURE_INVALIDATION":
        levels = plan["event"].get("levels", {})
        if "invalid_edge" in levels:
            active_stop = value_at(levels["invalid_edge"], ts) + levels["invalid_side"] * (0 if levels.get("hard") else levels["buffer"])
    stop_hit = (low <= active_stop if side > 0 else high >= active_stop) if touch_stop else side * (closed - active_stop) <= 0
    target_hit = high >= target if side > 0 else low <= target
    # Open is known to precede the intrabar envelope. A position held across a
    # gap exits adversely; a favorable target gap never receives free slippage.
    open_move = side * (opened - entry) / entry
    result["mfe"], result["mae"] = max(result["mfe"], open_move), min(result["mae"], open_move)
    if touch_stop and side * (opened - active_stop) <= 0:
        return _close(result, "STOP", "STOP_GAP", opened, ts)
    if side * (opened - target) >= 0:
        return _close(result, "TARGET", "TARGET_GAP_CONSERVATIVE", target, ts)
    result["mfe"] = max(result["mfe"], side * ((high if side > 0 else low) - entry) / entry)
    result["mae"] = min(result["mae"], side * ((low if side > 0 else high) - entry) / entry)
    result["holding_seconds"] = max(0, end - result["entry_time"])
    # A touch target is known to precede a later adverse CLOSE invalidation.
    # STOP_FIRST is needed for two intrabar touches, not for known ordering.
    if target_hit and not touch_stop:
        return _close(result, "TARGET", "TARGET_BEFORE_CLOSE_INVALIDATION" if stop_hit else "TARGET_TOUCH", target, end)
    if stop_hit:
        result["ambiguous"] = bool(target_hit)
        return _close(result, "STOP", "SAME_BAR_STOP_FIRST" if target_hit else "STOP_TOUCH" if touch_stop else "STRUCTURE_CLOSE_INVALIDATION",
                      active_stop if touch_stop else closed, end)
    if target_hit:
        return _close(result, "TARGET", "TARGET_TOUCH", target, end)
    if end >= result["holding_deadline"]:
        return _close(result, "TIMEOUT", "HOLDING_DEADLINE", closed, end)
    return result


def _interval(wins: int, count: int, alpha: float = .05) -> dict | None:
    if count < 20:
        return None
    p, z = wins / count, NormalDist().inv_cdf(1 - alpha / 2)
    den = 1 + z * z / count
    center = (p + z * z / (2 * count)) / den
    half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / den
    return {"lower": max(0., center - half), "upper": min(1., center + half)}


def summarize_cases(cases: Iterable[Mapping]) -> dict:
    """Descriptive, normalized per-case outcomes; never a deployment gate.

    Wilson is a descriptive interval, not proof of independent cases or a
    multiplicity-adjusted forecast. Rolling/block validation remains separate.
    """
    rows = list(cases)
    states = dict(Counter(str(c.get("state", "UNKNOWN")) for c in rows))
    complete = [c for c in rows if c.get("state") in SETTLED_STATES and _finite(c.get("net_return"))]
    n = len(complete)
    wins = [float(c["net_return"]) for c in complete if c["net_return"] > 0]
    losses = [-float(c["net_return"]) for c in complete if c["net_return"] <= 0]
    gains, loss = sum(wins), sum(losses)
    def mean(key: str) -> float | None:
        return sum(float(c.get(key) or 0) for c in complete) / n if n else None
    return {"version": VERSION, "candidate_count": len(rows), "sample_count": n, "wins": len(wins), "losses": len(losses),
            "net_win_rate": len(wins) / n if n else None,
            "target_first_rate": sum(c["state"] == "TARGET" for c in complete) / n if n else None,
            "stop_first_rate": sum(c["state"] == "STOP" for c in complete) / n if n else None,
            "timeout_rate": sum(c["state"] == "TIMEOUT" for c in complete) / n if n else None,
            "mean_net_return": mean("net_return"), "mean_net_r": mean("net_r"),
            "mean_fees": mean("fees"), "mean_funding": mean("funding"),
            "mean_slippage": mean("slippage_return"), "mean_costs": mean("costs_return"),
            "profit_factor": gains / loss if loss > 0 else None,
            "mean_win": gains / len(wins) if wins else None, "mean_loss": loss / len(losses) if losses else None,
            "interval": _interval(len(wins), n), "states": states,
            "ambiguous_count": sum(bool(c.get("ambiguous")) for c in rows), "censored_count": states.get("CENSORED", 0),
            "pending_count": sum(v for k, v in states.items() if k in {"OPEN", "WAITING_ENTRY", "WAITING_CONFIRMATION"}),
            "precision": "INSUFFICIENT" if n < 20 else "DESCRIPTIVE_ONLY",
            "interval_basis": "WILSON_DESCRIPTIVE_NOT_INDEPENDENCE_OR_SELECTION_PROOF",
            "return_units": "FRACTION_OF_INITIAL_NOTIONAL", "portfolio_pnl": False,
            "real_execution": False, "thresholds_enforced": False}


def reprice_case(case: Mapping, multiplier: float = 2.) -> dict:
    """Cost sensitivity with frozen decisions, not a second optimized backtest."""
    result = copy.deepcopy(dict(case))
    if not _finite(multiplier, positive=True) or multiplier > 10:
        raise ValueError("STRUCTURE_COST_STRESS_INVALID")
    if result.get("state") not in SETTLED_STATES:
        return result
    plan, side = result["plan"], result["plan"]["side"]
    policy = plan["policy"]
    slip, fee = policy["slippage_bps"] / 10000 * multiplier, policy["fee_bps"] / 10000 * multiplier
    entry = result["raw_entry_price"] * (1 + side * slip)
    exit_price = result["raw_exit_price"] * (1 - side * slip)
    gross = side * (exit_price - entry) / entry
    raw_gross = side * (result["raw_exit_price"] - result["raw_entry_price"]) / result["raw_entry_price"]
    fees, funding = fee * (1 + exit_price / entry), result["funding"] * multiplier
    net = gross - fees - funding
    # R keeps the original frozen risk budget, so higher entry costs do not
    # artificially make the denominator larger and the stress result look safer.
    risk = abs(result["entry_price"] - plan["stop_price"]) / result["entry_price"]
    result.update(entry_price=entry, exit_price=exit_price, gross_return=gross, raw_gross_return=raw_gross,
                  slippage_return=raw_gross - gross, costs_return=raw_gross - net, fees=fees, funding=funding,
                  net_return=net, net_r=net / risk if risk else None,
                  stress_basis="FROZEN_DECISIONS_COST_SENSITIVITY", stress_multiplier=multiplier)
    return result
