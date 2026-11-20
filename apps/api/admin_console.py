"""OWNER-only current-workspace administration; CSRF is enforced globally."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from gatescope.application.admin_console import AdminConsoleError, AdminConsoleService, require_owner
from gatescope.security import SlidingWindowLimiter, error_response


def build_admin_console_router(api_pool, identity):
    router = APIRouter(prefix="/api/v2/admin", tags=["workspace-administration"])
    service, limiter = AdminConsoleService(api_pool, identity), SlidingWindowLimiter()

    def call(request, response, method, *args, recent=False, **kwargs):
        response.headers["Cache-Control"] = "no-store"
        trace = getattr(request.state, "trace_id", "admin-console")
        actor = getattr(request.state, "auth_session", None)
        try:
            require_owner(actor, recent=recent, now=datetime.now(timezone.utc))
            allowed, retry = limiter.allow(f"admin-console:{actor.tenant_id}:{actor.user_id}:{request.method}",
                                          10 if recent else 90, 60)
            if not allowed:
                return error_response(429, "RATE_LIMITED", "管理请求较频繁，请稍后重试", trace, **{"Retry-After": str(retry)})
            return {**method(actor, *args, **kwargs), "trace_id": trace}
        except AdminConsoleError as exc:
            return error_response(exc.status, exc.code, exc.message, trace)
        except Exception:
            # Query/pool timeouts and permissions must not echo SQL, DSNs or
            # user data, nor turn unavailable counts into misleading zeroes.
            return error_response(503, "ADMIN_STATS_UNAVAILABLE", "管理数据暂不可用，请稍后重试", trace, **{"Retry-After": "5"})

    @router.get("/overview")
    def overview(request: Request, response: Response):
        return call(request, response, service.overview)

    @router.get("/users")
    def users(request: Request, response: Response, q: str = Query(default="", max_length=80),
              status: Literal["ALL", "ACTIVE", "LOCKED", "DISABLED", "REVOKED"] = "ALL",
              role: Literal["ALL", "OWNER", "OPERATOR", "RESEARCHER", "VIEWER"] = "ALL",
              page: int = Query(default=1, ge=1, le=10_000), page_size: int = Query(default=20, ge=1, le=100)):
        return call(request, response, service.users, q=q, status=status, role=role, page=page, page_size=page_size)

    @router.get("/resources")
    def resources(request: Request, response: Response):
        return call(request, response, service.resources)

    @router.get("/audit")
    def audit(request: Request, response: Response, action: str = Query(default="", max_length=80),
              outcome: Literal["ALL", "SUCCESS", "DENIED", "FAILED"] = "ALL",
              page: int = Query(default=1, ge=1, le=10_000), page_size: int = Query(default=20, ge=1, le=100)):
        return call(request, response, service.audit, action=action, outcome=outcome, page=page, page_size=page_size)

    @router.post("/users/{user_id}/sessions:revoke")
    def revoke(user_id: UUID, request: Request, response: Response):
        return call(request, response, service.revoke_sessions, user_id, recent=True,
                    trace_id=getattr(request.state, "trace_id", "admin-console"))

    return router
