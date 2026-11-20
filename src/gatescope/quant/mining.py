"""Offline, bounded strategy-family development; no execution/configuration ports.

Candidate signals reuse the production next-open/ATR/cost simulator. This module
does not add every searched parameter to the deployable strategy catalog.
"""
from typing import Literal
from dataclasses import dataclass, asdict
from itertools import product

from gatescope.indicators.builtins import RSI, SMA
from gatescope.quant.backtest import StrategySpec, features, simulate
from gatescope.quant.performance import performance_screen

FAMILIES = ("rsi2", "failed_breakout", "boll_reentry", "keltner", "trend_dip", "range_rejection")


@dataclass(frozen=True)
class Candidate:
    family: str
    threshold: int
    regime: str
    direction: Literal["LONG", "SHORT", "BOTH"]
    stop_atr: float
    reward_risk: float
    holding: int

    def execution_spec(self, cost=1):
        return StrategySpec(direction=self.direction, availability_mode="HISTORICAL_CLOSE",
            slow_period=100, stop_atr=self.stop_atr, reward_risk=self.reward_risk,
            max_holding_bars=self.holding, risk_fraction=.005,
            fee_bps=5*cost, slippage_bps=5*cost, funding_bps_per_8h=cost)

    def payload(self):
        return asdict(self)


def candidates():
    directions: tuple[Literal["LONG", "BOTH"], ...] = ("LONG", "BOTH")
    return [Candidate(*values) for values in product(FAMILIES,(0,1),("ANY","TREND","RANGE"),
            directions,(2.,3.),(.7,1.2),(6,18))]


def mining_features(bars):
    values = features(bars,StrategySpec(slow_period=100, availability_mode="HISTORICAL_CLOSE"))
    rsi, sma = RSI({"period":2}), SMA({"period":200})
    for bar, point in zip(bars,values, strict=False):
        point["values"]["rsi2"] = rsi.update(bar,{})["rsi"]
        point["values"]["sma200"] = sma.update(bar,{})["sma"]
    return values


def candidate_signals(bars, points, candidate):
    result = []
    for i,(bar,point) in enumerate(zip(bars,points, strict=False)):
        v, side = point["values"], 0
        if i >= 200 and point["ready"]:
            close, opened, high, low = map(float,(bar.close,bar.open,bar.high,bar.low))
            prev, prev_close = points[i-1]["values"],float(bars[i-1].close)
            atr, fast = v["atr.atr"], v["fast.ema"]
            strength = candidate.threshold
            if candidate.family=="rsi2":
                boundary = (10,25)[strength]
                side = 1 if v["rsi2"]<boundary else -1 if v["rsi2"]>100-boundary else 0
            elif candidate.family=="failed_breakout":
                fraction = (.5,.7)[strength]
                side = 1 if low<v["channel.low"]<close and close>=low+fraction*(high-low) else -1 if high>v["channel.high"]>close and close<=high-fraction*(high-low) else 0
            elif candidate.family=="boll_reentry":
                side = 1 if prev_close<prev["boll.lower"] and close>v["boll.lower"] and (not strength or close>opened) else -1 if prev_close>prev["boll.upper"] and close<v["boll.upper"] and (not strength or close<opened) else 0
            elif candidate.family=="keltner":
                distance = (1.5,2.5)[strength]*atr
                side = 1 if close<fast-distance else -1 if close>fast+distance else 0
            elif candidate.family=="trend_dip":
                boundary = (35,45)[strength]
                side = 1 if close>v["sma200"] and v["rsi.rsi"]<boundary else -1 if close<v["sma200"] and v["rsi.rsi"]>100-boundary else 0
            elif candidate.family=="range_rejection":
                fraction = (.55,.7)[strength]
                side = 1 if min(close,opened)-low>fraction*(high-low) and close<v["boll.middle"] else -1 if high-max(close,opened)>fraction*(high-low) and close>v["boll.middle"] else 0
            if candidate.regime=="TREND" and ((side>0 and close<=v["sma200"]) or (side<0 and close>=v["sma200"])):
                side = 0
            if candidate.regime=="RANGE" and v["adx.adx"]>=25:
                side = 0
            if candidate.direction=="LONG" and side<0:
                side = 0
        result.append({**point,"side":side,"matched_rules":[candidate.family] if side else []})
    return result


def evaluate(bars,signals,candidate,start,end,cost=1):
    outcome=simulate(bars,signals,candidate.execution_spec(cost),start,end)
    outcome["screening"] = performance_screen(outcome["metrics"])
    return outcome
