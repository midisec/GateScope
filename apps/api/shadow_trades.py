"""Read-only, authenticated shadow-trade research endpoints."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_shadow import ShadowTradeRepository


def build_shadow_router(pool: PostgresPool) -> APIRouter:
    router = APIRouter(prefix="/api/v2/research/shadow-trades", tags=["shadow-research"])
    repository = ShadowTradeRepository(pool)

    @router.get("")
    def list_trades(
        request: Request, response: Response,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        cohort: str = Query(default="", pattern=r"^(|SIGNAL|DELIVERED)$"),
        state: str = Query(default="", pattern=r"^(|WAITING_DELIVERY|PENDING|OPEN|CLOSED|SKIPPED|EXPIRED)$"),
        instrument_id: UUID | None = None,
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        session = request.state.auth_session
        return repository.list(session.tenant_id, session.user_id, limit=limit, offset=offset,
                               cohort=cohort, state=state, instrument_id=instrument_id)

    @router.get("/summary")
    def summary(
        request: Request, response: Response,
        cohort: str = Query(default="", pattern=r"^(|SIGNAL|DELIVERED)$"),
        instrument_id: UUID | None = None,
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        session = request.state.auth_session
        return repository.summary(session.tenant_id, session.user_id, cohort=cohort, instrument_id=instrument_id)

    @router.get("/{identifier}")
    def detail(identifier: UUID, request: Request, response: Response,
               event_limit: int = Query(default=100, ge=1, le=200),
               event_offset: int = Query(default=0, ge=0, le=1_000_000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        session = request.state.auth_session
        try:
            return repository.get(session.tenant_id, session.user_id, identifier,
                                  event_limit=event_limit, event_offset=event_offset)
        except LookupError:
            raise HTTPException(404, "影子交易记录不存在") from None

    return router
