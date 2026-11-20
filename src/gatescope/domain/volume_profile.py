"""Bounded, lower-timeframe OHLCV price allocation; never tick/order-flow data.

Volume is conserved by uniformly allocating each candle across its LINEAR
high-low interval. Finer candles improve resolution, not the underlying claim:
neither execution-at-price nor aggressor-side volume is available here.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from gatescope.domain.market import TIMEFRAME_SECONDS

VERSION = "fine-ohlcv-profile-1"
MAX_BARS = 50_000
MAX_RANGE_SECONDS = 366 * 86400
CACHE_SECONDS = 30
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$", re.IGNORECASE)


class VolumeProfileInputError(ValueError):
    pass


@dataclass(frozen=True)
class VolumeProfilePlan:
    timeframe: str
    from_time: int
    to_time: int
    rows: int
    value_area: int
    source_timeframe: str
    source_step_seconds: int
    expected_bars: int
    first_open: int


def plan_volume_profile(timeframe: str, from_time: int, to_time: int, *, rows: int = 48,
                        value_area: int = 70, asof: int) -> VolumeProfilePlan:
    if timeframe not in TIMEFRAME_SECONDS or rows not in (24, 48, 96) or isinstance(rows, bool):
        raise VolumeProfileInputError("周期或价格档位无效")
    if any(type(value) is not int for value in (from_time, to_time, value_area, asof)):
        raise VolumeProfileInputError("时间和价值区域须为整数")
    if not 50 <= value_area <= 95 or from_time < 0 or to_time <= from_time:
        raise VolumeProfileInputError("时间范围或价值区域无效")
    if to_time-from_time > MAX_RANGE_SECONDS:
        raise VolumeProfileInputError("可视范围不能超过366天，请缩小图表范围")
    if from_time > asof or to_time > asof+TIMEFRAME_SECONDS[timeframe]:
        raise VolumeProfileInputError("请求范围超出当前行情时间")
    for source, step in TIMEFRAME_SECONDS.items():
        if step > TIMEFRAME_SECONDS[timeframe]:
            break
        first = ((from_time+step-1)//step)*step
        expected = max(0, (to_time-first)//step)
        if expected <= MAX_BARS:
            return VolumeProfilePlan(timeframe, from_time, to_time, rows, value_area, source, step, expected, first)
    raise VolumeProfileInputError("该周期的可视范围超过50000根，请缩小图表范围")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, str) and not _DECIMAL.fullmatch(value.strip()):
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            return None
        # Do not transform a nonzero tiny decimal into a fake zero-volume bar.
        if number == 0 and Decimal(str(value)) != 0:
            return None
        return number
    except (ValueError, OverflowError, InvalidOperation):
        return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        integer = int(value)
        return integer if value == integer and abs(integer) <= 2**53-1 else None
    except (ValueError, OverflowError):
        return None


def _candle(row: Mapping[str, Any]) -> tuple[float, float, float] | None:
    values = [_number(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
    if any(value is None for value in values):
        return None
    opening, high, low, close, volume = values
    if low <= 0 or high < low or not low <= opening <= high or not low <= close <= high or volume < 0:
        return None
    return low, high, volume


def _iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else None


def _add(values: list[float], errors: list[float], index: int, amount: float) -> None:
    corrected = amount-errors[index]
    next_value = values[index]+corrected
    errors[index] = (next_value-values[index])-corrected
    values[index] = next_value


def compute_volume_profile(bars: Sequence[Mapping[str, Any]], *, rows: int = 48, value_area: float = .7) -> dict[str, Any]:
    """Pure numerical kernel, ported from the visible-profile TypeScript kernel.

The caller supplies canonical final bars. Invalid values remain counted, never
converted into zeroes. Two passes + an interior-bin sweep bound work to
O(n log rows + rows), with no per-candle array of price-bin contributions.
"""
    if len(bars) > MAX_BARS or rows not in (24, 48, 96) or isinstance(rows, bool):
        raise VolumeProfileInputError("计算数量或价格档位超出限制")
    if not isinstance(value_area, (float, int)) or not math.isfinite(value_area) or not .5 <= value_area <= .95:
        raise VolumeProfileInputError("价值区域须为50%至95%")
    result: dict[str, Any] = {
        "version": VERSION, "method": "LOWER_TIMEFRAME_OHLCV_ESTIMATE", "status": "NO_VISIBLE_BARS",
        "bins": [], "requestedBins": rows, "effectiveBins": 0, "valueArea": value_area, "actualValueArea": None,
        "poc": None, "vah": None, "val": None, "pocIndex": None, "totalVolume": 0,
        "firstIndex": None, "lastIndex": None, "firstTime": None, "lastTime": None,
        "visibleCount": len(bars), "requestedVisibleCount": len(bars), "includedCount": 0, "positiveVolumeCount": 0,
        "excludedUnclosedCount": 0, "excludedInvalidCount": 0, "unknownFinalityCount": 0,
        "coverage": None, "rangeCoverage": None, "qualityFlags": [],
    }
    if not bars:
        return result
    minimum, maximum, max_volume = math.inf, -math.inf, 0.0
    for row in bars:
        if row.get("is_final") is not True:
            result["excludedUnclosedCount"] += 1
            continue
        candle = _candle(row)
        if candle is None:
            result["excludedInvalidCount"] += 1
            continue
        low, high, volume = candle
        result["includedCount"] += 1
        result["positiveVolumeCount"] += volume > 0
        timestamp = _integer(row.get("ts"))
        if result["firstTime"] is None:
            result["firstTime"] = _iso(timestamp)
        result["lastTime"] = _iso(timestamp)
        minimum, maximum, max_volume = min(minimum, low), max(maximum, high), max(max_volume, volume)
    result["coverage"] = result["includedCount"]/len(bars)
    result["rangeCoverage"] = 1
    if not result["includedCount"]:
        result["status"] = "NO_VALID_BARS"
        return result
    span = maximum-minimum
    edges = [minimum]
    if span > 0:
        for index in range(1, rows+1):
            edge = maximum if index == rows else minimum+span*(index/rows)
            if edge > edges[-1]:
                edges.append(edge)
        if len(edges) != rows+1:
            result["qualityFlags"].append("PRICE_BINS_COLLAPSED_AT_FLOAT_PRECISION")
    else:
        edges.append(maximum)
    count = len(edges)-1
    result["effectiveBins"] = count
    result["bins"] = [{"low": edges[i], "high": edges[i+1], "price": edges[i]+(edges[i+1]-edges[i])/2,
                       "volume": 0, "volumeFraction": 0, "isPoc": False, "inValueArea": False} for i in range(count)]
    if max_volume == 0:
        result["status"] = "ZERO_VOLUME"
        return result
    mass, error = [0.0]*count, [0.0]*count
    delta, delta_error = [0.0]*(count+1), [0.0]*(count+1)
    total, total_error = [0.0], [0.0]
    precision_limited = False
    for row in bars:
        candle = _candle(row) if row.get("is_final") is True else None
        if candle is None or candle[2] == 0:
            continue
        low, high, volume = candle
        weight = volume/max_volume
        if weight == 0:
            precision_limited = True
            continue
        _add(total, total_error, 0, weight)
        first = max(0, min(count-1, bisect_right(edges, low)-1))
        last = max(first, min(count-1, bisect_left(edges, high)-1))
        if first == last or low == high:
            _add(mass, error, first, weight)
            continue
        width = high-low
        head = min(weight, max(0, weight*((edges[first+1]-low)/width)))
        _add(mass, error, first, head)
        if last == first+1:
            _add(mass, error, last, weight-head)
            continue
        tail = min(weight-head, max(0, weight*((high-edges[last])/width)))
        _add(mass, error, last, tail)
        remaining = max(0, weight-head-tail)
        interior_ratio = (edges[last]-edges[first+1])/span
        density = remaining/interior_ratio if interior_ratio > 0 else math.inf
        if math.isfinite(density):
            _add(delta, delta_error, first+1, density)
            _add(delta, delta_error, last, -density)
        else:
            precision_limited = True
            for index in range(first+1, last):
                _add(mass, error, index, remaining*((edges[index+1]-edges[index])/(edges[last]-edges[first+1])))
    density, density_error = [0.0], [0.0]
    for index in range(count):
        _add(density, density_error, 0, delta[index])
        interior = max(0, density[0])*((edges[index+1]-edges[index])/span) if span > 0 else 0
        _add(mass, error, index, interior)
    accumulated = math.fsum(mass)
    if precision_limited or abs(accumulated-total[0]) > total[0]*1e-10:
        result["qualityFlags"].append("VOLUME_FLOAT_PRECISION_LIMIT")
    total_volume = total[0]*max_volume
    result["totalVolume"] = total_volume if math.isfinite(total_volume) else None
    if result["totalVolume"] is None:
        result["qualityFlags"].append("TOTAL_VOLUME_EXCEEDS_NUMBER_RANGE")
    bin_underflow = False
    for index, item in enumerate(result["bins"]):
        fraction = mass[index]/accumulated
        item["volumeFraction"] = fraction
        amount = fraction*total_volume if result["totalVolume"] is not None else None
        if amount == 0 and fraction > 0:
            amount, bin_underflow = None, True
        item["volume"] = amount
    if bin_underflow:
        result["qualityFlags"].append("BIN_VOLUME_BELOW_NUMBER_RANGE")
    bins = result["bins"]
    poc = 0
    for index in range(1, count):
        previous, candidate = bins[poc]["volumeFraction"], bins[index]["volumeFraction"]
        if candidate > previous+max(previous, candidate)*1e-12:
            poc = index
    lower = upper = poc
    covered = bins[poc]["volumeFraction"]
    while covered+1e-12 < value_area and (lower > 0 or upper < count-1):
        below = bins[lower-1]["volumeFraction"] if lower > 0 else -1
        above = bins[upper+1]["volumeFraction"] if upper < count-1 else -1
        if below >= above-max(abs(below), abs(above))*1e-12:
            lower, covered = lower-1, covered+below
        else:
            upper, covered = upper+1, covered+above
    result.update(status="AVAILABLE", pocIndex=poc, poc=bins[poc]["price"], val=bins[lower]["low"],
                  vah=bins[upper]["high"], actualValueArea=min(1, covered))
    bins[poc]["isPoc"] = True
    for index in range(lower, upper+1):
        bins[index]["inValueArea"] = True
    return result


def build_volume_profile(instrument: Mapping[str, Any], plan: VolumeProfilePlan,
                         source_rows: Sequence[Mapping[str, Any]], *, asof: int) -> dict[str, Any]:
    """Defensive as-of canonicalization and honest completeness metadata.

Select the newest *known* revision before finality/OHLC validation: a new bad
or non-final revision must not resurrect an older final candle. Unknown/future
availability is not evidence that the revision was observable at this request.
"""
    if len(source_rows) > MAX_BARS:
        raise VolumeProfileInputError("源数据超过计算上限，请缩小范围")
    flags = {"OHLCV_PRICE_ALLOCATION_ESTIMATED", "VOLUME_UNIT_UNSPECIFIED"}
    if plan.source_step_seconds > 60:
        flags.add("ADAPTIVE_SOURCE_TIMEFRAME")
    if plan.source_timeframe == plan.timeframe:
        flags.add("SAME_AS_CHART_TIMEFRAME")
    if plan.to_time > asof:
        flags.add("FUTURE_RANGE_NOT_CLOSED")
    if plan.from_time % plan.source_step_seconds or plan.to_time % plan.source_step_seconds:
        flags.add("PARTIAL_SOURCE_BOUNDARIES")
    canonical: dict[int, Mapping[str, Any]] = {}
    conflicts: set[int] = set()
    for row in source_rows:
        if any(str(row.get(field)) != str(instrument.get(field)) for field in ("venue", "market")) \
                or str(row.get("instrument_id")) != str(instrument["id"]) or row.get("timeframe") != plan.source_timeframe:
            flags.add("SOURCE_DIMENSION_MISMATCH_EXCLUDED")
            continue
        ts, revision = _integer(row.get("ts")), _integer(row.get("revision"))
        available, created = _number(row.get("available_at")), _number(row.get("created_at"))
        if ts is None or revision is None or revision < 1 or available is None or created is None:
            flags.add("UNKNOWN_SOURCE_PROVENANCE_EXCLUDED")
            continue
        if available > asof or created > asof:
            flags.add("FUTURE_SOURCE_REVISION_EXCLUDED")
            continue
        if ts < plan.first_open or ts >= min(plan.to_time, asof):
            flags.add("OUTSIDE_SOURCE_RANGE_EXCLUDED")
            continue
        previous = canonical.get(ts)
        if previous is None or revision > previous["revision"]:
            canonical[ts] = row
            conflicts.discard(ts)
        elif revision == previous["revision"] and row != previous:
            conflicts.add(ts)
        elif revision != previous["revision"]:
            flags.add("OLDER_SOURCE_REVISION_EXCLUDED")
    selected = []
    temporal_excluded = 0
    for ts, row in sorted(canonical.items()):
        if ts in conflicts:
            flags.add("CONFLICTING_SOURCE_REVISION_EXCLUDED")
            continue
        source_flags = row.get("quality_flags", [])
        if isinstance(source_flags, (list, tuple, set, frozenset)):
            for flag in source_flags:
                if isinstance(flag, str) and len(flag) <= 120 and len(flags) < 64:
                    flags.add(flag)
        if instrument["market"] == "SPOT" and (not isinstance(source_flags, (list, tuple, set, frozenset))
                or "REST_POLLING" not in source_flags
                or any(flag in source_flags for flag in ("GATE_MONTHLY_ARCHIVE", "LEGACY_SOURCE"))):
            # Current Gate spot REST stores quote turnover; archive units have
            # not been verified. Relative fractions still cannot mix units.
            flags.add("SOURCE_VOLUME_UNIT_UNVERIFIED")
            continue
        close = _integer(row.get("close_ts"))
        if ts % plan.source_step_seconds or close != ts+plan.source_step_seconds:
            flags.add("INVALID_SOURCE_TIMESTAMP_EXCLUDED")
            continue
        if _number(row["available_at"]) < close or _number(row["created_at"]) < 0:
            flags.add("INVALID_SOURCE_AVAILABILITY_EXCLUDED")
            continue
        if close > plan.to_time or close > asof:
            temporal_excluded += 1
            flags.add("OPEN_OR_OUTSIDE_SOURCE_CANDLE_EXCLUDED")
            continue
        selected.append(row)
    profile = compute_volume_profile(selected, rows=plan.rows, value_area=plan.value_area/100)
    profile["requestedVisibleCount"] = plan.expected_bars
    profile["rangeCoverage"] = len(selected)/plan.expected_bars if plan.expected_bars else 0
    profile["excludedUnclosedCount"] += temporal_excluded
    if profile["excludedInvalidCount"]:
        flags.add("INVALID_OHLCV_EXCLUDED")
    if profile["excludedUnclosedCount"]:
        flags.add("NON_FINAL_SOURCE_CANDLES_EXCLUDED")
    used = profile["includedCount"]
    coverage = used/plan.expected_bars if plan.expected_bars else 0
    if used < plan.expected_bars:
        flags.add("SOURCE_COVERAGE_INCOMPLETE")
    flags.update(profile["qualityFlags"])
    profile["qualityFlags"] = sorted(flags)
    status = "NO_DATA" if not used else "ZERO_VOLUME" if profile["status"] == "ZERO_VOLUME" else \
        "PARTIAL" if coverage < 1 or plan.to_time > asof or "PARTIAL_SOURCE_BOUNDARIES" in flags else "AVAILABLE"
    return {
        "version": VERSION, "instrument_id": str(instrument["id"]), "venue": instrument["venue"],
        "market": instrument["market"], "symbol": instrument.get("symbol"), "timeframe": plan.timeframe,
        "from_time": plan.from_time, "to_time": plan.to_time, "effective_to_time": min(plan.to_time, asof),
        "source_timeframe": plan.source_timeframe, "source_step_seconds": plan.source_step_seconds,
        "expected_bars": plan.expected_bars, "used_bars": used, "coverage": coverage,
        "status": status, "quality_flags": sorted(flags), "generated_at": asof,
        "expires_at": asof+CACHE_SECONDS, "profile": profile,
    }
