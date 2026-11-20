"""Transfer test of fixed causal context filters; no original report changes."""

from dataclasses import asdict, dataclass
from itertools import product
from statistics import median

from gatescope.indicators.builtins import EMAValue
from gatescope.quant.directional import FAMILIES, Rule, signal_book, simulate_directional

VERSION = "directional-context-transfer-1.0.0"
CONTEXTS = {
    "RANGE": "ADX < 20",
    "STRONG_TREND": "ADX >= 25 and EMA20/200 aligned",
    "SLOW_TREND": "EMA80/800 aligned",
    "NORMAL_VOL": "ATR/close <= preceding 240-bar median",
}


@dataclass(frozen=True)
class ContextRule:
    family: str
    direction: str
    regime: str
    exit_style: str

    def payload(self):
        return asdict(self)


def rules():
    return [
        ContextRule(*v) for v in product(FAMILIES, ("LONG", "BOTH"), CONTEXTS, ("QUICK", "SWING", "MEASURED"))
    ]


def signal_context(bars):
    points, book = signal_book(bars)
    e80, e800 = EMAValue(80), EMAValue(800)
    volatility: list[float] = []
    for bar, point in zip(bars, points, strict=False):
        close = float(bar.close)
        a, b = e80.push(close), e800.push(close)
        v = point["values"]
        ratio = v["atr.atr"] / close if v["atr.atr"] is not None else None
        v["context.ema80"], v["context.ema800"] = a, b
        v["context.prior_atr_fraction_median"] = median(volatility[-240:]) if len(volatility) >= 240 else None
        v["context.atr_fraction"] = ratio
        if ratio is not None:
            volatility.append(ratio)
    return points, book


def filtered(points, events, regime):
    result: list = []
    for p, e in zip(points, events, strict=False):
        if e is None:
            result.append(None)
            continue
        v, side = p["values"], e["side"]
        if regime == "RANGE":
            valid = v["adx.adx"] < 20
        elif regime == "STRONG_TREND":
            valid = v["adx.adx"] >= 25 and side * (v["fast.ema"] - v["slow.ema"]) > 0
        elif regime == "SLOW_TREND":
            valid = v["context.ema800"] is not None and side * (v["context.ema80"] - v["context.ema800"]) > 0
        elif regime == "NORMAL_VOL":
            valid = (
                v["context.prior_atr_fraction_median"] is not None
                and v["context.atr_fraction"] <= v["context.prior_atr_fraction_median"]
            )
        else:
            raise ValueError("Unknown causal context")
        result.append(e if valid else None)
    return result


def evaluate(bars, points, events, rule, start, end, funding, **kwargs):
    result = simulate_directional(
        bars,
        points,
        events,
        Rule(rule.family, rule.direction, "ANY", rule.exit_style),
        start,
        end,
        funding,
        **kwargs,
    )
    result["rule"] = rule.payload()
    result["engine_version"] = VERSION
    return result
