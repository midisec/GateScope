"""Authenticated research and explicitly authorized PAPER execution MCP server."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncIterator
from urllib.parse import ParseResult, urlparse
from uuid import UUID

import anyio
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ToolAnnotations

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_alert_rules import PostgresAlertRuleRepository
from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_mcp import PostgresMcpRepository, VerifiedMcpToken
from gatescope.adapters.postgres_oauth import PostgresOAuthRepository
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from gatescope.adapters.postgres_mcp_structures import McpStructureRepository
from gatescope.adapters.postgres_mcp_research import McpResearchRepository
from gatescope.application.mcp_tools import McpAnalysisService
from apps.mcp_server.research import ResearchMCPServer, chart_tool_result, register_research_tools
from apps.mcp_server.paper import register_paper_tools
from gatescope.application.paper_execution import build_paper_execution

SERVER_INSTRUCTIONS = (
    "GateScope exposes tenant-isolated crypto market research and separately consented PAPER simulation. "
    "Never describe a tool result as guaranteed profit or as an order instruction. "
    "Research tools cannot place orders. Only paper_order_submit/cancel and paper_position_close can "
    "mutate the user's local PAPER account, requiring paper:read + paper:trade and confirm_paper=true. "
    "Never place LIVE or TESTNET orders. Read paper_account_list/snapshot and positions first, preview "
    "integer CONTRACT quantities and risk, then use one idempotency key per intent. Retry the SAME "
    "key/arguments after timeout; acceptance is not a fill. Model excludes funding and leveraged liquidation. "
    "Respect data_watermark, "
    "quality_flags, research_only, and usage_notice in every conclusion. Historical "
    "indicator values must not be rewritten to fit later price action."
    " Use market_analysis_snapshot with an instrument and timeframe supplied on each call. "
    "This existing tool now returns market-analysis-2, including an actual PNG, a same-candle chart "
    "with EMA9/21 and RSI14, and stored formations/waves under structures. Existing clients can use "
    "that same tool name before refreshing their cached tool list; image_status discloses rendering readiness. "
    "One authorized client/token can query different instruments and timeframes in successive "
    "calls; the management-page preview does not bind or configure those parameters. "
    "Use instrument_id OR exact symbol plus market, and pass timeframe explicitly when "
    "comparing periods. Its closed candles, "
    "display-only forming candle and cached Coinglass observations have distinct clocks. "
    "Do not conflate units, exchange/product scope, missing data, stale values, geometric "
    "confidence or strategy win rate. Coinglass tools never fetch upstream or execute trades. "
    "For trading research, resolve the instrument first, then use market_research_bundle for "
    "multiple periods. Use structure_current to filter formations/waves, structure_study for "
    "stored research. For visual confirmation call market_chart_snapshot separately for 5m, 15m and 1h: "
    "it returns an actual PNG with volume, EMA9/21 and RSI14 plus matching OHLCV and source/time metadata. "
    "Inspect captured_at, last_closed_at and includes_forming_candle; indicators use closed candles only. "
    "Use structure_study for "
    "stored historical sample evidence, and structure_event_list/detail for original predictions "
    "and failed cases. RECORDED and REPLAY evidence are different; never present reconstructed "
    "history as a live prediction. Geometry confidence, formation rate, target rate and cost-adjusted "
    "trade win rate are distinct statistics. Return unknown when samples or freshness are inadequate. "
    "indicator_get_series supports explicit indicator parameters and bounded history. "
    "watchlist_list and research_backtest_list/detail require separately consented research:read; "
    "if denied ask the user to reconnect with that permission, never bypass ownership. "
    "These new research tools only read existing evidence and cannot enqueue replay or place orders."
)
READ_ONLY_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


class GateScopeTokenVerifier:
    def __init__(self, repository: PostgresMcpRepository, issuer: str,
                 oauth: PostgresOAuthRepository | None = None) -> None:
        self.repository = repository
        self.issuer = issuer
        self.oauth = oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        is_oauth = token.startswith("gsoat_")
        if is_oauth:
            verified = await anyio.to_thread.run_sync(self.oauth.verify_access, token) if self.oauth else None
        else:
            verified = await anyio.to_thread.run_sync(self.repository.verify_token, token)
        if verified is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(verified.client_id),
            scopes=list(verified.scopes),
            expires_at=int(verified.expires_at.timestamp()),
            subject=str(verified.subject_user_id),
            claims={
                "iss": self.issuer,
                "tenant_id": str(verified.tenant_id),
                "subject_user_id": str(verified.subject_user_id),
                "token_id": str(verified.id),
                "aud": self.issuer + "/mcp",
                "oauth_market_only": is_oauth,
            },
        )


def create_mcp_server() -> MCPServer[Any]:
    database_url = _required("GATESCOPE_DATABASE_URL")
    public_url = os.environ.get("GATESCOPE_MCP_PUBLIC_URL", "http://127.0.0.1:8780/mcp").strip()
    parsed = _validated_public_url(public_url)
    issuer = f"{parsed.scheme}://{parsed.netloc}"
    pool = PostgresPool(database_url)
    access = PostgresMcpRepository(pool)
    service = McpAnalysisService(
        PostgresMarketReadDatabase(pool),
        PostgresAlertRuleRepository(pool),
        access,
        PostgresPaperRepository(pool),
        coinglass=PostgresCoinglassContextRepository(pool),
        structures=McpStructureRepository(pool),
        research=McpResearchRepository(pool),
    )

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[dict[str, Any]]:
        pool.open()
        try:
            yield {"pool": pool}
        finally:
            pool.close()

    server: MCPServer[Any] = ResearchMCPServer(
        name="gatescope-research",
        title="GateScope Research",
        description="Tenant-scoped market, indicator and alert research data.",
        instructions=SERVER_INSTRUCTIONS,
        version="1.4.0",
        token_verifier=GateScopeTokenVerifier(access, issuer, PostgresOAuthRepository(pool, public_origin=issuer)),
        auth=AuthSettings.model_validate({
            "issuer_url": issuer,
            "resource_server_url": public_url,
            "required_scopes": ["mcp:connect"],
        }),
        lifespan=lifespan,
    )

    @server.tool(
        name="market_search_instruments", annotations=READ_ONLY_TOOL, structured_output=True,
    )
    def market_search_instruments(
        query: str = "", market: str = "all", limit: int = 50,
    ) -> dict[str, Any]:
        """Search active instruments. market: all, spot, futures:usdt, or futures:btc."""
        return service.search_instruments(
            _principal(), query=query, market=market, limit=limit,
        )

    @server.tool(name="market_get_candles", annotations=READ_ONLY_TOOL, structured_output=True)
    def market_get_candles(
        instrument_id: str,
        timeframe: str = "15m",
        from_epoch: int | None = None,
        to_epoch: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Get immutable final OHLCV candles with availability and quality metadata."""
        return service.get_candles(
            _principal(),
            instrument_id=instrument_id,
            timeframe=timeframe,
            from_epoch=from_epoch,
            to_epoch=to_epoch,
            limit=limit,
        )

    @server.tool(name="indicator_get_snapshot", annotations=READ_ONLY_TOOL, structured_output=True)
    def indicator_get_snapshot(
        instrument_id: str,
        timeframe: str = "15m",
        indicator_ids: list[str] | None = None,
        bar_limit: int = 500,
    ) -> dict[str, Any]:
        """Compute a latest versioned indicator snapshot from immutable final candles."""
        return service.indicator_snapshot(
            _principal(),
            instrument_id=instrument_id,
            timeframe=timeframe,
            indicator_ids=indicator_ids,
            bar_limit=bar_limit,
        )

    @server.tool(name="market_analysis_snapshot", annotations=READ_ONLY_TOOL, structured_output=True)
    def market_analysis_snapshot(
        instrument_id: str | None = None,
        symbol: str | None = None,
        market: str | None = None,
        timeframe: str = "15m",
        indicator_ids: list[str] | None = None,
        bar_limit: int = 500,
    ) -> Annotated[CallToolResult, dict[str, Any]]:
        """Read latest candles, common indicators, Coinglass, chart PNG and formations/waves.

        Specify instrument_id OR exact symbol plus market (spot/futures:usdt/futures:btc).
        Choose symbol/market/timeframe on each call, not in client or management settings.
        The same token can make successive calls for different instruments and periods.
        Timeframes: 1m,5m,15m,30m,1h,4h,8h,1d. bar_limit: 50..2000.
        indicator_ids: 1..8 unique sma,ema,rsi,macd,boll,atr,adx,obv; defaults to all eight.
        Requires market:read and analysis:read. Forming candles are separate/display-only.
        Coinglass returns this user's authorized cache and requests a deduplicated background refresh.
        No inline provider call or credential access. Observe retry_at; repeated calls do not bypass cooldown.
        Retained STALE data is historical reference, not fresh analysis. NO_DATA is not a zero observation.
        Since market-analysis-2 the SAME tool name also returns an actual image/png content block,
        chart (at most 150 closed candles with volume, EMA9/21 and RSI14), and structures (at most 5
        stored chart patterns/waves, original trigger/invalidation levels and confirmation state).
        For 5m,15m,1h charts call this tool once per timeframe. Check image_status, chart.captured_at,
        chart.last_closed_at and structures.status. Missing/partial evidence is unknown, not zero probability.
        The chart uses exactly the returned candles; structures have their own saved checkpoint clock.
        No additional permission is needed for these public market additions. Refresh the ChatGPT connection
        and start a new conversation to discover dedicated chart/history tools and their pagination.
        """
        snapshot = service.market_analysis_with_research(
            _principal(), instrument_id=instrument_id, symbol=symbol, market=market,
            timeframe=timeframe, indicator_ids=indicator_ids, bar_limit=bar_limit,
        )
        return chart_tool_result(snapshot, nested_chart=True)

    @server.tool(name="coinglass_get_snapshot", annotations=READ_ONLY_TOOL, structured_output=True)
    def coinglass_get_snapshot(
        instrument_id: str | None = None,
        symbol: str | None = None,
        market: str | None = None,
        timeframe: str = "15m",
        wait_seconds: int = 15,
    ) -> dict[str, Any]:
        """Read this user's cached Coinglass series with exact scope, units and source times.

        Specify instrument_id OR exact symbol plus market (spot/futures:usdt/futures:btc).
        Choose symbol/market/timeframe on each call, not in client or management settings.
        Timeframes: 1m,5m,15m,30m,1h,4h,8h,1d. Requires analysis:read.
        Returns existing data immediately. If empty, wait_seconds (0..15, default 15) briefly waits
        for background collection by reading only this user's local cache. Enqueues once, never
        performs inline provider calls or accesses credentials. Use 0 for an immediate status check.
        refresh_state distinguishes QUEUED, COLLECTING, COOLDOWN and SCHEDULED; next_eligible_at is
        the earliest possible start, NOT a promised completion time. Respect next_poll_after_seconds.
        Read availability per metric: do not say all data is unavailable when some metrics are present.
        Newly acquired metrics are published before the whole batch finishes. Respect retry_at, cache_retained,
        NO_DATA/QUEUED/DEFERRED/STALE/MISMATCH and quality flags; do not poll repeatedly during cooldown.
        Retained STALE series preserve original collection times and are not fresh trading evidence.
        Past periods fetched today must not be described as evidence available in the past.
        """
        return service.coinglass_get_snapshot(
            _principal(), instrument_id=instrument_id, symbol=symbol, market=market, timeframe=timeframe,
            wait_seconds=wait_seconds,
        )

    @server.tool(name="alert_rule_list", annotations=READ_ONLY_TOOL, structured_output=True)
    def alert_rule_list(limit: int = 50) -> dict[str, Any]:
        """List the authenticated owner's current alert rule versions without modifying them."""
        return service.list_alert_rules(_principal(market_only=False), limit=limit)

    @server.tool(name="signal_list", annotations=READ_ONLY_TOOL, structured_output=True)
    def signal_list(limit: int = 50) -> dict[str, Any]:
        """List bounded immutable signal evidence; PAPER eligibility is not a profit guarantee."""
        return service.list_signals(_principal(market_only=False), limit=limit)

    @server.tool(name="signal_explain", annotations=READ_ONLY_TOOL, structured_output=True)
    def signal_explain(signal_id: str) -> dict[str, Any]:
        """Explain one signal with frozen gates, indicators and exact historical bar revisions."""
        return service.explain_signal(_principal(market_only=False), signal_id=signal_id)

    @server.tool(name="trade_get_status", annotations=READ_ONLY_TOOL, structured_output=True)
    def trade_get_status(public_trade_id: str) -> dict[str, Any]:
        """Read a persisted PAPER trade status without advancing or changing it."""
        return service.get_trade_status(_principal(market_only=False), public_trade_id=public_trade_id)

    @server.tool(name="trade_get_timeline", annotations=READ_ONLY_TOOL, structured_output=True)
    def trade_get_timeline(public_trade_id: str) -> dict[str, Any]:
        """Read the append-only hash-chained event timeline for a PAPER trade."""
        return service.get_trade_timeline(_principal(market_only=False), public_trade_id=public_trade_id)

    register_research_tools(server, service, _principal, issuer)
    register_paper_tools(server, build_paper_execution(pool), _principal, access, issuer)
    return server


def main() -> None:
    public_url = os.environ.get("GATESCOPE_MCP_PUBLIC_URL", "http://127.0.0.1:8780/mcp").strip()
    parsed = _validated_public_url(public_url)
    configured_path = parsed.path or "/mcp"
    if configured_path != "/mcp":
        raise RuntimeError("GATESCOPE_MCP_PUBLIC_URL path must be /mcp")
    host = os.environ.get("GATESCOPE_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("GATESCOPE_MCP_PORT", "8780"))
    create_mcp_server().run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=configured_path,
        json_response=True,
        stateless_http=True,
        max_request_body_size=256 * 1024,
        transport_security=_transport_security(parsed, host, port),
    )


def _principal(*, market_only: bool = True, research_only: bool = False) -> VerifiedMcpToken:
    access_token = get_access_token()
    claims = access_token.claims if access_token is not None else None
    if access_token is None or claims is None:
        raise PermissionError("authenticated MCP principal is required")
    if research_only and "research:read" not in access_token.scopes:
        raise PermissionError("research:read requires explicit consent; reconnect and authorize research data access")
    if claims.get("oauth_market_only") and not market_only:
        raise PermissionError("This OAuth connection permits market research tools only")
    try:
        token_id = UUID(str(claims["token_id"]))
        tenant_id = UUID(str(claims["tenant_id"]))
        subject_id = UUID(str(claims["subject_user_id"]))
        client_id = UUID(access_token.client_id)
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("MCP principal claims are invalid") from exc
    expiration = access_token.expires_at
    if expiration is None or expiration <= int(datetime.now(timezone.utc).timestamp()):
        raise PermissionError("MCP token has expired")
    return VerifiedMcpToken(
        id=token_id,
        tenant_id=tenant_id,
        subject_user_id=subject_id,
        client_id=client_id,
        scopes=tuple(access_token.scopes),
        expires_at=datetime.fromtimestamp(expiration, tz=timezone.utc),
    )


def _validated_public_url(value: str) -> ParseResult:
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment
            or parsed.username or parsed.password or "*" in parsed.netloc or any(char.isspace() for char in value)):
        raise RuntimeError("GATESCOPE_MCP_PUBLIC_URL must be an HTTP(S) URL without query or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("GATESCOPE_MCP_PUBLIC_URL port is invalid") from exc
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("non-loopback MCP endpoints must use HTTPS")
    return parsed


def _transport_security(parsed: ParseResult, listen_host: str, listen_port: int) -> TransportSecuritySettings:
    """Trust the one configured public host/origin, not arbitrary proxy headers.

    MCP's loopback defaults otherwise reject the legitimate public Host passed
    by nginx. Keep rebinding protection on, with exact hosts and exact ports.
    Requests from server-to-server clients legitimately have no Origin header.
    """
    if not 1 <= listen_port <= 65535:
        raise RuntimeError("GATESCOPE_MCP_PORT is invalid")
    host = f"[{parsed.hostname}]" if ":" in str(parsed.hostname) else str(parsed.hostname)
    hosts = {parsed.netloc, host}  # nginx $host omits the external port.
    origins = {f"{parsed.scheme}://{parsed.netloc}"}
    if parsed.port is None:
        explicit = f"{host}:{443 if parsed.scheme == 'https' else 80}"
        hosts.add(explicit)
        origins.add(f"{parsed.scheme}://{explicit}")
    if listen_host in {"127.0.0.1", "localhost", "::1"}:
        local = f"[{listen_host}]" if ":" in listen_host else listen_host
        hosts.add(f"{local}:{listen_port}")
        origins.add(f"http://{local}:{listen_port}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts), allowed_origins=sorted(origins),
    )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    main()
