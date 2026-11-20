"""Authenticated Web preview of the exact read-only MCP market projection."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.errors import QueryCanceled

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_alert_rules import PostgresAlertRuleRepository
from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from gatescope.application.mcp_tools import McpAnalysisService
from gatescope.security import SlidingWindowLimiter


def build_mcp_market_preview_router(pool: PostgresPool) -> APIRouter:
    router = APIRouter(prefix="/api/v2/mcp", tags=["mcp-market-preview"])
    limiter = SlidingWindowLimiter()
    service = McpAnalysisService(
        PostgresMarketReadDatabase(pool), PostgresAlertRuleRepository(pool), PostgresMcpRepository(pool),
        coinglass=PostgresCoinglassContextRepository(pool),
    )

    @router.get("/market-preview")
    def market_preview(
        request: Request, response: Response, instrument_id: UUID,
        timeframe: str = Query(default="15m", pattern=r"^(1m|5m|15m|30m|1h|4h|8h|1d)$"),
        bar_limit: int = Query(default=500, alias="limit", ge=50, le=2_000),
    ) -> dict:
        session = getattr(request.state, "auth_session", None)
        if session is None or not session.fully_authenticated:
            raise HTTPException(401, "请先完成登录验证")
        allowed, retry = limiter.allow(f"mcp-market-preview:{session.tenant_id}:{session.user_id}", 12, 60)
        if not allowed:
            raise HTTPException(429, "预览请求较频繁，请稍后重试", headers={"Retry-After": str(retry)})
        response.headers["Cache-Control"] = "no-store"
        try:
            result = service.market_preview(
                session.tenant_id, session.user_id, instrument_id=str(instrument_id),
                timeframe=timeframe, bar_limit=bar_limit,
            )
        except QueryCanceled:
            raise HTTPException(503, "行情查询超时，请稍后重试", headers={"Retry-After": "5"}) from None
        except ValueError:
            raise HTTPException(422, "标的或数据窗口不可用，请重新选择") from None
        return {**result, "access_mode": "AUTHENTICATED_WEB_PREVIEW_NOT_EXTERNAL_MCP_AUTH",
                "trace_id": getattr(request.state, "trace_id", None)}

    return router
