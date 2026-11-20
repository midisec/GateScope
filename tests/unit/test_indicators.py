from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gatescope.domain.market import Bar
from gatescope.indicators import (
    IndicatorBatchCache,
    IndicatorNode,
    IndicatorStream,
    compute_batch,
    default_registry,
)


def bars(closes: list[float], *, highs: list[float] | None = None, lows: list[float] | None = None) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, close in enumerate(closes):
        opened = start + timedelta(minutes=index)
        high = highs[index] if highs else close + 1
        low = lows[index] if lows else close - 1
        result.append(
            Bar(
                venue="GATE", market="FUTURES", instrument_id="btc", timeframe="1m",
                open_time=opened, close_time=opened + timedelta(minutes=1),
                open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(low)),
                close=Decimal(str(close)), volume=Decimal(str((index + 1) * 10)),
                is_final=True, revision=1, available_at=opened + timedelta(minutes=1),
            )
        )
    return result


def values_for(indicator_id: str, data: list[Bar], params: dict[str, int | float]) -> list[dict[str, float | None]]:
    node = IndicatorNode("test", indicator_id, params)
    points = compute_batch(default_registry(), [node], data)
    return [dict(point.values) for point in points]


def test_sma_ema_macd_reference_values() -> None:
    data = bars([10, 11, 12, 13])
    sma = values_for("sma", data, {"period": 3})
    ema = values_for("ema", data, {"period": 3})
    macd = values_for("macd", data, {"fast_period": 2, "slow_period": 3, "signal_period": 2})
    assert sma[-2]["sma"] == pytest.approx(11.0)
    assert sma[-1]["sma"] == pytest.approx(12.0)
    assert ema[-2]["ema"] == pytest.approx(11.0)
    assert ema[-1]["ema"] == pytest.approx(12.0)
    assert macd[-1] == pytest.approx({"macd": 0.5, "signal": 0.5, "histogram": 0.0})


def test_rsi_boll_atr_obv_reference_values() -> None:
    rsi = values_for("rsi", bars([10, 11, 12, 11]), {"period": 3})
    boll = values_for("boll", bars([10, 11, 12]), {"period": 3, "deviations": 2.0})
    atr = values_for(
        "atr",
        bars([11, 12, 13], highs=[12, 13, 14], lows=[10, 11, 12]),
        {"period": 3},
    )
    obv = values_for("obv", bars([10, 11, 10.5]), {})

    assert rsi[-1]["rsi"] == pytest.approx(66.6666666667)
    assert boll[-1]["middle"] == pytest.approx(11.0)
    assert boll[-1]["upper"] == pytest.approx(11 + 2 * (2 / 3) ** 0.5)
    assert boll[-1]["lower"] == pytest.approx(11 - 2 * (2 / 3) ** 0.5)
    assert atr[-1]["atr"] == pytest.approx(2.0)
    assert [point["obv"] for point in obv] == pytest.approx([0.0, 20.0, -10.0])


def test_adx_reference_for_monotonic_trend() -> None:
    data = bars(
        [10, 11, 12, 13, 14, 15],
        highs=[11, 12, 13, 14, 15, 16],
        lows=[9, 10, 11, 12, 13, 14],
    )
    adx = values_for("adx", data, {"period": 3})
    assert adx[-1]["adx"] == pytest.approx(100.0)
    assert adx[-1]["plus_di"] == pytest.approx(50.0)
    assert adx[-1]["minus_di"] == pytest.approx(0.0)


def test_bollinger_variance_retains_small_volatility_at_large_prices():
    import math
    values = [1e9 + .01*(i%5) for i in range(120)]
    result = values_for("boll", bars(values), {"period":20,"deviations":2.0})
    for i in range(19,len(values)):
        sample=values[i-19:i+1]
        origin=sample[0]; centered=[v-origin for v in sample]
        mean=math.fsum(centered)/20
        std=math.sqrt(math.fsum((v-mean)**2 for v in centered)/20)
        assert result[i]["upper"]-result[i]["lower"]==pytest.approx(4*std,rel=1e-4)


def test_cached_indicators_do_not_bypass_earlier_watermark():
    data=bars([10,11,12]); cache=IndicatorBatchCache()
    registry=default_registry(); nodes=[IndicatorNode("e","ema",{"period":2})]
    compute_batch(registry,nodes,data,cache=cache)
    with pytest.raises(ValueError,match="not available"):
        compute_batch(registry,nodes,data,cache=cache,watermark=data[0].available_at)


def test_batch_and_stream_are_identical_and_cache_is_versioned() -> None:
    registry = default_registry()
    nodes = [
        IndicatorNode("close_trend", "ema", {"period": 3}),
        IndicatorNode("volatility", "atr", {"period": 3}, depends_on=("close_trend",)),
    ]
    data = bars([10, 11, 12, 13, 14])
    stream = IndicatorStream(registry, nodes)
    streamed = tuple(point for bar in data for point in stream.update(bar, watermark=bar.available_at))
    cache = IndicatorBatchCache()
    batched = compute_batch(registry, nodes, data, watermark=data[-1].available_at, cache=cache)
    assert streamed == batched
    assert compute_batch(registry, nodes, data, cache=cache) is batched
    assert len(cache) == 1
    assert all(point.indicator_version == "1.0.0" for point in batched)


def test_registry_rejects_bad_parameters_missing_dependencies_and_cycles() -> None:
    registry = default_registry()
    with pytest.raises(ValueError, match="between"):
        registry.plan([IndicatorNode("bad", "sma", {"period": 0})])
    with pytest.raises(ValueError, match="unknown parameters"):
        registry.plan([IndicatorNode("bad", "sma", {"period": 3, "mystery": 1})])
    with pytest.raises(ValueError, match="missing"):
        registry.plan([IndicatorNode("bad", "sma", {}, depends_on=("absent",))])
    with pytest.raises(ValueError, match="cycle"):
        registry.plan([
            IndicatorNode("a", "sma", {}, depends_on=("b",)),
            IndicatorNode("b", "ema", {}, depends_on=("a",)),
        ])
    with pytest.raises(ValueError, match="smaller"):
        IndicatorStream(
            registry,
            [IndicatorNode("bad", "macd", {"fast_period": 5, "slow_period": 3})],
        )


def test_stream_rejects_unavailable_nonfinal_and_revised_bars() -> None:
    registry = default_registry()
    first, second = bars([10, 11])
    stream = IndicatorStream(registry, [IndicatorNode("sma", "sma", {"period": 2})])
    with pytest.raises(ValueError, match="not available"):
        stream.update(first, watermark=first.open_time)
    nonfinal = Bar(
        venue=first.venue, market=first.market, instrument_id=first.instrument_id,
        timeframe=first.timeframe, open_time=first.open_time, close_time=first.close_time,
        open=first.open, high=first.high, low=first.low, close=first.close,
        volume=first.volume, is_final=False, revision=1, available_at=first.available_at,
    )
    with pytest.raises(ValueError, match="final"):
        stream.update(nonfinal)
    stream.update(first)
    stream.update(second)
    with pytest.raises(ValueError, match="rebuild"):
        stream.update(second)
