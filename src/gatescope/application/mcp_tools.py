"""Read-only MCP analysis facade with bounded results and immutable audit facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from statistics import stdev
import time
from typing import Any, Callable, Protocol
from uuid import UUID, uuid4

from psycopg.errors import LockNotAvailable, QueryCanceled

from gatescope.adapters.postgres_alert_rules import PostgresAlertRuleRepository
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_mcp import PostgresMcpRepository, VerifiedMcpToken
from gatescope.adapters.postgres_paper import PaperNotFound, PostgresPaperRepository
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.domain.coinglass_context import METRICS
from gatescope.indicators import IndicatorNode, compute_batch, default_registry
from gatescope.indicators.models import IndicatorPoint
from gatescope.application.mcp_research_tools import McpResearchTools

TIMEFRAMES = frozenset(TIMEFRAME_SECONDS)
MARKETS = frozenset({"all", "spot", "futures:usdt", "futures:btc"})
INDICATORS = frozenset(default_registry().identifiers())
DEFAULT_INDICATORS = ("sma", "ema", "rsi", "macd", "boll", "atr", "adx", "obv")
RESEARCH_NOTICE = (
    "Research-only historical/observational data. This is not an order instruction, "
    "does not guarantee profit, and cannot access the execution service."
)


class McpToolDenied(PermissionError):
    pass


class McpDataUnavailable(RuntimeError):
    pass


class CoinglassContextReader(Protocol):
    def get(
        self, tenant_id: UUID, user_id: UUID, instrument_id: UUID,
        timeframe: str, request: bool = False,
    ) -> dict[str, Any]: ...


class McpAnalysisService(McpResearchTools):
    def __init__(
        self,
        market: PostgresMarketReadDatabase,
        alerts: PostgresAlertRuleRepository,
        access: PostgresMcpRepository,
        paper: PostgresPaperRepository | None = None,
        coinglass: CoinglassContextReader | None = None,
        structures: Any = None,
        research: Any = None,
    ) -> None:
        self.market = market
        self.alerts = alerts
        self.access = access
        self.paper = paper
        self.coinglass = coinglass
        self.structures = structures
        self.research = research
        self.registry = default_registry()

    def search_instruments(
        self,
        principal: VerifiedMcpToken,
        *,
        query: str = "",
        market: str = "all",
        limit: int = 50,
    ) -> dict[str, Any]:
        arguments = {"query": query, "market": market, "limit": limit}

        def run() -> dict[str, Any]:
            if len(query) > 100 or market not in MARKETS or not 1 <= limit <= 100:
                raise ValueError("query, market or limit is outside the supported bounds")
            items = self.market.search_instruments(query, market, limit, 0)
            return self._envelope(items=items)

        return self._invoke(principal, "market_search_instruments", "market:read", arguments, run)

    def get_candles(
        self,
        principal: VerifiedMcpToken,
        *,
        instrument_id: str,
        timeframe: str = "15m",
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        arguments = {
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "from_epoch": from_epoch,
            "to_epoch": to_epoch,
            "limit": limit,
        }

        def run() -> dict[str, Any]:
            instrument = self.market.get_instrument(instrument_id)
            if instrument is None:
                raise ValueError("instrument does not exist")
            if timeframe not in TIMEFRAMES or not 1 <= limit <= 1_000:
                raise ValueError("timeframe or limit is outside the supported bounds")
            if from_epoch is not None and to_epoch is not None and from_epoch > to_epoch:
                raise ValueError("from_epoch must be no later than to_epoch")
            rows = self.market.get_canonical_candles(
                instrument_id, timeframe, from_epoch, to_epoch, limit,
            )
            as_of = datetime.now(timezone.utc)
            bars, flags = _final_bars(instrument, instrument_id, timeframe, rows, as_of)
            items = [_mcp_bar(bar) for bar in bars]
            quality = _bar_quality(bars, timeframe, as_of, flags)
            return self._envelope(
                items=items,
                instrument=instrument,
                timeframe=timeframe,
                data_watermark=quality["data_watermark"],
                quality_flags=quality["flags"],
                quality=quality,
                order="open_time ascending",
                selection="LATEST_PERIOD_BOUNDED" if from_epoch is None and to_epoch is None else "HISTORICAL_RANGE",
            )

        return self._invoke(principal, "market_get_candles", "market:read", arguments, run)

    def indicator_snapshot(
        self,
        principal: VerifiedMcpToken,
        *,
        instrument_id: str,
        timeframe: str = "15m",
        indicator_ids: list[str] | None = None,
        bar_limit: int = 500,
    ) -> dict[str, Any]:
        requested = list(DEFAULT_INDICATORS) if indicator_ids is None else indicator_ids
        arguments = {
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "indicator_ids": requested,
            "bar_limit": bar_limit,
        }

        def run() -> dict[str, Any]:
            instrument = self.market.get_instrument(instrument_id)
            if instrument is None:
                raise ValueError("instrument does not exist")
            _validate_snapshot(timeframe, requested, bar_limit)
            raw = self.market.get_canonical_candles(
                instrument_id, timeframe, None, None, bar_limit,
            )
            as_of = datetime.now(timezone.utc)
            bars, flags = _final_bars(instrument, instrument_id, timeframe, raw, as_of)
            quality = _bar_quality(bars, timeframe, as_of, flags)
            return self._envelope(
                items=self._indicators(bars, requested),
                instrument=instrument,
                timeframe=timeframe,
                bars_evaluated=len(bars),
                data_watermark=quality["data_watermark"],
                quality_flags=quality["flags"],
                quality=quality,
                indicator_policy="CONTIGUOUS_FINAL_SUFFIX_ONLY",
            )

        return self._invoke(principal, "indicator_get_snapshot", "analysis:read", arguments, run)

    def market_analysis_snapshot(
        self, principal: VerifiedMcpToken, *, instrument_id: str | None = None,
        symbol: str | None = None, market: str | None = None, timeframe: str = "15m",
        indicator_ids: list[str] | None = None, bar_limit: int = 500,
    ) -> dict[str, Any]:
        requested = list(DEFAULT_INDICATORS) if indicator_ids is None else indicator_ids
        arguments = {
            "instrument_id": instrument_id, "symbol": symbol, "market": market,
            "timeframe": timeframe, "indicator_ids": requested, "bar_limit": bar_limit,
        }

        def run() -> dict[str, Any]:
            return self.market_preview(
                principal.tenant_id, principal.subject_user_id, instrument_id=instrument_id,
                symbol=symbol, market=market, timeframe=timeframe, indicator_ids=requested, bar_limit=bar_limit,
            )

        return self._invoke(
            principal, "market_analysis_snapshot", ("market:read", "analysis:read"), arguments, run,
        )

    def market_preview(
        self, tenant_id: UUID, user_id: UUID, *, instrument_id: str | None = None,
        symbol: str | None = None, market: str | None = None, timeframe: str = "15m",
        indicator_ids: list[str] | None = None, bar_limit: int = 500,
        request_coinglass: bool = True,
    ) -> dict[str, Any]:
        """Shared read projection. Caller must authenticate a session or MCP token.

        Not registered as an MCP tool. Web preview must not mint a synthetic MCP
        identity or append an audit row referencing a nonexistent token/client.
        """
        requested = list(DEFAULT_INDICATORS) if indicator_ids is None else indicator_ids
        _validate_snapshot(timeframe, requested, bar_limit)
        instrument = self._resolve_instrument(instrument_id, symbol, market)
        identifier = str(instrument["id"])
        # Each provider has a distinct clock; this is not an atomic snapshot.
        as_of = datetime.now(timezone.utc)
        raw = self.market.get_canonical_candles(identifier, timeframe, None, None, bar_limit)
        bars, flags = _final_bars(instrument, identifier, timeframe, raw, as_of)
        quality = _bar_quality(bars, timeframe, as_of, flags)
        cache = self.market.cache_status(identifier, timeframe)
        realtime = _live_observation(cache.get("live_bar"), identifier, timeframe, as_of)
        context = self._coinglass(tenant_id, user_id, identifier, timeframe, request_refresh=request_coinglass)
        return self._envelope(
            items=[], version="market-analysis-1", as_of=as_of.isoformat(), instrument=instrument, timeframe=timeframe,
            requested_bar_limit=bar_limit, window_policy="LATEST_PERIOD_BOUNDED_NO_BACKFILL_ACROSS_GAPS",
            candles=[_mcp_bar(bar) for bar in bars], latest_closed=_mcp_bar(bars[-1]) if bars else None,
            realtime=realtime, indicators=self._indicators(bars, requested),
            statistics=_window_statistics(bars, timeframe), coinglass=context,
            quality=quality, quality_flags=quality["flags"], data_watermark=quality["data_watermark"],
            cache=_json_safe({key: cache.get(key) for key in (
                "active", "history_status", "cached_bars", "target_bars", "missing_bars",
                "coverage_ratio", "stale", "last_live_at", "last_close_time",
            )}),
            policies={
                "indicators": "CONTIGUOUS_FINAL_SUFFIX_ONLY",
                "realtime": "DISPLAY_ONLY_NOT_USED_FOR_INDICATORS_OR_STATISTICS",
                "coinglass": ("AUTHORIZED_USER_CACHE_WITH_ASYNC_REFRESH_NO_INLINE_UPSTREAM" if request_coinglass
                              else "AUTHORIZED_USER_CACHE_ONLY_NO_REFRESH"),
                "alignment": "READ_EACH_SOURCE_SCOPE_UNIT_TIME_AND_QUALITY_BEFORE_COMBINING",
                "historical_availability": "TODAY_FETCHED_HISTORY_IS_NOT_EVIDENCE_AVAILABLE_IN_THE_PAST",
                "strategy_win_rate": "NOT_ESTIMATED_FROM_PRICE_DIRECTION_OR_INDICATOR_VALUES",
            },
        )

    def coinglass_get_snapshot(
        self, principal: VerifiedMcpToken, *, instrument_id: str | None = None,
        symbol: str | None = None, market: str | None = None, timeframe: str = "15m",
        wait_seconds: int = 15,
    ) -> dict[str, Any]:
        arguments = {"instrument_id": instrument_id, "symbol": symbol, "market": market,
                     "timeframe": timeframe, "wait_seconds": wait_seconds}

        def run() -> dict[str, Any]:
            if timeframe not in TIMEFRAMES:
                raise ValueError("timeframe is outside the supported bounds")
            if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 15:
                raise ValueError("wait_seconds must be an integer between 0 and 15")
            instrument = self._resolve_instrument(instrument_id, symbol, market)
            context = self._coinglass(principal.tenant_id, principal.subject_user_id,
                                     str(instrument["id"]), timeframe, wait_seconds=wait_seconds)
            return self._envelope(
                items=context.get("series", []), instrument=instrument, timeframe=timeframe,
                coinglass=context, data_watermark=context.get("fetched_at"),
                market_period_as_of=context.get("as_of"),
                quality_flags=[f"COINGLASS_{context.get('status', 'NO_DATA')}"],
                policy="AUTHORIZED_USER_CACHE_WITH_ASYNC_REFRESH_NO_INLINE_UPSTREAM",
            )

        return self._invoke(principal, "coinglass_get_snapshot", "analysis:read", arguments, run)

    def _resolve_instrument(
        self, instrument_id: str | None, symbol: str | None, market: str | None,
    ) -> dict[str, Any]:
        if instrument_id is not None:
            if symbol is not None or market is not None:
                raise ValueError("use instrument_id OR exact symbol plus market, not both")
            identifier = str(_uuid(instrument_id, "instrument_id"))
            instrument = self.market.get_instrument(identifier)
            if instrument is None:
                raise ValueError("instrument does not exist")
            return instrument
        if not symbol or len(symbol) > 100 or symbol.strip() != symbol or market not in MARKETS - {"all"}:
            raise ValueError("provide instrument_id or an exact symbol plus spot/futures:usdt/futures:btc market")
        expected_market = market.replace(":", "_")
        matches = [item for item in self.market.search_instruments(symbol, market, 100, 0)
                   if str(item["symbol"]).casefold() == symbol.casefold()
                   and str(item["market"]).casefold() == expected_market]
        if len(matches) != 1:
            raise ValueError("symbol is missing or ambiguous; search and specify an instrument_id")
        return matches[0]

    def _coinglass(
        self, tenant_id: UUID, user_id: UUID, identifier: str, timeframe: str, *, wait_seconds: int = 0,
        request_refresh: bool = True,
    ) -> dict[str, Any]:
        if self.coinglass is None:
            return {"version": "coinglass-context-1", "status": "NO_DATA", "series": [],
                    "quality": {"flags": ["CACHE_READER_UNAVAILABLE"]}}
        started = time.monotonic()
        raw = self.coinglass.get(
            tenant_id, user_id, UUID(identifier), timeframe, request=request_refresh,
        )
        # Wait only for this user's local cache: never call a provider, retain a
        # database transaction while sleeping, or keep re-enqueuing the job.
        reads = 1
        context = _context_projection(raw)
        for _ in range(wait_seconds):
            availability = _coinglass_availability(context, identifier, timeframe)
            remaining = wait_seconds - (time.monotonic() - started)
            state = context.get("refresh_state")
            if availability["available_metric_ids"] or remaining <= 0:
                break
            if state not in {"QUEUED", "COLLECTING", "SCHEDULED"}:
                break
            if (_context_epoch(context.get("retry_at")) > time.time()
                    or (state == "SCHEDULED" and _context_epoch(context.get("next_eligible_at")) > time.time() + remaining)):
                break
            time.sleep(min(1.0, remaining))
            if time.monotonic() - started >= wait_seconds:
                break
            raw = self.coinglass.get(tenant_id, user_id, UUID(identifier), timeframe, request=False)
            reads += 1
            context = _context_projection(raw)
        context["availability"] = _coinglass_availability(context, identifier, timeframe)
        context["cache_wait"] = {
            "budget_seconds": wait_seconds, "waited_seconds": round(time.monotonic() - started, 3),
            "cache_reads": reads, "inline_provider_calls": 0,
        }
        # Queue a deduplicated, owner-scoped background refresh when needed.
        # This read path never decrypts credentials or calls the provider inline;
        # the repository preserves the worker's retry_at and lease boundaries.
        # Cache contract only. Never expose provider credentials/configuration,
        # account details, quota or raw gateway responses through MCP.
        return context

    def _indicators(self, bars: list[Bar], requested: list[str]) -> list[dict[str, Any]]:
        contiguous = _contiguous_suffix(bars)
        points = compute_batch(self.registry, [IndicatorNode(item, item, {}) for item in requested], contiguous)
        latest: dict[str, IndicatorPoint] = {}
        for sample in points:
            previous = latest.get(sample.instance_id)
            if previous is None or sample.open_time > previous.open_time:
                latest[sample.instance_id] = sample
        items = []
        for identifier in requested:
            definition = self.registry.definition(identifier)
            parameters = definition.normalize_params({})
            point = latest.get(identifier)
            values = dict(point.values) if point else {output: None for output in definition.outputs}
            values = {key: value if value is None or isfinite(value) else None for key, value in values.items()}
            items.append({
                "indicator_id": identifier, "version": definition.version, "parameters": parameters,
                "warmup_bars": definition.warmup(parameters), "bars_evaluated": len(contiguous),
                "ready": bool(point and point.ready and all(value is not None for value in values.values())),
                "values": values, "open_time": point.open_time.isoformat() if point else None,
                "available_at": point.available_at.isoformat() if point else None,
                "bar_revision": point.bar_revision if point else None,
                "window_start": contiguous[0].open_time.isoformat() if contiguous else None,
                "is_final": True,
            })
        return items

    def list_alert_rules(
        self, principal: VerifiedMcpToken, *, limit: int = 50,
    ) -> dict[str, Any]:
        arguments = {"limit": limit}

        def run() -> dict[str, Any]:
            if not 1 <= limit <= 100:
                raise ValueError("limit is outside the supported bounds")
            rules = self.alerts.list(
                principal.tenant_id, principal.subject_user_id, limit=limit,
            )
            items = [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "status": item.status,
                    "etag": item.etag,
                    "current_version": item.current_version,
                    "condition": dict(item.condition),
                    "routing": dict(item.routing),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in rules
            ]
            return self._envelope(items=items)

        return self._invoke(principal, "alert_rule_list", "alerts:read", arguments, run)

    def list_signals(
        self, principal: VerifiedMcpToken, *, limit: int = 50,
    ) -> dict[str, Any]:
        arguments = {"limit": limit}

        def run() -> dict[str, Any]:
            if not 1 <= limit <= 100:
                raise ValueError("limit is outside the supported bounds")
            paper = self._paper()
            signals = paper.list_signals(
                principal.tenant_id, principal.subject_user_id, limit=limit,
            )
            items = [
                {
                    "id": str(item.id),
                    "instrument_id": str(item.instrument_id),
                    "symbol": item.symbol,
                    "timeframe": item.timeframe,
                    "strategy_version_ref": item.strategy_version_ref,
                    "evidence_tier": item.evidence_tier,
                    "eligibility_status": item.eligibility_status,
                    "side": item.side,
                    "entry_reference": str(item.entry_reference),
                    "stop_price": str(item.stop_price),
                    "take_profit_price": str(item.take_profit_price),
                    "time_stop_bars": item.time_stop_bars,
                    "metrics": dict(item.metrics),
                    "reason_codes": list(item.reason_codes),
                    "decision_at": item.decision_at.isoformat(),
                    "data_watermark": item.data_watermark.isoformat(),
                    "evidence_digest": item.evidence_digest,
                }
                for item in signals
            ]
            watermark = max((item.data_watermark for item in signals), default=None)
            return self._envelope(
                items=items,
                data_watermark=watermark.isoformat() if watermark else None,
                quality_flags=[],
            )

        return self._invoke(principal, "signal_list", "analysis:read", arguments, run)

    def explain_signal(
        self, principal: VerifiedMcpToken, *, signal_id: str,
    ) -> dict[str, Any]:
        arguments = {"signal_id": signal_id}

        def run() -> dict[str, Any]:
            identifier = _uuid(signal_id, "signal_id")
            paper = self._paper()
            signal = paper.get_signal(principal.tenant_id, principal.subject_user_id, identifier)
            gates = paper.signal_gates(principal.tenant_id, principal.subject_user_id, identifier)
            chart = paper.signal_chart(principal.tenant_id, principal.subject_user_id, identifier)
            return self._envelope(
                items=[
                    {
                        "id": str(signal.id),
                        "symbol": signal.symbol,
                        "timeframe": signal.timeframe,
                        "strategy_version_ref": signal.strategy_version_ref,
                        "policy_version": signal.policy_version,
                        "evidence_tier": signal.evidence_tier,
                        "eligibility_status": signal.eligibility_status,
                        "side": signal.side,
                        "entry_reference": str(signal.entry_reference),
                        "stop_price": str(signal.stop_price),
                        "take_profit_price": str(signal.take_profit_price),
                        "time_stop_bars": signal.time_stop_bars,
                        "metrics": dict(signal.metrics),
                        "assumptions": list(signal.assumptions),
                        "reason_codes": list(signal.reason_codes),
                        "indicator_snapshot": dict(signal.indicator_snapshot),
                        "gates": [_json_safe(item) for item in gates],
                        "bar_manifest": [_json_safe(item) for item in chart["bars"]],
                        "evidence_digest": signal.evidence_digest,
                        "decision_at": signal.decision_at.isoformat(),
                        "data_watermark": signal.data_watermark.isoformat(),
                        "history_policy": "exact frozen bar revisions; later prices cannot rewrite this evidence",
                    }
                ],
                data_watermark=signal.data_watermark.isoformat(),
                quality_flags=list(chart["quality_flags"]),
            )

        return self._invoke(principal, "signal_explain", "analysis:read", arguments, run)

    def get_trade_status(
        self, principal: VerifiedMcpToken, *, public_trade_id: str,
    ) -> dict[str, Any]:
        arguments = {"public_trade_id": public_trade_id}

        def run() -> dict[str, Any]:
            if len(public_trade_id) != 32 or not public_trade_id.startswith("paper_"):
                raise ValueError("public_trade_id must be a PAPER trade id")
            item = self._paper().get_trade(
                principal.tenant_id, principal.subject_user_id, public_trade_id,
            )
            return self._envelope(
                items=[
                    {
                        "public_trade_id": item.public_trade_id,
                        "signal_evidence_snapshot_id": str(item.signal_evidence_snapshot_id),
                        "symbol": item.symbol,
                        "timeframe": item.timeframe,
                        "environment": item.environment,
                        "side": item.side,
                        "exact_terms": dict(item.exact_terms),
                        "exact_terms_digest": item.exact_terms_digest,
                        "state": item.state,
                        "close_reason": item.close_reason,
                        "entry_fill_price": str(item.entry_fill_price),
                        "exit_fill_price": str(item.exit_fill_price) if item.exit_fill_price else None,
                        "quantity": str(item.quantity),
                        "fees": str(item.fees),
                        "net_pnl": str(item.net_pnl) if item.net_pnl is not None else None,
                        "evaluated_bars": item.evaluated_bars,
                        "protection_status": item.protection_status,
                        "data_watermark": item.last_evaluated_available_at.isoformat(),
                        "simulation": True,
                        "live_eligible": False,
                    }
                ],
                data_watermark=item.last_evaluated_available_at.isoformat(),
                quality_flags=[],
            )

        return self._invoke(principal, "trade_get_status", "analysis:read", arguments, run)

    def get_trade_timeline(
        self, principal: VerifiedMcpToken, *, public_trade_id: str,
    ) -> dict[str, Any]:
        arguments = {"public_trade_id": public_trade_id}

        def run() -> dict[str, Any]:
            if len(public_trade_id) != 32 or not public_trade_id.startswith("paper_"):
                raise ValueError("public_trade_id must be a PAPER trade id")
            items = self._paper().trade_timeline(
                principal.tenant_id, principal.subject_user_id, public_trade_id,
            )
            return self._envelope(
                items=[_json_safe(item) for item in items],
                immutable=True,
                chain_order="sequence ascending",
            )

        return self._invoke(principal, "trade_get_timeline", "analysis:read", arguments, run)

    def _invoke(
        self,
        principal: VerifiedMcpToken,
        tool_name: str,
        required_scope: str | tuple[str, ...],
        arguments: dict[str, Any],
        function: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        trace_id = str(uuid4())
        scopes = (required_scope,) if isinstance(required_scope, str) else required_scope
        missing = [scope for scope in scopes if scope not in principal.scopes]
        if missing:
            self.access.audit_invocation(
                principal,
                tool_name=tool_name,
                arguments=arguments,
                outcome="DENIED",
                safe_error_code="INSUFFICIENT_SCOPE",
                trace_id=trace_id,
            )
            raise McpToolDenied(f"scope {', '.join(missing)} is required")
        try:
            result = function()
        except (QueryCanceled, LockNotAvailable):
            self.access.audit_invocation(
                principal, tool_name=tool_name, arguments=arguments, outcome="ERROR",
                safe_error_code="DATA_QUERY_TIMEOUT", trace_id=trace_id,
            )
            raise McpDataUnavailable("Cached market query timed out; retry after five seconds") from None
        except LookupError:
            self.access.audit_invocation(
                principal, tool_name=tool_name, arguments=arguments, outcome="ERROR",
                safe_error_code="EVIDENCE_NOT_FOUND", trace_id=trace_id,
            )
            raise McpDataUnavailable("Evidence not found or not accessible to this user") from None
        except (ValueError, TypeError, PaperNotFound):
            self.access.audit_invocation(
                principal,
                tool_name=tool_name,
                arguments=arguments,
                outcome="ERROR",
                safe_error_code="INVALID_ARGUMENT",
                trace_id=trace_id,
            )
            raise
        self.access.audit_invocation(
            principal,
            tool_name=tool_name,
            arguments=arguments,
            outcome="SUCCESS",
            trace_id=trace_id,
        )
        result["trace_id"] = trace_id
        return result

    def _paper(self) -> PostgresPaperRepository:
        if self.paper is None:
            raise ValueError("PAPER evidence repository is unavailable")
        return self.paper

    @staticmethod
    def _envelope(*, items: list[dict[str, Any]], **metadata: Any) -> dict[str, Any]:
        return {
            "items": items,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "research_only": True,
            "usage_notice": RESEARCH_NOTICE,
            **metadata,
        }


def _canonical_bar(
    instrument: dict[str, Any], instrument_id: str, timeframe: str, row: dict[str, Any],
) -> Bar:
    opened = datetime.fromtimestamp(int(row["ts"]), tz=timezone.utc)
    closed = opened + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    updated = datetime.fromtimestamp(int(row["updated_at"]), tz=timezone.utc)
    legacy = any(key not in row for key in ("venue", "market", "revision", "quality_flags", "is_final"))
    quality_flags = set(row.get("quality_flags", []))
    if legacy:
        quality_flags.update({"AVAILABLE_AT_UNKNOWN", "LEGACY_FINALITY_ASSUMED", "LEGACY_SOURCE"})
    return Bar(
        venue=str(row.get("venue", "GATE")),
        market=str(row.get("market", instrument["market"])).upper(),
        instrument_id=instrument_id,
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
        is_final=bool(row.get("is_final", True)),
        revision=int(row.get("revision", 1)),
        available_at=max(closed, updated) if legacy else updated,
        quality_flags=frozenset(quality_flags),
    )


def _mcp_bar(bar: Bar) -> dict[str, Any]:
    return {
        "venue": bar.venue,
        "market": bar.market,
        "instrument_id": bar.instrument_id,
        "timeframe": bar.timeframe,
        "open_time": bar.open_time.isoformat(),
        "close_time": bar.close_time.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "is_final": bar.is_final,
        "revision": bar.revision,
        "available_at": bar.available_at.isoformat(),
        "quality_flags": sorted(bar.quality_flags),
        "live_eligible": False,
    }


def _validate_snapshot(timeframe: str, requested: list[str], bar_limit: int) -> None:
    if timeframe not in TIMEFRAMES or isinstance(bar_limit, bool) or not 50 <= bar_limit <= 2_000:
        raise ValueError("timeframe or bar_limit is outside the supported bounds")
    if not 1 <= len(requested) <= 8 or len(set(requested)) != len(requested):
        raise ValueError("indicator_ids must contain between one and eight unique values")
    if not set(requested) <= INDICATORS:
        raise ValueError("indicator_ids contains an unknown indicator")


def _final_bars(
    instrument: dict[str, Any], instrument_id: str, timeframe: str,
    rows: list[dict[str, Any]], as_of: datetime,
) -> tuple[list[Bar], set[str]]:
    flags: set[str] = set()
    by_time: dict[datetime, Bar] = {}
    for row in rows:
        bar = _canonical_bar(instrument, instrument_id, timeframe, row)
        if not bar.is_final:
            flags.add("NON_FINAL_CANDLE_EXCLUDED")
            continue
        if bar.close_time > as_of or bar.available_at > as_of:
            flags.add("FUTURE_CANDLE_EXCLUDED")
            continue
        if bar.available_at < bar.close_time:
            flags.add("INVALID_AVAILABILITY_EXCLUDED")
            continue
        if bar.market != str(instrument["market"]).upper() or bar.venue.upper() != "GATE":
            flags.add("DIMENSION_MISMATCH_EXCLUDED")
            continue
        previous = by_time.get(bar.open_time)
        if previous is not None:
            flags.add("DUPLICATE_REVISION_REDUCED")
        if previous is None or bar.revision > previous.revision:
            by_time[bar.open_time] = bar
    return sorted(by_time.values(), key=lambda bar: bar.open_time), flags


def _contiguous_suffix(bars: list[Bar]) -> list[Bar]:
    start = 0
    for index in range(1, len(bars)):
        if bars[index].open_time != bars[index - 1].close_time:
            start = index
    return bars[start:]


def _bar_quality(
    bars: list[Bar], timeframe: str, as_of: datetime, extra_flags: set[str],
) -> dict[str, Any]:
    flags = set(extra_flags)
    for bar in bars:
        flags.update(bar.quality_flags)
    step = TIMEFRAME_SECONDS[timeframe]
    gaps = []
    for left, right in zip(bars, bars[1:], strict=False):
        if right.open_time != left.close_time:
            gaps.append({
                "from": left.close_time.isoformat(), "to": right.open_time.isoformat(),
                "missing_bars": max(0, int((right.open_time - left.close_time).total_seconds() // step)),
            })
    if gaps:
        flags.add("NON_CONTIGUOUS_WINDOW")
    age = max(0, (as_of - bars[-1].close_time).total_seconds()) if bars else None
    stale = age is None or age > max(90, step * 2)
    if not bars:
        flags.add("NO_CLOSED_CANDLES")
    elif stale:
        flags.add("STALE_CLOSED_CANDLES")
    return {
        "flags": sorted(flags), "closed_bar_count": len(bars), "gap_count": len(gaps),
        "missing_bars": sum(gap["missing_bars"] for gap in gaps), "gaps": gaps,
        "contiguous_indicator_bars": len(_contiguous_suffix(bars)), "stale": stale,
        "latest_close_age_seconds": age,
        "data_watermark": max((bar.available_at for bar in bars)).isoformat() if bars else None,
        "window_start": bars[0].open_time.isoformat() if bars else None,
        "window_end": bars[-1].close_time.isoformat() if bars else None,
    }


def _window_statistics(bars: list[Bar], timeframe: str) -> dict[str, Any]:
    adjacent = [(left, right) for left, right in zip(bars, bars[1:], strict=False) if left.close_time == right.open_time]
    returns = [float((right.close / left.close - 1) * 100) for left, right in adjacent]
    return {
        "timeframe": timeframe, "bar_count": len(bars), "return_sample_count": len(returns),
        "window_start": bars[0].open_time.isoformat() if bars else None,
        "window_end": bars[-1].close_time.isoformat() if bars else None,
        "close_to_close_change_percent": float((bars[-1].close / bars[0].close - 1) * 100) if bars else None,
        "high": str(max(bar.high for bar in bars)) if bars else None,
        "low": str(min(bar.low for bar in bars)) if bars else None,
        "volume_sum": str(sum((bar.volume for bar in bars), Decimal(0))) if bars else None,
        "volume_unit": "EXCHANGE_REPORTED_VOLUME_NOT_ASSUMED_USD_OR_BASE",
        "return_sample_stddev_percent": stdev(returns) if len(returns) > 1 else None,
        "return_policy": "ADJACENT_FINAL_CLOSE_TO_CLOSE_ONLY_GAPS_EXCLUDED",
        "strategy_win_rate": None,
    }


def _live_observation(
    raw: dict[str, Any] | None, instrument_id: str, timeframe: str, as_of: datetime,
) -> dict[str, Any]:
    empty = {"status": "NO_CURRENT_OBSERVATION", "candle": None, "used_for_indicators": False}
    if not raw or raw.get("is_final") is not False:
        return empty
    try:
        opened = datetime.fromisoformat(str(raw["open_time"]))
        closed = datetime.fromisoformat(str(raw["close_time"]))
        available = datetime.fromisoformat(str(raw["available_at"]))
        if not (opened <= as_of < closed and available <= as_of):
            return empty
        if (closed - opened).total_seconds() != TIMEFRAME_SECONDS[timeframe]:
            return empty
        prices = {key: Decimal(str(raw[key])) for key in ("open", "high", "low", "close", "volume")}
        if not all(value.is_finite() for value in prices.values()):
            return empty
        if min(prices[key] for key in ("open", "high", "low", "close")) <= 0 or prices["volume"] < 0:
            return empty
        if prices["high"] < max(prices[key] for key in ("open", "close", "low")):
            return empty
        if prices["low"] > min(prices[key] for key in ("open", "close", "high")):
            return empty
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return empty
    age = (as_of - available).total_seconds()
    return {
        "status": "STALE" if age > 45 else "CURRENT", "observation_age_seconds": age,
        "used_for_indicators": False,
        "candle": {"instrument_id": instrument_id, "timeframe": timeframe,
                   "open_time": opened.isoformat(), "close_time": closed.isoformat(),
                   "available_at": available.isoformat(), **{key: str(value) for key, value in prices.items()},
                   "is_final": False, "revision": 0, "quality_flags": ["FORMING_CANDLE", "DISPLAY_ONLY"]},
    }


def _context_projection(raw: dict[str, Any]) -> dict[str, Any]:
    result = {key: _public_context_value(raw[key]) for key in (
        "version", "status", "scope", "match", "snapshot_id", "as_of", "fetched_at", "expires_at", "quality",
        "quality_flags", "history_scope", "reason", "retry_after_seconds", "automatic_feature_eligible",
        "automatic_feature_reason", "instrument_id", "timeframe",
        "safe_errors", "retry_at", "last_attempt_at", "refresh_status", "cache_retained",
        "next_refresh_at", "refresh_requested", "last_refresh_status", "last_safe_errors", "last_reason",
        "refresh_state", "queued_at", "queue_age_seconds", "collection_started_at", "next_eligible_at",
        "next_poll_after_seconds", "cache_available", "cached_points_count", "last_success_at",
    ) if key in raw}
    result["series"] = [{key: _public_context_value(item[key]) for key in (
        "id", "label", "unit", "population", "native_timeframe", "funding_interval", "quality",
        "points", "status", "scope", "source", "coverage", "gaps",
        "quality_flags", "unit_status", "unit_source", "funding_interval_seconds", "freshness_seconds",
        "fetched_at", "expires_at", "as_of", "cache_retained", "refresh_status",
    ) if key in item} for item in raw.get("series", [])[:16]]
    return _json_safe(result)


def _context_epoch(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return 0.0
    return max(0.0, float(value))


def _coinglass_availability(context: dict[str, Any], instrument_id: str, timeframe: str) -> dict[str, Any]:
    """Give consumers per-metric availability, not an all-or-nothing queue label."""
    series = {item.get("id"): item for item in context.get("series", []) if isinstance(item, dict)}
    metrics: list[dict] = []
    available: list[str] = []
    fresh: list[str] = []
    stale: list[str] = []
    scope = context.get("scope")
    exact = ((context.get("match") or {}).get("status") == "EXACT"
             and isinstance(scope, dict) and bool(scope)
             and context.get("instrument_id") == instrument_id and context.get("timeframe") == timeframe)
    for identifier, (label, unit, _field, population) in METRICS.items():
        item = series.get(identifier, {})
        points = item.get("points", [])
        validated = []
        if (exact and item.get("unit_status") == "VERIFIED" and item.get("unit") == unit
                and item.get("population") == population and item.get("native_timeframe") == timeframe
                and item.get("status") in {"READY", "PARTIAL", "STALE"} and isinstance(points, list)):
            for point in points:
                if not isinstance(point, dict) or "value" not in point or isinstance(point["value"], bool):
                    continue
                try:
                    value = float(point["value"])
                except (ValueError, TypeError, OverflowError):
                    continue
                opened, closed, known = (_context_epoch(point.get(key)) for key in ("time", "close_time", "available_at"))
                if isfinite(value) and closed - opened == TIMEFRAME_SECONDS[timeframe] and closed <= known <= time.time():
                    validated.append(point)
        old = (item.get("status") == "STALE" or context.get("status") == "STALE" or item.get("cache_retained")
               or _context_epoch(item.get("expires_at", context.get("expires_at"))) <= time.time())
        if validated:
            available.append(identifier)
            (stale if old else fresh).append(identifier)
        metrics.append({
            "id": identifier, "label": label, "status": item.get("status", "NOT_AVAILABLE"),
            "usable_points": len(validated), "reference_only": bool(validated and old),
            "unit_status": item.get("unit_status", "UNKNOWN"),
            "fetched_at": item.get("fetched_at", context.get("fetched_at")),
            "as_of": max((_context_epoch(point.get("close_time")) for point in validated), default=None),
        })
    return {
        "available_metric_ids": available, "ready_metric_ids": fresh, "stale_metric_ids": stale,
        "unavailable_metric_ids": [name for name in METRICS if name not in available],
        "ready_count": len(fresh), "available_count": len(available), "total_count": len(METRICS),
        "metrics": metrics,
        "interpretation_policy": "REPORT_EACH_METRIC_SEPARATELY_STALE_IS_REFERENCE_ONLY_QUEUE_IS_NOT_DATA_AVAILABILITY",
    }


def _public_context_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public_context_value(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in (
                    "secret", "token", "password", "api_key", "apikey", "credential", "quota", "remaining",
                    "raw_response", "account_id", "account_info",
                ))}
    if isinstance(value, (tuple, list)):
        return [_public_context_value(item) for item in value[:2_000]]
    return value


def _uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    return value
