"""Amortized O(1) streaming implementations of technical indicators."""

from __future__ import annotations

import math
from collections import deque
from typing import Mapping

from gatescope.domain.market import Bar
from gatescope.indicators.models import IndicatorValues
from gatescope.indicators.registry import IndicatorDefinition, IndicatorRegistry, ParamSpec


class Rolling:
    def __init__(self, period: int) -> None:
        self.period = period
        self.values: deque[float] = deque()
        self.total = 0.0
        self.total_sq = 0.0
        self.moment_mean = 0.0
        self.m2 = 0.0
        self.pushes = 0

    def push(self, value: float) -> None:
        n = len(self.values)
        delta = value-self.moment_mean
        self.moment_mean += delta/(n+1)
        self.m2 += delta*(value-self.moment_mean)
        self.values.append(value)
        self.total += value
        self.total_sq += value * value
        if len(self.values) > self.period:
            removed = self.values.popleft()
            self.total -= removed
            self.total_sq -= removed * removed
            mean = self.moment_mean + (self.moment_mean-removed)/self.period
            self.m2 -= (removed-self.moment_mean)*(removed-mean)
            self.moment_mean = mean
        self.pushes += 1
        # Bound round-off drift, centering before summation avoids cancellation
        # for high nominal prices with very small genuine volatility.
        if self.pushes % self.period == 0:
            origin = self.values[0]
            centered = [v-origin for v in self.values]
            offset = math.fsum(centered)/len(centered)
            self.moment_mean = origin+offset
            self.m2 = math.fsum((v-offset)**2 for v in centered)

    @property
    def ready(self) -> bool:
        return len(self.values) == self.period

    @property
    def mean(self) -> float:
        return self.total / self.period


class SMA:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.window = Rolling(int(params["period"]))

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        self.window.push(float(bar.close))
        return {"sma": self.window.mean if self.window.ready else None}


class EMAValue:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.seed: list[float] = []
        self.value: float | None = None

    def push(self, value: float) -> float | None:
        if self.value is None:
            self.seed.append(value)
            if len(self.seed) == self.period:
                self.value = sum(self.seed) / self.period
                self.seed.clear()
        else:
            self.value += self.alpha * (value - self.value)
        return self.value


class EMA:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.ema = EMAValue(int(params["period"]))

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        return {"ema": self.ema.push(float(bar.close))}


class MACD:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        fast = int(params["fast_period"])
        slow = int(params["slow_period"])
        if fast >= slow:
            raise ValueError("fast_period must be smaller than slow_period")
        self.fast = EMAValue(fast)
        self.slow = EMAValue(slow)
        self.signal = EMAValue(int(params["signal_period"]))

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        price = float(bar.close)
        fast = self.fast.push(price)
        slow = self.slow.push(price)
        macd = fast - slow if fast is not None and slow is not None else None
        signal = self.signal.push(macd) if macd is not None else None
        histogram = macd - signal if macd is not None and signal is not None else None
        return {"macd": macd, "signal": signal, "histogram": histogram}


class RSI:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.period = int(params["period"])
        self.previous: float | None = None
        self.gains: list[float] = []
        self.losses: list[float] = []
        self.average_gain: float | None = None
        self.average_loss: float | None = None

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        close = float(bar.close)
        if self.previous is None:
            self.previous = close
            return {"rsi": None}
        change = close - self.previous
        self.previous = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.average_gain is None or self.average_loss is None:
            self.gains.append(gain)
            self.losses.append(loss)
            if len(self.gains) < self.period:
                return {"rsi": None}
            self.average_gain = sum(self.gains) / self.period
            self.average_loss = sum(self.losses) / self.period
            self.gains.clear()
            self.losses.clear()
        else:
            self.average_gain = ((self.period - 1) * self.average_gain + gain) / self.period
            self.average_loss = ((self.period - 1) * self.average_loss + loss) / self.period
        if self.average_loss == 0:
            value = 50.0 if self.average_gain == 0 else 100.0
        else:
            value = 100.0 - 100.0 / (1.0 + self.average_gain / self.average_loss)
        return {"rsi": value}


class BollingerBands:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.window = Rolling(int(params["period"]))
        self.deviations = float(params["deviations"])

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        self.window.push(float(bar.close))
        if not self.window.ready:
            return {"middle": None, "upper": None, "lower": None, "bandwidth": None}
        middle = self.window.moment_mean
        variance = max(0.0, self.window.m2 / self.window.period)
        deviation = math.sqrt(variance)
        upper = middle + self.deviations * deviation
        lower = middle - self.deviations * deviation
        bandwidth = (upper - lower) / middle if middle != 0 else None
        return {"middle": middle, "upper": upper, "lower": lower, "bandwidth": bandwidth}


class ATRValue:
    def __init__(self, period: int) -> None:
        self.period = period
        self.seed: list[float] = []
        self.value: float | None = None

    def push(self, true_range: float) -> float | None:
        if self.value is None:
            self.seed.append(true_range)
            if len(self.seed) == self.period:
                self.value = sum(self.seed) / self.period
                self.seed.clear()
        else:
            self.value = ((self.period - 1) * self.value + true_range) / self.period
        return self.value


class ATR:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.atr = ATRValue(int(params["period"]))
        self.previous_close: float | None = None

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        true_range = high - low
        if self.previous_close is not None:
            true_range = max(true_range, abs(high - self.previous_close), abs(low - self.previous_close))
        self.previous_close = close
        return {"atr": self.atr.push(true_range)}


class ADX:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.period = int(params["period"])
        self.previous_high: float | None = None
        self.previous_low: float | None = None
        self.previous_close: float | None = None
        self.tr_seed: list[float] = []
        self.plus_seed: list[float] = []
        self.minus_seed: list[float] = []
        self.smoothed_tr: float | None = None
        self.smoothed_plus: float | None = None
        self.smoothed_minus: float | None = None
        self.dx_seed: list[float] = []
        self.adx: float | None = None

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        if self.previous_close is None or self.previous_high is None or self.previous_low is None:
            self.previous_high, self.previous_low, self.previous_close = high, low, close
            return self._empty()
        up_move, down_move = high - self.previous_high, self.previous_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        true_range = max(high - low, abs(high - self.previous_close), abs(low - self.previous_close))
        self.previous_high, self.previous_low, self.previous_close = high, low, close

        if self.smoothed_tr is None or self.smoothed_plus is None or self.smoothed_minus is None:
            self.tr_seed.append(true_range)
            self.plus_seed.append(plus_dm)
            self.minus_seed.append(minus_dm)
            if len(self.tr_seed) < self.period:
                return self._empty()
            self.smoothed_tr = sum(self.tr_seed)
            self.smoothed_plus = sum(self.plus_seed)
            self.smoothed_minus = sum(self.minus_seed)
            self.tr_seed.clear()
            self.plus_seed.clear()
            self.minus_seed.clear()
        else:
            self.smoothed_tr = self.smoothed_tr - self.smoothed_tr / self.period + true_range
            self.smoothed_plus = self.smoothed_plus - self.smoothed_plus / self.period + plus_dm
            self.smoothed_minus = self.smoothed_minus - self.smoothed_minus / self.period + minus_dm

        plus_di = 100.0 * self.smoothed_plus / self.smoothed_tr if self.smoothed_tr else 0.0
        minus_di = 100.0 * self.smoothed_minus / self.smoothed_tr if self.smoothed_tr else 0.0
        denominator = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0
        if self.adx is None:
            self.dx_seed.append(dx)
            if len(self.dx_seed) == self.period:
                self.adx = sum(self.dx_seed) / self.period
                self.dx_seed.clear()
        else:
            self.adx = ((self.period - 1) * self.adx + dx) / self.period
        return {"adx": self.adx, "plus_di": plus_di, "minus_di": minus_di}

    @staticmethod
    def _empty() -> IndicatorValues:
        return {"adx": None, "plus_di": None, "minus_di": None}


class OBV:
    def __init__(self, params: Mapping[str, int | float]) -> None:
        self.previous_close: float | None = None
        self.value = 0.0

    def update(self, bar: Bar, upstream: Mapping[str, IndicatorValues]) -> IndicatorValues:
        close, volume = float(bar.close), float(bar.volume)
        if self.previous_close is not None:
            if close > self.previous_close:
                self.value += volume
            elif close < self.previous_close:
                self.value -= volume
        self.previous_close = close
        return {"obv": self.value}


def default_registry() -> IndicatorRegistry:
    period = {"period": ParamSpec(int, 14, 1, 10_000)}
    registry = IndicatorRegistry()
    registry.register(IndicatorDefinition("sma", "1.0.0", ("sma",), period, lambda p: int(p["period"]), SMA))
    registry.register(IndicatorDefinition("ema", "1.0.0", ("ema",), period, lambda p: int(p["period"]), EMA))
    registry.register(
        IndicatorDefinition(
            "macd", "1.0.0", ("macd", "signal", "histogram"),
            {
                "fast_period": ParamSpec(int, 12, 1, 10_000),
                "slow_period": ParamSpec(int, 26, 2, 10_000),
                "signal_period": ParamSpec(int, 9, 1, 10_000),
            },
            lambda p: int(p["slow_period"]) + int(p["signal_period"]) - 1,
            MACD,
        )
    )
    registry.register(IndicatorDefinition("rsi", "1.0.0", ("rsi",), period, lambda p: int(p["period"]) + 1, RSI))
    registry.register(
        IndicatorDefinition(
            "boll", "1.0.1", ("middle", "upper", "lower", "bandwidth"),
            {"period": ParamSpec(int, 20, 2, 10_000), "deviations": ParamSpec(float, 2.0, 0.1, 10.0)},
            lambda p: int(p["period"]), BollingerBands,
        )
    )
    registry.register(IndicatorDefinition("atr", "1.0.0", ("atr",), period, lambda p: int(p["period"]), ATR))
    registry.register(IndicatorDefinition("adx", "1.0.0", ("adx", "plus_di", "minus_di"), period, lambda p: int(p["period"]) * 2, ADX))
    registry.register(IndicatorDefinition("obv", "1.0.0", ("obv",), {}, lambda p: 1, OBV))
    return registry
