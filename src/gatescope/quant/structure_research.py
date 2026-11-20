"""Causal selection and purged expanding-prefix evaluation for frozen structures."""
from __future__ import annotations

import math
from typing import Any

from gatescope.quant.backtest import StrategySpec

ENGINE_VERSION = "structure-research-1"


def dependence_horizon(spec: StrategySpec, step: int, observed_span_seconds: int = 0) -> dict[str,int | str]:
    """Cover confirmation waiting, entry latency and holding, not just holding.

    A longer actually observed decision-to-exit span may widen the descriptive
    block/purge budget, never shorten it to manufacture a tighter interval.
    """
    from gatescope.quant.pattern_study import WINDOW
    configured = spec.max_holding_bars + spec.entry_delay_bars + (WINDOW if spec.structure_entry_phase == "FORMED" else 2)
    bars = max(configured,math.ceil(max(0,observed_span_seconds)/step))
    days = max(7,math.ceil(bars*step/86400))
    return {"embargo_bars":bars,"block_days":min(1000,days),"required_block_days":days,
        "observed_span_seconds":max(0,observed_span_seconds),
        "basis":"MAX_CONFIRMATION_ENTRY_HOLD_BUDGET_AND_OBSERVED_DECISION_TO_EXIT"}


def selection(event: dict[str, Any], environment: dict[str, Any], spec: StrategySpec) -> dict[str, Any]:
    """Select at observation, never after an outcome; retain excluded cases too."""
    reasons: list[str] = []
    if spec.structure_patterns and event["pattern"] not in spec.structure_patterns:
        reasons.append("PATTERN_NOT_SELECTED")
    direction = event.get("signal_direction", event.get("direction"))
    if (direction == "bullish" and spec.direction == "SHORT") or (direction == "bearish" and spec.direction == "LONG"):
        reasons.append("DIRECTION_NOT_SELECTED")
    baseline = not reasons
    score = event.get("confidence")
    if not isinstance(score,(int,float)) or score < spec.pattern_confidence:
        reasons.append("BELOW_GEOMETRY_FILTER")
    if spec.regimes and environment.get("trend", "UNKNOWN") not in spec.regimes:
        reasons.append("REGIME_NOT_SELECTED")
    return {"selected": not reasons, "baseline": baseline, "selection_reasons": reasons}


def walk_forward_windows(start: int, end: int, step: int, folds: int, embargo_bars: int) -> list[dict[str, int]]:
    """Fixed rules, expanding prefix. No training outcome may cross test start.

    The first 20% is initial training. Each later chronological block is tested
    once; training outcomes must be closed before the purge boundary. An embargo
    of the maximum holding/entry/confirmation observation horizon separates them.
    """
    count = max(0, (end-start)//step)
    initial = max(60, count//5)
    remaining = max(0, count-initial)
    windows = []
    for index in range(folds):
        test_start = start+(initial+remaining*index//folds)*step
        test_end = start+(initial+remaining*(index+1)//folds)*step
        train_end = test_start-embargo_bars*step
        if train_end <= start or test_end <= test_start:
            continue
        windows.append({"fold": index+1, "train_from": start, "train_to": train_end,
            "test_from": test_start, "test_to": min(test_end, end), "embargo_bars": embargo_bars})
    return windows


def case_context(event: dict[str, Any], environment: dict[str, Any]) -> dict[str, Any]:
    from gatescope.quant.structure_detection import bucket
    higher = environment.get("higher_timeframe", {})
    direction = event.get("signal_direction", event.get("direction"))
    higher_trend = higher.get("trend", "UNKNOWN")
    alignment = "UNKNOWN" if higher_trend == "UNKNOWN" or direction not in {"bullish", "bearish"} else (
        "RANGE" if higher_trend == "RANGE" else "ALIGNED" if
        (direction == "bullish" and higher_trend == "UP") or (direction == "bearish" and higher_trend == "DOWN") else "OPPOSED")
    return {**environment, "score_bucket": bucket(event), "geometry_score": event.get("confidence"),
        "higher_alignment": alignment}


def policy_from_spec(spec: StrategySpec, step: int, *, spot: bool = False) -> dict[str, Any]:
    return {"fee_bps": spec.fee_bps, "slippage_bps": spec.slippage_bps,
        "funding_bps_per_8h": 0 if spot else spec.funding_bps_per_8h,
        "entry_delay_seconds": spec.entry_delay_bars*step,
        "max_holding_bars": spec.max_holding_bars, "max_entry_wait_bars": 2,
        "timeframe_seconds": step, "stop_mode": "PRICE_TOUCH"}
