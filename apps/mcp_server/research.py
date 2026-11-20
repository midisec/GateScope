"""Discoverable, structured read tools for the existing research evidence plane."""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp_types import ContentBlock, CallToolResult, ImageContent, TextContent, ToolAnnotations
from apps.mcp_server.paper import PAPER_READ_TOOLS, PAPER_WRITE_TOOLS

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)
TOOL_SCOPES = {
    **{name: ["paper:read"] for name in PAPER_READ_TOOLS},
    **{name: ["paper:read", "paper:trade"] for name in PAPER_WRITE_TOOLS},
    "market_search_instruments": ["market:read"], "market_get_candles": ["market:read"],
    "indicator_get_snapshot": ["analysis:read"],
    "market_analysis_snapshot": ["market:read", "analysis:read"],
    "coinglass_get_snapshot": ["analysis:read"],
    "structure_current": ["analysis:read"], "structure_study": ["analysis:read"],
    "structure_event_list": ["analysis:read"], "structure_event_detail": ["analysis:read"],
    "watchlist_list": ["research:read"], "research_backtest_list": ["research:read"],
    "research_backtest_detail": ["research:read"],
    "indicator_get_series": ["market:read", "analysis:read"],
    "market_research_bundle": ["market:read", "analysis:read"],
    "market_chart_snapshot": ["market:read", "analysis:read"],
}


def chart_tool_result(payload: dict[str, Any], *, nested_chart: bool = False) -> CallToolResult:
    """Preserve numerical evidence even if an optional image cannot be drawn.

    Both the historical tool name and the dedicated chart tool return real MCP
    image content. No URLs, credentials, or provider fetches are involved.
    """
    result = dict(payload)
    chart = payload.get("chart") if nested_chart else payload
    image_content = None
    if isinstance(chart, dict) and chart.get("candles"):
        from gatescope.application.mcp_chart import render_research_chart
        try:
            png = render_research_chart(chart)
            image_content = ImageContent(type="image", mime_type="image/png",
                data=base64.b64encode(png).decode("ascii"))
            result["image_status"] = "READY"
        except (ValueError, TypeError, KeyError, ArithmeticError, OSError):
            result["image_status"] = "UNAVAILABLE"
            result["image_unavailable_reason"] = "CHART_RENDER_FAILED_NUMERICAL_EVIDENCE_PRESERVED"
    else:
        result["image_status"] = "NO_DATA" if isinstance(chart, dict) and chart.get("status") == "NO_DATA" else "UNAVAILABLE"
        result["image_unavailable_reason"] = "NO_RENDERABLE_CHART_CANDLES"
    content: list[ContentBlock] = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False))]
    if image_content is not None:
        content.append(image_content)
    return CallToolResult(content=content, structured_content=result)


async def tool_auth_metadata(ctx, call_next):
    """Add OpenAI's extension after the SDK's per-version schema projection.

    The SDK retains standard _meta but strips non-standard top-level fields
    while shaping older protocol versions. Its public middleware API is the
    extension boundary; transport-level tests cover the actual serialized result.
    """
    result = await call_next(ctx)
    if ctx.method == "tools/list" and isinstance(result, dict):
        listed = []
        for tool in result.get("tools", []):
            scopes = TOOL_SCOPES.get(tool.get("name"))
            if scopes:
                schemes = [{"type": "oauth2", "scopes": ["mcp:connect", *scopes]}]
                tool = {**tool, "securitySchemes": schemes,
                        "_meta": {**(tool.get("_meta") or {}), "securitySchemes": schemes}}
            listed.append(tool)
        result = {**result, "tools": listed}
    return result


class ResearchMCPServer(MCPServer[Any]):
    """Preserve per-tool auth declarations without patching the protocol SDK."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.middleware.append(tool_auth_metadata)

    async def list_tools(self):
        result = []
        for tool in await super().list_tools():
            scopes = TOOL_SCOPES.get(tool.name)
            schemes = [{"type": "oauth2", "scopes": ["mcp:connect", *scopes]}] if scopes else None
            if schemes:
                tool.meta = {**(tool.meta or {}), "securitySchemes": schemes}
            result.append(tool)
        return result


def register_research_tools(server, service, principal, public_origin: str) -> None:
    def private_call(name, values):
        try:
            identity = principal(research_only=True)
        except PermissionError:
            return CallToolResult(
                is_error=True,
                content=[TextContent(type="text", text=(
                    "Research data permission is required. Reconnect and explicitly authorize research:read "
                    "to read your own watchlist and existing backtests. No data was returned."))],
                _meta={"mcp/www_authenticate": [
                    f'Bearer resource_metadata="{public_origin}/.well-known/oauth-protected-resource/mcp", '
                    'error="insufficient_scope", error_description="Explicit research data consent is required", '
                    'scope="mcp:connect market:read analysis:read research:read"'
                ]},
            )
        return getattr(service, name)(identity, **locals_without_principal(values))

    @server.tool(name="structure_current", annotations=READ_ONLY, structured_output=True)
    def structure_current(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", phase: str | None = None, family: str | None = None,
        pattern: str | None = None, confidence_min: float | None = None,
        confidence_max: float | None = None, unscored_only: bool = False,
        limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        """Read stored chart patterns and Elliott waves, with original predictions and historical references.

        Specify instrument_id OR exact symbol plus market on each call. Requires analysis:read.
        family: chart/wave; phase: FORMING/FORMED/CONFIRMED/INVALIDATED/EXPIRED/CENSORED.
        confidence_min/max: 0..1 GEOMETRIC scores, NOT formation probability or trade win rate.
        limit: 1..50; offset: 0..10000. Filter before pagination; summary describes filtered candidates.
        Use returned pattern ids for further filtering. Read sample counts, timing, versions and quality.
        Counts refer to the tracked window, not all historical events. Never starts tracking or replay.
        Use structure_event_detail for immutable original geometry and structure_study for stored statistics.
        """
        return service.structure_current(principal(), **locals_without_principal(locals()))

    @server.tool(name="structure_study", annotations=READ_ONLY, structured_output=True)
    def structure_study(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", pattern: str | None = None,
    ) -> dict[str, Any]:
        """Read existing historical formation/wave study, sample counts and uncertainty; no new backtest.

        Specify instrument_id OR exact symbol plus market on each call. Requires analysis:read.
        Optional pattern id comes from structure_current/catalog. Inspect history coverage, replay progress,
        detector version and statistic denominators. Failure/censored/pending samples are not successes.
        Formation rate and target observation rate are not net strategy win rate. NOT_REQUESTED is unknown,
        not evidence of zero patterns. Historical replay is not proof that data was available in real time.
        """
        return service.structure_study(principal(), **locals_without_principal(locals()))

    @server.tool(name="structure_event_list", annotations=READ_ONLY, structured_output=True)
    def structure_event_list(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", source: str = "RECORDED", context_id: str | None = None,
        phase: str | None = None, pattern: str | None = None, limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        """Find persisted pattern/wave cases, including failed or expired formations, in bounded pages.

        Specify instrument_id OR exact symbol plus market on each call. Requires analysis:read.
        source RECORDED means live-observed evidence; REPLAY means historical reconstruction.
        Keep source/context/model versions separate; reuse returned context_id for subsequent pages.
        phase: FORMING/FORMED/CONFIRMED/INVALIDATED/EXPIRED/CENSORED. limit 1..50; offset 0..10000.
        Use candidate id plus context_id with structure_event_detail. This does not generate new cases.
        """
        return service.structure_event_list(principal(), **locals_without_principal(locals()))

    @server.tool(name="structure_event_detail", annotations=READ_ONLY, structured_output=True)
    def structure_event_detail(
        context_id: str, candidate_id: str,
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", transition_limit: int = 20, transition_offset: int = 0,
        bar_limit: int = 300,
    ) -> dict[str, Any]:
        """Read one frozen formation/wave lifecycle with original forecasts, exact line anchors and candles.

        Obtain context_id/candidate_id from structure_current or structure_event_list; do not invent ids.
        Specify matching instrument_id OR exact symbol plus market and timeframe. Requires analysis:read.
        transition_limit 1..50; transition_offset 0..10000; bar_limit 0..500 (zero omits candles).
        Returns structured geometry, not an image. Distinguish original-time forecasts from follow-up prices.
        Read pagination/truncation and source/revision metadata. Never redraw history to fit later outcomes.
        """
        return service.structure_event_detail(principal(), **locals_without_principal(locals()))

    @server.tool(name="watchlist_list", annotations=READ_ONLY, structured_output=True)
    def watchlist_list(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Read this user's saved instruments, default periods and cached-data availability.

        Requires explicitly consented research:read; old market-only OAuth tokens cannot access this.
        limit 1..100; offset 0..10000. Does not change watchlists, subscriptions or background jobs.
        Use the returned instrument ids with market_research_bundle or structure_current.
        """
        return private_call("watchlist_list", {"limit": limit, "offset": offset})

    @server.tool(name="research_backtest_list", annotations=READ_ONLY, structured_output=True)
    def research_backtest_list(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str | None = None, limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        """List this user's EXISTING backtest runs and bounded summary metrics, never launch one.

        Requires explicitly consented research:read. Optional filter: instrument_id OR exact symbol plus market;
        optional timeframe. limit 1..50; offset 0..10000. Read run state, costs, validation and evidence versions.
        Use returned run id with research_backtest_detail; in-sample performance is not a future guarantee.
        """
        return private_call("research_backtest_list", locals())

    @server.tool(name="research_backtest_detail", annotations=READ_ONLY, structured_output=True)
    def research_backtest_detail(
        run_id: str, section: str = "summary", limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        """Read an existing owner-scoped backtest report and page its frozen trade/input evidence.

        Requires explicitly consented research:read. run_id is a UUID from research_backtest_list.
        section: summary, validation, trades, equity, cases, events, inputs. limit 1..100; offset 0..10000.
        Some sections apply only to indicator or structure engines: unavailable is not an empty-success result.
        Preserve costs/slippage/funding assumptions, sample counts, test windows and uncertainty.
        Never create a run, alter a strategy, advance a simulation or trade through this tool.
        """
        return private_call("research_backtest_detail", locals())

    @server.tool(name="indicator_get_series", annotations=READ_ONLY, structured_output=True)
    def indicator_get_series(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", indicator_id: str = "rsi", parameters: dict[str, Any] | None = None,
        bar_limit: int = 500, limit: int = 200, offset: int = 0, end_epoch: int | None = None,
    ) -> dict[str, Any]:
        """Calculate a paged historical indicator series with explicit parameters from cached closed candles.

        Specify instrument_id OR exact symbol plus market on each call. Requires market:read and analysis:read.
        indicator_id: sma/ema/rsi/macd/boll/atr/adx/obv. Example parameters: {"period":200} for EMA;
        MACD uses fast_period/slow_period/signal_period; BOLL uses period/deviations; OBV uses {}.
        bar_limit 50..2000; limit 1..500; offset 0..10000. Optional end_epoch is inclusive final candle OPEN
        time in UTC Unix seconds. Reuse returned window_end_epoch and bar_limit while paging.
        Restarts warmup after gaps, returning only the contiguous suffix; never interpolate missing candles.
        This uses currently recorded revisions, NOT a historical point-in-time backtest or win-rate estimate.
        """
        return service.indicator_get_series(principal(), **locals_without_principal(locals()))

    @server.tool(name="market_research_bundle", annotations=READ_ONLY, structured_output=True)
    def market_research_bundle(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframes: list[str] | None = None, bar_limit: int = 100, include_patterns: bool = True,
    ) -> dict[str, Any]:
        """Start multi-timeframe research: cached candles, indicators, Coinglass and current structure evidence.

        Specify instrument_id OR exact symbol plus market on each call. Requires market:read and analysis:read.
        timeframes: 1..4 unique supported periods; defaults 1d/4h/1h/15m. bar_limit 50..500 per period.
        At most 5 current patterns per period; use structure_current for full filtered pagination.
        Pure local reads: no Coinglass refresh or new replay. Partial/time-budget results must be reported.
        Not an atomic snapshot: inspect each source's time, units, market scope, expiry and missing data.
        Expand structure_event_detail/structure_study for evidence; do not guess missing win probabilities.
        """
        return service.market_research_bundle(principal(), **locals_without_principal(locals()))

    @server.tool(name="market_chart_snapshot", annotations=READ_ONLY, structured_output=True)
    def market_chart_snapshot(
        instrument_id: str | None = None, symbol: str | None = None, market: str | None = None,
        timeframe: str = "15m", bar_limit: int = 150, include_forming: bool = False,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        """Return an actual PNG candlestick chart AND the exact OHLCV/indicators used to draw it.

        Specify instrument_id OR exact symbol plus market on each call. Requires market:read and analysis:read.
        For short-term review call separately with timeframe 5m, 15m, 1h; other supported periods also work.
        Three aligned panels: candles + EMA9/EMA21, exchange-reported volume, RSI14 with 30/70 reference lines.
        bar_limit 30..300 includes optional forming candle; include_forming defaults false.
        Returns venue, market, symbol, captured_at UTC, last_closed_at, includes_forming_candle and bar revisions.
        A forming candle is display-only, never used to compute EMA/RSI. Missing bars/indicator warmup are not filled.
        Use numerical OHLCV to verify chart reasoning. Position sizing ALSO needs account risk budget and
        contract multiplier/lot rules; those are not supplied here. Use structure_current for formation details.
        """
        snapshot = service.market_chart_snapshot(principal(), **locals_without_principal(locals()))
        return chart_tool_result(snapshot)


def locals_without_principal(values: dict[str, Any]) -> dict[str, Any]:
    # Nested handlers close over service/principal; CPython may include them in
    # locals(). Only explicit input parameters belong in the audited request.
    return {key: value for key, value in values.items() if key not in {"service", "principal", "private_call"}}
