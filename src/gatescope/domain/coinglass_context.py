"""Exact, causal Coinglass chart evidence; never an order or probability model.

History is known to this platform at collection time, not retroactively at its
provider period. USD open interest is a marked value, not signed capital flow.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unicodedata
from decimal import Decimal
from typing import Any, Mapping

from gatescope.domain.market import TIMEFRAME_SECONDS

VERSION = "coinglass-context-1"
REVIEW_VERSION = "coinglass-review-1"
HISTORY_SCOPE = "LATEST_WINDOW_256_NOT_FULL_HISTORY"
METRICS = {
    "oi": ("OI 持仓量", "USD", "close", "OPEN_INTEREST_MARK_VALUE"),
    "funding": ("资金费率", "PERCENT", "close", "FUNDING_RATE"),
    "lsr": ("全账户多空比", "RATIO", "global_account_long_short_ratio", "GLOBAL_ACCOUNTS"),
    "liquidation_long": ("多单实际爆仓", "USD", "long_liquidation_usd", "ACTUAL_LONG_LIQUIDATIONS"),
    "liquidation_short": ("空单实际爆仓", "USD", "short_liquidation_usd", "ACTUAL_SHORT_LIQUIDATIONS"),
}
_SOURCES = {
    "oi": "https://docs.coinglass.com/reference/oi-ohlc-histroy",
    "funding": "https://docs.coinglass.com/reference/fr-ohlc-histroy",
    "lsr": "https://docs.coinglass.com/reference/global-longshort-account-ratio",
    "liquidation_long": "https://docs.coinglass.com/reference/liquidation-history",
    "liquidation_short": "https://docs.coinglass.com/reference/liquidation-history",
}


def canonical_hash(value: Any) -> str:
    """Canonical content digest; rejects non-JSON finite values."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _epoch(value: Any) -> int | None:
    result = _number(value)
    return int(result) if result is not None and result.is_integer() and 0 <= result <= 253402300799 else None


def _identity(value: Any, *, asset: bool = False) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 120:
        return None
    normalized = unicodedata.normalize("NFC", value)
    if not all(unicodedata.category(char)[0] in "LNM" or char in "_.-" for char in normalized):
        return None
    # Unicode case-folding/NFKC could conflate distinct tokens. ASCII case is the
    # only case alias permitted for asset metadata, never for contract symbols.
    return normalized.upper() if asset and normalized.isascii() else normalized


def _alias(record: Mapping[str, Any], *keys: str, asset: bool = False) -> str | None:
    values = [_identity(record[key], asset=asset) for key in keys if record.get(key) is not None]
    return values[0] if values and values[0] is not None and len(set(values)) == 1 else None


def match_instrument(instrument: Mapping[str, Any], supported_pairs: Any) -> dict[str, Any]:
    """Match full Gate perpetual identity, never infer assets or remove multipliers.

    ``supported_pairs`` is the unwrapped exchange-keyed provider catalog, or a
    list whose records each explicitly carry ``exchange``. Caller must supply
    the local venue from its catalog; missing venue is not assumed to be Gate.
    """
    def failed(reason: str, status: str = "UNSUPPORTED") -> dict[str, Any]:
        return {"status": status, "reason": reason, "scope": None}
    if not isinstance(instrument, Mapping):
        return failed("INSTRUMENT_INVALID")
    venue = instrument.get("venue")
    if not isinstance(venue, str) or venue.upper() != "GATE":
        return failed("VENUE_NOT_GATE")
    base = _alias(instrument, "base_asset", "base", asset=True)
    quote = _alias(instrument, "quote_asset", "quote", asset=True)
    settle = _alias(instrument, "settlement_currency", "settle_asset", "settle", asset=True)
    symbol = _alias(instrument, "symbol")
    if None in (base, quote, settle, symbol):
        return failed("LOCAL_DIMENSIONS_INCOMPLETE_OR_CONFLICTING")
    market = str(instrument.get("market", "")).upper().replace(":", "_")
    if market == "FUTURES" and settle in {"USDT", "BTC"}:
        market = "FUTURES_" + settle
    if market not in {"FUTURES_USDT", "FUTURES_BTC"} or market != "FUTURES_" + str(settle):
        return failed("MARKET_OR_SETTLEMENT_UNSUPPORTED")
    if str(instrument.get("contract_type", "PERPETUAL")).upper() not in {"PERPETUAL", "SWAP"}:
        return failed("PRODUCT_NOT_PERPETUAL")
    if isinstance(supported_pairs, Mapping):
        gate_keys = [key for key in supported_pairs if isinstance(key, str) and key.upper() == "GATE"]
        if len(gate_keys) != 1 or not isinstance(supported_pairs[gate_keys[0]], list):
            return failed("GATE_CATALOG_UNAVAILABLE")
        pairs = supported_pairs[gate_keys[0]]
    elif isinstance(supported_pairs, list):
        pairs = [record for record in supported_pairs if isinstance(record, Mapping)
                 and str(record.get("exchange", "")).upper() == "GATE"]
    else:
        return failed("CATALOG_SCHEMA_INVALID")
    matched = []
    for record in pairs:
        if not isinstance(record, Mapping):
            continue
        if record.get("exchange") is not None and str(record["exchange"]).upper() != "GATE":
            continue
        if _alias(record, "instrument_id", "symbol") != symbol:
            continue
        if (_alias(record, "base_asset", asset=True), _alias(record, "quote_asset", asset=True),
                _alias(record, "settlement_currency", asset=True)) != (base, quote, settle):
            continue
        if str(record.get("contract_type", "PERPETUAL")).upper() not in {"PERPETUAL", "SWAP"}:
            continue
        if str(record.get("market", "FUTURES")).upper() not in {"FUTURES", "PERPETUAL", "SWAP", market}:
            continue
        matched.append(record)
    if len(matched) != 1:
        return failed("NO_EXACT_CONTRACT" if not matched else "MULTIPLE_EXACT_CONTRACTS",
                      "UNSUPPORTED" if not matched else "AMBIGUOUS")
    pair = matched[0]
    funding_hours = _number(pair.get("funding_interval"))
    funding_seconds = int(funding_hours * 3600) if funding_hours is not None and 0 < funding_hours <= 24 else None
    return {"status": "EXACT", "reason": "FULL_CONTRACT_IDENTITY", "scope": {
        "exchange": "Gate", "symbol": symbol, "base_asset": base, "quote_asset": quote,
        "settlement_currency": settle, "market": market, "product_type": "PERPETUAL",
        "funding_interval_seconds": funding_seconds,
    }}


def normalize_series(
    metric_id: str, rows: Any, *, timeframe: str, fetched_at: int,
    unit: str | None = None, unit_status: str = "VERIFIED", limit: int = 256,
    closed_at: int | None = None,
) -> dict[str, Any]:
    """Retain genuine closed native periods only, never zero-fill or upsample.

    Vendor timestamps are explicitly milliseconds; second-based/unaligned
    timestamps are invalid rather than auto-detected. Conflicting duplicates
    exclude the entire timestamp, independent of provider ordering.
    """
    if metric_id not in METRICS or timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("unsupported Coinglass metric or timeframe")
    if _epoch(fetched_at) is None or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
        raise ValueError("invalid collection time or window limit")
    closed_at = fetched_at if closed_at is None else closed_at
    if _epoch(closed_at) is None or closed_at > fetched_at:
        raise ValueError("invalid finality cutoff")
    label, expected_unit, field, population = METRICS[metric_id]
    unit = unit or expected_unit
    if unit_status not in {"VERIFIED", "UNKNOWN"}:
        raise ValueError("invalid unit verification status")
    step = TIMEFRAME_SECONDS[timeframe]
    flags: set[str] = {"LATEST_WINDOW_ONLY", "KNOWN_AT_COLLECTION_NOT_HISTORICAL_PERIOD"}
    result: dict[str, Any] = {
        "id": metric_id, "label": label, "unit": unit, "unit_status": unit_status,
        "population": population, "native_timeframe": timeframe, "points": [], "quality_flags": [],
        "unit_source": _SOURCES[metric_id], "status": "EMPTY", "gaps": [], "coverage": 0.0,
        "freshness_seconds": None,
    }
    if unit != expected_unit or unit_status != "VERIFIED":
        flags.add("UNIT_UNKNOWN_OR_MISMATCH")
        result.update(status="MISMATCH", quality_flags=sorted(flags))
        return result
    if not isinstance(rows, list):
        flags.add("SCHEMA_INVALID")
        result.update(status="ERROR", quality_flags=sorted(flags))
        return result
    values: dict[int, float] = {}
    conflicted: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            flags.add("INVALID_ROW_EXCLUDED")
            continue
        native_interval = row.get("interval", row.get("timeframe"))
        if native_interval is not None and native_interval != timeframe:
            flags.add("NATIVE_INTERVAL_MISMATCH")
            result.update(status="MISMATCH", quality_flags=sorted(flags))
            return result
        if row.get("unit") is not None and str(row["unit"]).upper() != expected_unit:
            flags.add("UNIT_UNKNOWN_OR_MISMATCH")
            result.update(status="MISMATCH", quality_flags=sorted(flags))
            return result
        if row.get("population") is not None and row["population"] != population:
            flags.add("POPULATION_MISMATCH")
            result.update(status="MISMATCH", quality_flags=sorted(flags))
            return result
        milliseconds = _number(row.get("time"))
        if milliseconds is None or not milliseconds.is_integer() or milliseconds < 1_000_000_000_000:
            flags.add("INVALID_TIME_EXCLUDED")
            continue
        timestamp = int(milliseconds) // 1000
        if milliseconds != timestamp * 1000 or timestamp % step:
            flags.add("UNALIGNED_TIME_EXCLUDED")
            continue
        if timestamp + step > closed_at:
            flags.add("OPEN_OR_FUTURE_PERIOD_EXCLUDED")
            continue
        value = _number(row.get(field))
        if value is None or (metric_id != "funding" and value < 0):
            flags.add("INVALID_VALUE_EXCLUDED")
            continue
        if metric_id in {"oi", "funding"} and all(key in row for key in ("open", "high", "low")):
            ohlc = [_number(row[key]) for key in ("open", "high", "low", "close")]
            if any(number is None for number in ohlc):
                flags.add("INVALID_OHLC_EXCLUDED")
                continue
            opening, high, low, closing = ohlc
            if high < max(opening, low, closing) or low > min(opening, high, closing) or (metric_id == "oi" and low < 0):
                flags.add("INVALID_OHLC_EXCLUDED")
                continue
        if timestamp in values:
            flags.add("DUPLICATE_TIME_DEDUPLICATED")
            if values[timestamp] != value:
                conflicted.add(timestamp)
                flags.add("CONFLICTING_DUPLICATE_EXCLUDED")
        else:
            values[timestamp] = value
    for timestamp in conflicted:
        values.pop(timestamp, None)
    points = [{"time": timestamp, "close_time": timestamp + step,
               "available_at": fetched_at, "value": values[timestamp]}
              for timestamp in sorted(values)[-limit:]]
    gaps = [{"after_time": left["time"], "before_time": right["time"],
             "missing_count": (right["time"] - left["time"]) // step - 1}
            for left, right in zip(points, points[1:], strict=False) if right["time"] - left["time"] > step]
    if gaps:
        flags.add("GAPS_PRESENT")
    invalid_flags = flags - {"LATEST_WINDOW_ONLY", "KNOWN_AT_COLLECTION_NOT_HISTORICAL_PERIOD",
                             "DUPLICATE_TIME_DEDUPLICATED", "OPEN_OR_FUTURE_PERIOD_EXCLUDED"}
    status = "PARTIAL" if invalid_flags else "READY"
    coverage, freshness = 0.0, None
    if points:
        expected = (points[-1]["time"] - points[0]["time"]) // step + 1
        coverage = len(points) / expected
        freshness = fetched_at - points[-1]["close_time"]
        if freshness > max(step * 2, 120):
            flags.add("SOURCE_STALE")
            status = "STALE"
    else:
        status = "EMPTY"
    result.update(points=points, gaps=gaps, coverage=coverage, freshness_seconds=freshness,
                  quality_flags=sorted(flags), status=status)
    return result


def _edge_price(edge: Any, timestamp: int) -> float | None:
    if not isinstance(edge, Mapping):
        return None
    first, last = edge.get("from"), edge.get("to")
    if not isinstance(first, Mapping) or not isinstance(last, Mapping):
        return None
    start, end = _epoch(first.get("time")), _epoch(last.get("time"))
    a, b = _number(first.get("price")), _number(last.get("price"))
    if start is None or end is None or a is None or b is None or a <= 0 or b <= 0:
        return None
    if end < start or end > timestamp or (end == start and a != b):
        return None
    projected = a if end == start else a + (b - a) * (timestamp - start) / (end - start)
    return projected if math.isfinite(projected) and projected > 0 else None


def _conditions(event: Mapping[str, Any], timestamp: int) -> dict[str, Any]:
    levels = event.get("levels") if isinstance(event.get("levels"), Mapping) else {}
    buffer = _number(levels.get("buffer"))
    buffer = buffer if buffer is not None and buffer >= 0 else None
    direction = {"bullish": 1, "bearish": -1}.get(event.get("signal_direction"), 0)
    if not direction and event.get("pattern") in {"double_top", "double_bottom"}:
        direction = -1 if event["pattern"] == "double_top" else 1
    boundaries = [(direction, "trigger")] if direction and "trigger" in levels else [
        (side, key) for side, key in ((1, "upper"), (-1, "lower")) if not direction or side == direction]
    confirmation = []
    for side, key in boundaries:
        projected = _edge_price(levels.get(key), timestamp)
        if projected is not None and buffer is not None and projected + side * buffer > 0:
            confirmation.append({"side": side, "price": projected + side * buffer,
                                 "projected_at": timestamp, "comparison": "CLOSE_ABOVE" if side > 0 else "CLOSE_BELOW"})
    side = _number(levels.get("invalid_side"))
    base = _edge_price(levels.get("invalid_edge"), timestamp) if "invalid_edge" in levels else _number(levels.get("invalid_price"))
    used_buffer = 0.0 if levels.get("hard") is True else buffer
    invalidation = None
    if side in {-1, 1} and base is not None and base > 0 and used_buffer is not None:
        price = base + side * used_buffer
        if math.isfinite(price) and price > 0:
            invalidation = {"price": price, "side": int(side), "projected_at": timestamp,
                            "mode": "WICK_INVALIDATION" if levels.get("hard") is True else "CLOSE_INVALIDATION"}
    return {"confirmation": confirmation, "confirmation_closes": levels.get("closes"),
            "invalidation": invalidation, "projected_at": timestamp}


def _scenario(scenario: Mapping[str, Any], event: Mapping[str, Any], conditions: dict[str, Any], assessed_at: int) -> dict[str, Any]:
    forecast = event.get("forecast") if isinstance(event.get("forecast"), Mapping) else {}
    if event.get("phase") not in {"FORMING", "FORMED", "CONFIRMED"}:
        raise ValueError("terminal structures cannot create active scenarios")
    entry = _number(scenario.get("entry"))
    lower = _number(scenario.get("target_lower"))
    upper = _number(scenario.get("target_upper"))
    if any(value is None or value <= 0 for value in (entry, lower, upper)) or lower > upper:
        raise ValueError("scenario prices must be finite, positive and ordered")
    next_pivot = event.get("phase") == "FORMING" or forecast.get("kind") == "NEXT_PIVOT"
    result: dict[str, Any] = {"kind": "NEXT_PIVOT_OBSERVATION" if next_pivot else "CONDITIONAL_MOVE_SCENARIO",
        "entry": entry, "target_lower": lower, "target_upper": upper, "issued_at": assessed_at,
        "source": "USER_SCENARIO_NOT_PROVIDER_PRICE_PREDICTION", "risk_reward": None,
        "auto_order": False, "costs_included": False, "parent_forecast_id": forecast.get("id")}
    if next_pivot:
        result.update(direction="neutral", target_label="下一拐点观察区，不是止盈区", status="WAIT_STRUCTURE")
        return result
    if forecast.get("kind") != "CONDITIONAL_MOVE":
        raise ValueError("structure has no conditional price forecast")
    direction_name = event.get("signal_direction")
    direction = {"bullish": 1, "bearish": -1}.get(direction_name, 0)
    if not direction or scenario.get("direction", direction_name) != direction_name:
        raise ValueError("scenario direction must match the conditional structure")
    stop = conditions.get("invalidation")
    if not stop or stop["side"] != -direction or direction * (entry - stop["price"]) <= 0:
        raise ValueError("scenario entry crosses the original invalidation")
    if "stop" in scenario and _number(scenario["stop"]) != stop["price"]:
        raise ValueError("scenario cannot move the original invalidation")
    if min(direction * (lower - entry), direction * (upper - entry)) <= 0:
        raise ValueError("scenario targets must remain on the profit side")
    ratios = sorted([direction * (target - entry) / (direction * (entry - stop["price"]))
                     for target in (lower, upper)])
    if not all(math.isfinite(value) and value > 0 for value in ratios):
        raise ValueError("scenario risk reward is not finite")
    result.update(direction=direction_name, invalidation=copy.deepcopy(stop),
                  risk_reward={"lower": ratios[0], "upper": ratios[1]}, target_label="独立条件目标区间", status="CONDITIONAL")
    return result


def build_review(
    event: Mapping[str, Any], bundle: Mapping[str, Any], *, assessed_at: int,
    decision_bar_time: int, scenario: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only present reassessment, explicitly not original-time evidence.

    The repository must resolve an authorized immutable candidate before this
    function. No market feature changes its geometry, phase or success rate.
    """
    if _epoch(assessed_at) is None or _epoch(decision_bar_time) is None or decision_bar_time > assessed_at:
        raise ValueError("invalid assessment decision time")
    event = copy.deepcopy(dict(event))
    bundle = copy.deepcopy(dict(bundle))
    forecast = event.get("forecast") if isinstance(event.get("forecast"), Mapping) else {}
    conditions = _conditions(event, decision_bar_time)
    reasons: list[str] = []
    match = bundle.get("match") if isinstance(bundle.get("match"), Mapping) else {}
    if match.get("status") != "EXACT":
        reasons.append("CONTRACT_NOT_EXACT")
    for dimension in ("instrument_id", "timeframe"):
        if event.get(dimension) is not None and event[dimension] != bundle.get(dimension):
            reasons.append("EVENT_" + dimension.upper() + "_MISMATCH")
    fetched_at = _epoch(bundle.get("fetched_at"))
    expires_at = _epoch(bundle.get("expires_at"))
    if fetched_at is None or fetched_at > assessed_at:
        reasons.append("SNAPSHOT_TIME_INVALID")
    if expires_at is None or expires_at <= assessed_at:
        reasons.append("SNAPSHOT_STALE")
    if bundle.get("status") not in {"READY", "PARTIAL"}:
        reasons.append("SNAPSHOT_NOT_USABLE")
    deadline = _epoch(forecast.get("deadline_time"))
    terminal = event.get("phase") not in {"FORMING", "FORMED", "CONFIRMED"} or (deadline is not None and deadline <= assessed_at)
    if terminal:
        reasons.append("STRUCTURE_TERMINATED_OR_EXPIRED")
    issued_at = _epoch(forecast.get("issued_at"))
    if issued_at is not None and issued_at > assessed_at:
        reasons.append("FORECAST_NOT_YET_KNOWN")
    evidence = []
    allow_evidence = not any(code in reasons for code in (
        "CONTRACT_NOT_EXACT", "EVENT_INSTRUMENT_ID_MISMATCH", "EVENT_TIMEFRAME_MISMATCH", "SNAPSHOT_TIME_INVALID"))
    for series in bundle.get("series", []) if isinstance(bundle.get("series"), list) and allow_evidence else []:
        if not isinstance(series, Mapping) or series.get("id") not in METRICS:
            continue
        if series.get("status") not in {"READY", "PARTIAL"}:
            continue
        # Progressive collection has distinct clocks per metric. A later
        # endpoint must not extend an earlier one's eligibility for research.
        # Legacy snapshots without series clocks retain their bundle clock.
        series_fetched_at = _epoch(series.get("fetched_at", bundle.get("fetched_at")))
        series_expires_at = _epoch(series.get("expires_at", bundle.get("expires_at")))
        if (series.get("native_timeframe", bundle.get("timeframe")) != bundle.get("timeframe")
                or series_fetched_at is None or series_fetched_at > assessed_at
                or series_expires_at is None or series_expires_at <= assessed_at):
            continue
        metric_id = series["id"]
        expected = METRICS[metric_id]
        if (series.get("unit") != expected[1] or series.get("unit_status") != "VERIFIED"
                or series.get("population") != expected[3]):
            continue
        rows = series.get("points") if isinstance(series.get("points"), list) else []
        # decision_bar_time is the latest finalized candle's OPEN timestamp;
        # no use of a provider period closing beyond that candle's close.
        step = TIMEFRAME_SECONDS.get(str(bundle.get("timeframe")), 0)
        visible_end = min(assessed_at, decision_bar_time + step)
        visible = []
        for point in rows:
            if not isinstance(point, Mapping):
                continue
            available = _epoch(point.get("available_at"))
            closed = _epoch(point.get("close_time"))
            timestamp = _epoch(point.get("time"))
            value = _number(point.get("value"))
            if (available is not None and available <= assessed_at and closed is not None and closed <= visible_end
                    and timestamp is not None and timestamp < closed and value is not None):
                visible.append({"time": timestamp, "close_time": closed, "available_at": available, "value": value})
        visible.sort(key=lambda point: point["time"])
        if not visible:
            continue
        last, first = visible[-1], visible[0]
        change = (last["value"] - first["value"]) / abs(first["value"]) * 100 if len(visible) > 1 and first["value"] else None
        notes = {"oi": "美元持仓估值变化含价格影响，不等于净多或净空资金流入",
                 "funding": "当前区间费率，不是未来价格预测或历史极端分位",
                 "lsr": "全账户数量之比，不是持仓金额之比或胜率",
                 "liquidation_long": "本期已发生多单爆仓，不是未来清算价格热力区",
                 "liquidation_short": "本期已发生空单爆仓，不是未来清算价格热力区"}
        evidence.append({"id": metric_id, "label": series.get("label", expected[0]), "unit": expected[1],
                         "population": expected[3], "latest": last, "window_start": first["time"],
                         "window_end": last["close_time"], "points_count": len(visible),
                         "window_change_percent": change if metric_id == "oi" else None,
                         "quality_flags": copy.deepcopy(series.get("quality_flags", [])), "note": notes[metric_id]})
    if not evidence:
        reasons.append("NO_USABLE_METRICS")
    next_pivot = event.get("phase") == "FORMING" or forecast.get("kind") == "NEXT_PIVOT"
    result: dict[str, Any] = {
        "assessment_version": REVIEW_VERSION, "version": REVIEW_VERSION,
        "candidate_id": event.get("candidate_id", event.get("id")), "evidence_id": event.get("evidence_id"),
        "instrument_id": bundle.get("instrument_id"), "timeframe": bundle.get("timeframe"),
        "snapshot_ids": [bundle["snapshot_id"]] if bundle.get("snapshot_id") else [],
        "assessed_at": assessed_at, "decision_bar_time": decision_bar_time,
        "basis": "CURRENT_REASSESSMENT_NOT_ORIGINAL_EVIDENCE", "state": "UNAVAILABLE" if reasons else "READY",
        "reason_codes": reasons, "scope": bundle.get("scope"), "match": match,
        "original": {"phase": event.get("phase"), "signal_direction": event.get("signal_direction"),
                     "confidence": event.get("confidence"), "forecast": forecast,
                     "initial_forecast": event.get("initial_forecast"), "recognition": event.get("recognition"),
                     "historical_reference": event.get("historical_reference")},
        "original_conditions": conditions, "metric_evidence": evidence,
        "observation_action": "WAIT_STRUCTURE" if next_pivot else "WAIT_GEOMETRIC_CONFIRMATION",
        "target_semantics": "NEXT_PIVOT_OBSERVATION_NOT_TAKE_PROFIT" if next_pivot else "ORIGINAL_CONDITIONAL_TARGET",
        "original_time_evidence": {"status": "NOT_SAVED", "note": "本次获取的历史不能补作原判断时已保存证据"},
        "adjusted_formation_probability": None, "adjusted_win_rate": None,
        "automatic_price_adjustment": False, "auto_order": False, "scenario": None,
    }
    if scenario is not None:
        if reasons:
            raise ValueError("unavailable or expired evidence cannot create a new scenario")
        if not isinstance(scenario, Mapping):
            raise ValueError("scenario must be an object")
        result["scenario"] = _scenario(scenario, event, conditions, assessed_at)
    result["assessment_id"] = canonical_hash(result)
    return result
