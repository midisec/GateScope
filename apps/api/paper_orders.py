"""Session-authenticated endpoints shared with the MCP PAPER account ledger."""
from datetime import datetime, timedelta, timezone
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Query, Request, Response

from gatescope.application.paper_execution import (
    PaperCancelRequest, PaperCloseRequest, PaperOrderRequest, build_paper_execution, execution_failure,
)
from gatescope.domain.identity import Action, AuthorizationContext, AuthorizationDenied, authorize
from gatescope.security import error_response


def build_paper_orders_router(pool):
    router = APIRouter(prefix="/api/v2/paper", tags=["paper-orders"])
    service = build_paper_execution(pool)

    def allowed(request, *, opening=False):
        session = request.state.auth_session
        try:
            authorize(AuthorizationContext(session.tenant_id, session.user_id, session.role,
                session.recent_auth_at, session.mfa_verified_at, "WEB"), Action.PROPOSAL_APPROVE,
                now=datetime.now(timezone.utc))
        except AuthorizationDenied:
            return error_response(403, "PAPER_ROLE_FORBIDDEN", "当前角色不能操作模拟委托", request.state.trace_id)
        if opening and (session.recent_auth_at is None or session.recent_auth_at < datetime.now(timezone.utc)-timedelta(minutes=10)):
            return error_response(403, "RECENT_AUTH_REQUIRED", "开仓前请再次验证当前密码", request.state.trace_id)
        return None

    def call(request, function, *args, **kwargs):
        session = request.state.auth_session
        try:
            result = function(session.tenant_id, session.user_id, *args, **kwargs)
            return {**result, "trace_id": request.state.trace_id}
        except Exception as exc:
            failure = execution_failure(exc)
            return error_response(failure.status, failure.code, failure.message, request.state.trace_id)

    @router.get("/accounts/{account_id}/snapshot")
    def snapshot(account_id: UUID, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.account_snapshot, account_id)

    @router.get("/accounts/{account_id}/orders")
    def orders(account_id: UUID, request: Request, response: Response,
               state: Literal["ALL", "ACTIVE", "NEW", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"] | None = None,
               limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
               cursor: str | None = Query(None,max_length=512), pagination: Literal["OFFSET","CURSOR"]="OFFSET"):
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.repository.list_orders, account_id=account_id,
                    status=None if state == "ALL" else state, limit=limit, offset=offset,cursor=cursor,pagination=pagination)

    @router.get("/accounts/{account_id}/positions")
    def positions(account_id: UUID, request: Request, response: Response,
                  state: Literal["ALL", "OPEN", "CLOSED"] | None = None,
                  limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                  cursor: str | None = Query(None,max_length=512), pagination: Literal["OFFSET","CURSOR"]="OFFSET"):
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.repository.list_positions, account_id=account_id,
                    status=None if state == "ALL" else state, limit=limit, offset=offset,cursor=cursor,pagination=pagination)

    @router.get("/accounts/{account_id}/fills")
    def fills(account_id: UUID, request: Request, response: Response,
              limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
              cursor: str | None = Query(None,max_length=512), pagination: Literal["OFFSET","CURSOR"]="OFFSET"):
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.repository.list_fills, account_id=account_id, limit=limit, offset=offset,cursor=cursor,pagination=pagination)

    @router.get("/orders/{order_id}/fills")
    def order_fills(order_id: UUID, request: Request, response: Response,
                    limit: int = Query(50,ge=1,le=100),cursor: str | None = Query(None,max_length=512)):
        response.headers["Cache-Control"]="no-store"
        return call(request,service.repository.order_history,order_id,kind="fills",limit=limit,cursor=cursor)

    @router.get("/orders/{order_id}/events")
    def order_events(order_id: UUID, request: Request, response: Response,
                     limit: int = Query(50,ge=1,le=100),cursor: str | None = Query(None,max_length=512)):
        response.headers["Cache-Control"]="no-store"
        return call(request,service.repository.order_history,order_id,kind="events",limit=limit,cursor=cursor)

    @router.get("/orders/{order_id}")
    def order(order_id: UUID, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.repository.get_order, order_id)

    @router.get("/orders/{order_id}/chart")
    def original_chart(order_id: UUID, request: Request, pattern_index: int = Query(0, ge=0, le=4)):
        from gatescope.application.paper_decision_chart import render_paper_decision_chart
        session = request.state.auth_session
        try:
            order = service.repository.get_order(session.tenant_id, session.user_id, order_id)
            png, _ = render_paper_decision_chart(order, pattern_index=pattern_index)
            return Response(png, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except Exception as exc:
            failure = execution_failure(exc)
            return error_response(failure.status, failure.code, failure.message, request.state.trace_id)

    @router.post("/orders")
    def submit(payload: PaperOrderRequest, request: Request):
        denied = allowed(request, opening=True)
        if denied is not None:
            return denied
        return call(request, service.submit, payload.model_dump(mode="json"), source="WEB")

    @router.post("/orders:preview")
    def preview(payload: PaperOrderRequest, request: Request, response: Response):
        # Read-only even though the bounded request is submitted as JSON. The
        # session/MFA/CSRF boundary still applies, without granting trade rights.
        response.headers["Cache-Control"] = "no-store"
        return call(request, service.preview, payload.model_dump(mode="json"))

    @router.post("/orders/{order_id}:cancel")
    def cancel(order_id: UUID, payload: PaperCancelRequest, request: Request):
        denied = allowed(request)
        if denied is not None:
            return denied
        return call(request, service.cancel, order_id, payload.model_dump(mode="json"))

    @router.post("/positions/{position_id}:close")
    def close(position_id: UUID, payload: PaperCloseRequest, request: Request):
        denied = allowed(request)
        if denied is not None:
            return denied
        return call(request, service.close, position_id, payload.model_dump(mode="json"), source="WEB")

    return router
