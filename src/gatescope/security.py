"""HTTP authentication boundary, CSRF enforcement and baseline rate limiting."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from gatescope.auth import CSRF_COOKIE, SESSION_COOKIE

TRACE_RE = re.compile(r"^[a-fA-F0-9-]{16,64}$")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SessionSecurityAuth(Protocol):
    def session(self, token: str | None) -> Any | None: ...

    def valid_csrf(
        self, session: Any, cookie_token: str | None, header_token: str | None,
    ) -> bool: ...


class SlidingWindowLimiter:
    """Process-local limiter for the Phase 0 single-instance deployment."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window_seconds - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
            if len(self._events) > 10_000:
                for stale_key in list(self._events)[:1000]:
                    if not self._events[stale_key] or self._events[stale_key][-1] <= cutoff:
                        self._events.pop(stale_key, None)
            return True, 0


def error_response(status: int, code: str, message: str, trace_id: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "detail": message, "trace_id": trace_id},
        headers=headers,
    )


class SecurityMiddleware(BaseHTTPMiddleware):
    # These machine endpoints authenticate OAuth clients/codes, NEVER cookies.
    OAUTH_MACHINE_EXACT = {"/oauth/register", "/oauth/token", "/oauth/revoke"}
    OAUTH_BROWSER_EXACT = {"/oauth/authorize", "/integrations/mcp/authorize"}
    OAUTH_DISCOVERY_EXACT = {"/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
                             "/.well-known/oauth-protected-resource/mcp",
                             "/.well-known/oauth-authorization-server/", "/.well-known/oauth-protected-resource/",
                             "/.well-known/oauth-protected-resource/mcp/"}
    PUBLIC_EXACT = {
        "/health/live", "/health/ready", "/login", "/auth/login", "/auth/workspaces",
        "/api/v2/security/submission-key",
        "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    } | OAUTH_MACHINE_EXACT | OAUTH_BROWSER_EXACT | OAUTH_DISCOVERY_EXACT
    UI_EXACT = {
        "/", "/markets", "/alerts", "/paper", "/exchange", "/mcp", "/data-sources",
        "/notifications", "/settings", "/settings/security",
        "/markets/chart", "/strategies/alerts", "/strategies/backtests", "/trading/paper", "/trading/shadow",
        "/trading/exchange-accounts", "/integrations/mcp", "/integrations/data-sources",
        "/notifications/inbox", "/settings/account", "/settings/notifications",
        "/admin", "/admin/overview", "/admin/users", "/admin/resources", "/admin/audit",
    }
    PARTIAL_AUTH_EXACT = {
        "/auth/me", "/auth/logout", "/auth/mfa/verify", "/auth/mfa-methods:enroll",
        "/auth/mfa-methods", "/auth/recovery-codes:consume",
    } | UI_EXACT | OAUTH_BROWSER_EXACT

    def __init__(
        self,
        app: Any,
        *,
        auth: SessionSecurityAuth,
        public_origin: str | None,
        cookie_secure: bool,
        limiter: SlidingWindowLimiter,
    ) -> None:
        super().__init__(app)
        self.auth = auth
        self.public_origin = public_origin
        self.public_netloc = urlparse(public_origin).netloc.lower() if public_origin else None
        self.cookie_secure = cookie_secure
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        supplied_trace = request.headers.get("x-request-id", "")
        trace_id = supplied_trace if TRACE_RE.fullmatch(supplied_trace) else uuid.uuid4().hex
        request.state.trace_id = trace_id

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > 1_048_576:
                    return self._secure(error_response(413, "BODY_TOO_LARGE", "请求体过大", trace_id), request)
            except ValueError:
                return self._secure(error_response(400, "INVALID_CONTENT_LENGTH", "请求格式无效", trace_id), request)

        path = request.url.path
        request_host = request.headers.get("host", "").lower()
        local_hostname = (request.url.hostname or "").lower()
        local_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
        client_host = (request.client.host if request.client else "").lower()
        local_client = client_host in {"127.0.0.1", "localhost", "::1", "testclient"}
        local_ingress = local_client and local_hostname in local_hosts
        if self.public_netloc:
            host_valid = request_host == self.public_netloc or local_ingress
        else:
            host_valid = local_hostname in local_hosts
        if not host_valid:
            return self._secure(error_response(400, "HOST_REJECTED", "请求主机无效", trace_id), request)

        is_static = path.startswith(("/static/", "/assets/"))
        is_telegram_webhook = request.method == "POST" and path.startswith("/api/v2/telegram/webhook/")
        is_public = path in self.PUBLIC_EXACT or is_static or path == "/favicon.ico" or is_telegram_webhook
        session = None
        if not is_static and path not in self.OAUTH_MACHINE_EXACT | self.OAUTH_DISCOVERY_EXACT:
            session = self.auth.session(request.cookies.get(SESSION_COOKIE))
        request.state.auth_session = session

        client_key = request.client.host if request.client else "unknown"
        if path in self.OAUTH_DISCOVERY_EXACT:
            # Metadata discovery must not be starved by ordinary anonymous
            # page/static requests. All exact aliases still share one bound.
            rate_key, rate_limit = f"oauth-discovery:{client_key}", 120
        else:
            rate_key = f"session:{session.session_id}" if session else f"ip:{client_key}"
            rate_limit = 240 if session else 80
        allowed, retry_after = self.limiter.allow(rate_key, rate_limit, 60)
        if not allowed:
            return self._secure(
                error_response(429, "RATE_LIMITED", "请求过于频繁", trace_id, **{"Retry-After": str(retry_after)}),
                request,
            )

        if path in self.UI_EXACT and session is None:
            return self._secure(RedirectResponse("/login", status_code=303), request)
        if not is_public and session is None:
            return self._secure(error_response(401, "AUTH_REQUIRED", "请先登录", trace_id), request)
        if (
            session is not None
            and not bool(getattr(session, "fully_authenticated", True))
            and path not in self.PARTIAL_AUTH_EXACT
            and not path.startswith("/auth/mfa-methods/")
        ):
            return self._secure(error_response(403, "MFA_REQUIRED", "需要完成多因素认证", trace_id), request)

        if (
            session is not None
            and request.method in UNSAFE_METHODS
            and path != "/auth/login"
            and not is_telegram_webhook
        ):
            if not self.auth.valid_csrf(
                session,
                request.cookies.get(CSRF_COOKIE),
                request.headers.get("x-csrf-token"),
            ):
                return self._secure(error_response(403, "CSRF_REJECTED", "安全校验失败", trace_id), request)

        origin = request.headers.get("origin")
        if origin and request.method in UNSAFE_METHODS and path not in self.OAUTH_MACHINE_EXACT:
            expected = self.public_origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
            parsed_origin = urlparse(origin)
            trusted_local_origin = (
                local_ingress
                and (parsed_origin.hostname or "").lower() in local_hosts
                and parsed_origin.netloc.lower() == request_host
            )
            if origin.rstrip("/") != expected.rstrip("/") and not trusted_local_origin:
                return self._secure(error_response(403, "ORIGIN_REJECTED", "请求来源无效", trace_id), request)

        response = await call_next(request)
        if path in self.OAUTH_MACHINE_EXACT | self.OAUTH_DISCOVERY_EXACT:
            # OAuth public-client endpoints do not consume session cookies.
            response.headers["Access-Control-Allow-Origin"] = "*"
        return self._secure(response, request)

    def _secure(self, response: Response, request: Request) -> Response:
        response.headers["X-Request-ID"] = getattr(request.state, "trace_id", uuid.uuid4().hex)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/auth/", "/oauth/", "/.well-known/oauth-")) or request.url.path == "/integrations/mcp/authorize":
            response.headers["Cache-Control"] = "no-store"
        if self.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
