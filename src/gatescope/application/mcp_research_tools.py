"""Bounded research tool orchestration; no replay, orders or provider calls."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from math import isfinite
from abc import ABC, abstractmethod
from typing import Any, Callable
from gatescope.adapters.postgres_mcp import VerifiedMcpToken
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.indicators.registry import IndicatorRegistry
from uuid import UUID

from psycopg.errors import LockNotAvailable, QueryCanceled

from gatescope.domain.current_patterns import PHASES, validate_confidence_filter
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.indicators import IndicatorNode, compute_batch


def bounded(value: int, name: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")


def period(value: str) -> None:
    if not isinstance(value, str) or value not in TIMEFRAME_SECONDS:
        raise ValueError("unsupported timeframe")


def page(limit: int, offset: int, maximum: int = 50) -> None:
    bounded(limit, "limit", 1, maximum)
    bounded(offset, "offset", 0, 10_000)


def short_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip():
        raise ValueError(f"{name} must be a nonempty identifier of at most 128 characters")


def arguments(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key not in {"self", "principal"}}


class McpResearchTools(ABC):
    """Mixin sharing the authenticated/audited facade's dependencies and envelope."""

    market: PostgresMarketReadDatabase
    registry: IndicatorRegistry
    structures: Any
    research: Any

    @abstractmethod
    def market_analysis_snapshot(
        self, principal: VerifiedMcpToken, *, instrument_id: str | None = None,
        symbol: str | None = None, market: str | None = None, timeframe: str = "15m",
        indicator_ids: list[str] | None = None, bar_limit: int = 500,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def market_preview(
        self, tenant_id: UUID, user_id: UUID, *, instrument_id: str | None = None,
        symbol: str | None = None, market: str | None = None, timeframe: str = "15m",
        indicator_ids: list[str] | None = None, bar_limit: int = 500,
        request_coinglass: bool = True,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def _resolve_instrument(
        self, instrument_id: str | None, symbol: str | None, market: str | None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def _invoke(
        self,
        principal: VerifiedMcpToken,
        tool_name: str,
        required_scope: str | tuple[str, ...],
        arguments: dict[str, Any],
        function: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        ...

    @staticmethod
    @abstractmethod
    def _envelope(*, items: list[dict[str, Any]], **metadata: Any) -> dict[str, Any]:
        ...

    def _evidence(self, payload: dict[str, Any], **metadata: Any) -> dict[str, Any]:
        from gatescope.application.mcp_tools import _json_safe
        return self._envelope(**_json_safe({
            "items": [], **payload, **metadata, "research_only": True,
            "read_policy": "EXISTING_LOCAL_EVIDENCE_ONLY_NO_REPLAY_NO_ORDERS_NO_UPSTREAM",
        }))

    def _structure_reader(self):
        if self.structures is None:
            raise ValueError("structure evidence reader is unavailable")
        return self.structures

    def _research_reader(self):
        if self.research is None:
            raise ValueError("private research reader is unavailable")
        return self.research

    def structure_current(self, principal, *, instrument_id=None, symbol=None, market=None,
                          timeframe="15m", phase=None, family=None, pattern=None,
                          confidence_min=None, confidence_max=None, unscored_only=False,
                          limit=20, offset=0):
        args = arguments(locals())

        def run():
            period(timeframe)
            page(limit, offset)
            if phase is not None and phase not in PHASES:
                raise ValueError("unsupported structure phase")
            if family not in {None, "chart", "wave"}:
                raise ValueError("family must be chart or wave")
            if not isinstance(unscored_only, bool):
                raise ValueError("unscored_only must be boolean")
            validate_confidence_filter(confidence_min, confidence_max, unscored_only)
            if pattern is not None:
                short_id(pattern, "pattern")
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            data = self._structure_reader().current(UUID(instrument["id"]), timeframe,
                phase=phase, family=family, pattern=pattern, confidence_min=confidence_min,
                confidence_max=confidence_max, unscored_only=unscored_only, limit=limit, offset=offset)
            return self._evidence(data, instrument=instrument, timeframe=timeframe)

        return self._invoke(principal, "structure_current", "analysis:read", args, run)

    def structure_study(self, principal, *, instrument_id=None, symbol=None, market=None,
                        timeframe="15m", pattern=None):
        args = arguments(locals())

        def run():
            period(timeframe)
            if pattern is not None:
                short_id(pattern, "pattern")
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            data = self._structure_reader().study(UUID(instrument["id"]), timeframe, pattern=pattern)
            return self._evidence(data, instrument=instrument, timeframe=timeframe)

        return self._invoke(principal, "structure_study", "analysis:read", args, run)

    def structure_event_list(self, principal, *, instrument_id=None, symbol=None, market=None,
                             timeframe="15m", source="RECORDED", context_id=None,
                             phase=None, pattern=None, limit=20, offset=0):
        args = arguments(locals())

        def run():
            period(timeframe)
            page(limit, offset)
            if source not in {"RECORDED", "REPLAY"}:
                raise ValueError("source must be RECORDED or REPLAY")
            if phase is not None and phase not in PHASES:
                raise ValueError("unsupported structure phase")
            for value, name in ((context_id, "context_id"), (pattern, "pattern")):
                if value is not None:
                    short_id(value, name)
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            data = self._structure_reader().events(UUID(instrument["id"]), timeframe,
                source=source, context=context_id, phase=phase, pattern=pattern, limit=limit, offset=offset)
            return self._evidence(data, instrument=instrument, timeframe=timeframe)

        return self._invoke(principal, "structure_event_list", "analysis:read", args, run)

    def structure_event_detail(self, principal, *, context_id, candidate_id,
                               instrument_id=None, symbol=None, market=None, timeframe="15m",
                               transition_limit=20, transition_offset=0, bar_limit=300):
        args = arguments(locals())

        def run():
            period(timeframe)
            short_id(context_id, "context_id")
            short_id(candidate_id, "candidate_id")
            page(transition_limit, transition_offset)
            bounded(bar_limit, "bar_limit", 0, 500)
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            data = self._structure_reader().detail(UUID(instrument["id"]), timeframe, context_id,
                candidate_id, transition_limit=transition_limit, transition_offset=transition_offset,
                bar_limit=bar_limit)
            if data is None:
                raise LookupError("structure evidence not found for this instrument and timeframe")
            return self._evidence(data, instrument=instrument, timeframe=timeframe)

        return self._invoke(principal, "structure_event_detail", "analysis:read", args, run)

    def watchlist_list(self, principal, *, limit=50, offset=0):
        args = arguments(locals())

        def run():
            page(limit, offset, 100)
            return self._evidence(self._research_reader().watchlist(
                principal.tenant_id, principal.subject_user_id, limit=limit, offset=offset))

        return self._invoke(principal, "watchlist_list", "research:read", args, run)

    def research_backtest_list(self, principal, *, instrument_id=None, symbol=None, market=None,
                               timeframe=None, limit=20, offset=0):
        args = arguments(locals())

        def run():
            page(limit, offset)
            if timeframe is not None:
                period(timeframe)
            instrument = (self._resolve_instrument(instrument_id, symbol, market)
                          if any(value is not None for value in (instrument_id, symbol, market)) else None)
            return self._evidence(self._research_reader().backtests(
                principal.tenant_id, principal.subject_user_id,
                instrument=UUID(instrument["id"]) if instrument else None,
                timeframe=timeframe, limit=limit, offset=offset))

        return self._invoke(principal, "research_backtest_list", "research:read", args, run)

    def research_backtest_detail(self, principal, *, run_id, section="summary", limit=50, offset=0):
        args = arguments(locals())

        def run():
            from gatescope.application.mcp_tools import _uuid
            page(limit, offset, 100)
            if section not in {"summary", "trades", "equity", "validation", "cases", "events", "inputs"}:
                raise ValueError("unsupported backtest section")
            identifier = _uuid(run_id, "run_id")
            return self._evidence(self._research_reader().backtest_detail(
                principal.tenant_id, principal.subject_user_id, identifier,
                section=section, limit=limit, offset=offset))

        return self._invoke(principal, "research_backtest_detail", "research:read", args, run)

    def indicator_get_series(self, principal, *, instrument_id=None, symbol=None, market=None,
                             timeframe="15m", indicator_id="rsi", parameters=None,
                             bar_limit=500, limit=200, offset=0, end_epoch=None):
        args = arguments(locals())

        def run():
            from gatescope.application.mcp_tools import _bar_quality, _contiguous_suffix, _final_bars
            period(timeframe)
            bounded(bar_limit, "bar_limit", 50, 2000)
            page(limit, offset, 500)
            short_id(indicator_id, "indicator_id")
            definition = self.registry.definition(indicator_id)
            if parameters is not None and (not isinstance(parameters, dict) or len(parameters) > 8):
                raise ValueError("parameters must be a bounded indicator parameter object")
            normalized = definition.normalize_params(parameters or {})
            now = datetime.now(timezone.utc)
            if end_epoch is not None:
                bounded(end_epoch, "end_epoch", 0, int(now.timestamp()))
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            step = TIMEFRAME_SECONDS[timeframe]
            # Pin a calendar window for repeatable pagination. end_epoch is the
            # inclusive last candle OPEN time, not a historical knowledge cutoff.
            end = end_epoch if end_epoch is not None else int(now.timestamp()) // step * step - step
            start = max(0, end - (bar_limit - 1) * step)
            raw = self.market.get_canonical_candles(instrument["id"], timeframe, start, end, bar_limit)
            bars, flags = _final_bars(instrument, instrument["id"], timeframe, raw, now)
            quality = _bar_quality(bars, timeframe, now, flags)
            contiguous = _contiguous_suffix(bars)
            points = compute_batch(self.registry, [IndicatorNode(indicator_id, indicator_id, normalized)], contiguous)
            items = [{"open_time": p.open_time.isoformat(), "available_at": p.available_at.isoformat(),
                "bar_revision": p.bar_revision, "ready": p.ready,
                "values": {key: value if value is None or isfinite(value) else None
                           for key, value in p.values.items()}} for p in points[offset:offset + limit]]
            return self._evidence({"items": items, "version": "indicator-series-1", "instrument": instrument,
                "timeframe": timeframe, "indicator_id": indicator_id, "indicator_version": definition.version,
                "parameters": normalized, "warmup_bars": definition.warmup(normalized),
                "window_start_epoch": start, "window_end_epoch": end, "requested_bar_limit": bar_limit,
                "total": len(points), "limit": limit, "offset": offset,
                "has_more": offset + len(items) < len(points),
                "next_offset": offset + limit if offset + len(items) < len(points) else None,
                "quality": quality, "data_watermark": quality["data_watermark"],
                "quality_flags": quality["flags"], "order": "open_time ascending",
                "policies": {"indicators": "CONTIGUOUS_FINAL_SUFFIX_ONLY",
                    "pagination": "REUSE_WINDOW_END_EPOCH_AND_BAR_LIMIT_DATA_REVISIONS_MAY_CHANGE",
                    "historical_availability": "CURRENT_RECORDED_REVISIONS_NOT_POINT_IN_TIME_BACKTEST",
                    "probability": "INDICATOR_SERIES_IS_NOT_A_WIN_RATE"}})

        return self._invoke(principal, "indicator_get_series", ("market:read", "analysis:read"), args, run)

    def market_chart_snapshot(self, principal, *, instrument_id=None, symbol=None, market=None,
                              timeframe="15m", bar_limit=150, include_forming=False):
        args = arguments(locals())

        def run():
            from gatescope.application.mcp_tools import _bar_quality, _final_bars, _live_observation
            period(timeframe)
            bounded(bar_limit, "bar_limit", 30, 300)
            if not isinstance(include_forming, bool):
                raise ValueError("include_forming must be boolean")
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            captured = datetime.now(timezone.utc)
            raw = self.market.get_canonical_candles(instrument["id"], timeframe, None, None, bar_limit + 200)
            bars, flags = _final_bars(instrument, instrument["id"], timeframe, raw, captured)
            quality = _bar_quality(bars, timeframe, captured, flags)
            live = {"status": "NOT_REQUESTED", "candle": None}
            if include_forming:
                cached = self.market.cache_status(instrument["id"], timeframe)
                live = _live_observation(cached.get("live_bar"), instrument["id"], timeframe, captured)
            return self._chart_evidence(bars, instrument=instrument, timeframe=timeframe,
                captured=captured, quality=quality, bar_limit=bar_limit, include_forming=include_forming,
                live=live, input_policy="CLOSED_ONLY_CONTIGUOUS_SUFFIX_WITH_UP_TO_200_EXTRA_WARMUP_BARS")

        return self._invoke(principal, "market_chart_snapshot", ("market:read", "analysis:read"), args, run)

    def _chart_evidence(self, bars, *, instrument, timeframe, captured, quality, bar_limit,
                        include_forming=False, live=None, input_policy):
        """Build chart data without fetching or changing the supplied candle window."""
        from gatescope.application.mcp_tools import _contiguous_suffix, _mcp_bar
        live = live if live is not None else {"status": "NOT_REQUESTED", "candle": None}
        # Include canonical source identity, never infer an exchange from a symbol.
        chart_instrument = {**instrument, "venue": bars[-1].venue if bars else instrument.get("venue")}
        forming = live.get("candle") if live.get("status") == "CURRENT" else None
        # The display bound includes the optional live candle, never indicator input.
        displayed = bars[-(bar_limit - bool(forming)):]
        candles = [_mcp_bar(bar) for bar in displayed]
        if forming:
            candles.append(forming)
        contiguous = _contiguous_suffix(bars)
        nodes = [IndicatorNode("ema9", "ema", {"period": 9}),
                 IndicatorNode("ema21", "ema", {"period": 21}),
                 IndicatorNode("rsi14", "rsi", {"period": 14})]
        points = compute_batch(self.registry, nodes, contiguous)
        visible_times = {bar.open_time for bar in displayed}
        overlays: dict[str, list] = {node.instance_id: [] for node in nodes}
        for point in points:
            if point.open_time not in visible_times:
                continue
            value = point.values.get("ema" if point.indicator_id == "ema" else "rsi")
            overlays[point.instance_id].append({
                "open_time": point.open_time.isoformat(), "value": value,
                "ready": point.ready, "bar_revision": point.bar_revision,
                "available_at": point.available_at.isoformat(),
            })
        metadata = [{"instance_id": node.instance_id, "indicator_id": node.indicator_id,
            "parameters": dict(node.params), "version": self.registry.definition(node.indicator_id).version,
            "input_bars": len(contiguous)} for node in nodes]
        return self._evidence({"items": [], "version": "market-chart-1", "instrument": chart_instrument,
            "timeframe": timeframe, "captured_at": captured.isoformat(),
            "last_closed_at": bars[-1].close_time.isoformat() if bars else None,
            "includes_forming_candle": bool(forming), "forming_requested": include_forming,
            "forming_observation_status": live["status"], "candles": candles, "overlays": overlays,
            "indicators": metadata, "quality": quality, "quality_flags": quality["flags"],
            "data_watermark": quality["data_watermark"], "timezone": "UTC",
            "closed_bar_count": len(displayed), "requested_bar_limit": bar_limit,
            "status": ("STALE" if quality["stale"] else "READY") if displayed else "NO_DATA",
            "policies": {"indicator_input": input_policy,
                "image_basis": "EXACT_RETURNED_CANDLES_AND_OVERLAYS",
                "forming": "DISPLAY_ONLY_NOT_USED_FOR_INDICATORS_OR_PROBABILITIES",
                "volume_unit": "EXCHANGE_REPORTED_NOT_ASSUMED_USD_OR_BASE",
                "position_sizing": "OHLCV_IS_NOT_ACCOUNT_EQUITY_OR_CONTRACT_MULTIPLIER_DATA"}})

    def market_analysis_with_research(self, principal, *, instrument_id=None, symbol=None,
                                      market=None, timeframe="15m", indicator_ids=None, bar_limit=500):
        """Enrich the legacy tool without a second market read or new permission.

        Existing clients can receive chart and public structural evidence through
        their already-discovered tool. The original market result remains intact
        if optional evidence fails; authorization errors are never downgraded.
        """
        snapshot = self.market_analysis_snapshot(principal, instrument_id=instrument_id,
            symbol=symbol, market=market, timeframe=timeframe, indicator_ids=indicator_ids,
            bar_limit=bar_limit)
        result = {**snapshot, "version": "market-analysis-2",
                  "market_data_version": snapshot["version"]}
        instrument = snapshot["instrument"]
        try:
            captured = datetime.fromisoformat(snapshot["as_of"])
            bars: list[Bar] = []
            for candle in snapshot["candles"]:
                bar = Bar(**{key: candle[key] for key in (
                    "venue", "market", "instrument_id", "timeframe", "open", "high", "low",
                    "close", "volume", "is_final", "revision")},
                    open_time=datetime.fromisoformat(candle["open_time"]),
                    close_time=datetime.fromisoformat(candle["close_time"]),
                    available_at=datetime.fromisoformat(candle["available_at"]),
                    quality_flags=frozenset(candle["quality_flags"]))
                if (not bar.is_final or bar.instrument_id != str(instrument["id"])
                        or bar.timeframe != timeframe or bar.close_time > captured
                        or not bar.close_time <= bar.available_at <= captured
                        or (bars and bar.open_time < bars[-1].close_time)):
                    raise ValueError("legacy chart input is not the canonical closed window")
                bars.append(bar)
            chart = self._chart_evidence(bars, instrument=instrument, timeframe=timeframe,
                captured=captured, quality=snapshot["quality"], bar_limit=150,
                input_policy="SAME_MARKET_SNAPSHOT_CLOSED_CANDLES_CONTIGUOUS_SUFFIX_ONLY_NO_EXTRA_FETCH")
            input_count = chart["indicators"][0]["input_bars"]
            chart["source_window"] = {
                "source_field": "candles", "source_as_of": snapshot["as_of"],
                "source_closed_bars": len(bars), "displayed_closed_bars": chart["closed_bar_count"],
                "display_limit": 150, "display_truncated": len(bars) > 150,
                "indicator_input_bars": input_count,
                "indicator_input_start": bars[-input_count].open_time.isoformat() if input_count else None,
                "extra_candle_queries": 0,
            }
            result["chart"] = chart
        except PermissionError:
            raise
        except Exception:
            result["chart"] = {"status": "UNAVAILABLE", "reason": "CHART_EVIDENCE_UNAVAILABLE",
                "candles": [], "overlays": {}, "source_as_of": snapshot["as_of"]}
        try:
            result["structures"] = self.structure_current(principal, instrument_id=instrument["id"],
                timeframe=timeframe, limit=5, offset=0)
        except PermissionError:
            raise
        except Exception:
            result["structures"] = {"status": "UNAVAILABLE", "reason": "STRUCTURE_EVIDENCE_UNAVAILABLE",
                "items": [], "total": None, "formation_probability": None,
                "historical_win_rate": None}
        result["evidence_alignment"] = {
            "chart": "EXACT_SAME_MARKET_SNAPSHOT_CANDLES_REVISIONS_AND_CAPTURE_TIME",
            "structures": "SEPARATE_LOCAL_READ_CHECK_EACH_STRUCTURE_OBSERVED_AT_AND_QUALITY",
            "market_as_of": snapshot["as_of"],
            "structures_as_of": result["structures"].get("as_of"),
            "atomic_market_and_structures": False,
            "probabilities": "GEOMETRY_FORMATION_CONFIDENCE_AND_HISTORICAL_WIN_RATE_ARE_DISTINCT",
        }
        return result

    def market_research_bundle(self, principal, *, instrument_id=None, symbol=None, market=None,
                               timeframes=None, bar_limit=100, include_patterns=True):
        args = arguments(locals())

        def run():
            periods = ["1d", "4h", "1h", "15m"] if timeframes is None else timeframes
            if (not isinstance(periods, list) or not 1 <= len(periods) <= 4
                    or any(not isinstance(item, str) for item in periods) or len(set(periods)) != len(periods)):
                raise ValueError("timeframes must contain 1 to 4 unique supported periods")
            for value in periods:
                period(value)
            bounded(bar_limit, "bar_limit", 50, 500)
            if not isinstance(include_patterns, bool):
                raise ValueError("include_patterns must be boolean")
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            started, items = time.monotonic(), []
            for value in periods:
                if time.monotonic() - started >= 12:
                    items.append({"timeframe": value, "status": "NOT_READ", "reason": "BUNDLE_TIME_BUDGET"})
                    continue
                try:
                    snapshot = self.market_preview(principal.tenant_id, principal.subject_user_id,
                        instrument_id=instrument["id"], timeframe=value, bar_limit=bar_limit,
                        request_coinglass=False)
                except (QueryCanceled, LockNotAvailable):
                    items.append({"timeframe": value, "status": "NOT_READ", "reason": "DATA_QUERY_TIMEOUT"})
                    continue
                entry = {"timeframe": value, "status": "READ", "market": snapshot}
                if include_patterns:
                    if time.monotonic() - started >= 12:
                        entry["structures"] = {"status": "NOT_READ", "reason": "BUNDLE_TIME_BUDGET"}
                    else:
                        try:
                            entry["structures"] = self._structure_reader().current(
                                UUID(instrument["id"]), value, limit=5, offset=0)
                        except (QueryCanceled, LockNotAvailable):
                            entry["structures"] = {"status": "NOT_READ", "reason": "DATA_QUERY_TIMEOUT"}
                items.append(entry)
            return self._evidence({"items": items, "instrument": instrument, "version": "research-bundle-1",
                "requested_timeframes": periods, "elapsed_seconds": round(time.monotonic() - started, 3),
                "alignment_policy": "NON_ATOMIC_READ_EACH_COMPONENT_SCOPE_TIME_UNIT_AND_QUALITY",
                "budget_policy": "12_SECOND_SOFT_SCHEDULING_BUDGET_IN_FLIGHT_BOUNDED_QUERIES_MAY_FINISH_LATER",
                "pattern_page_limit": 5 if include_patterns else 0,
                "detail_tools": ["structure_current", "structure_event_detail", "structure_study", "coinglass_get_snapshot"],
                "probability_policy": "GEOMETRIC_SCORE_FORMATION_RATE_AND_NET_TRADE_WIN_RATE_ARE_DISTINCT"})

        return self._invoke(principal, "market_research_bundle", ("market:read", "analysis:read"), args, run)
