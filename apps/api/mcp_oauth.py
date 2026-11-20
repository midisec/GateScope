"""OAuth 2.1 authorization-code/PKCE boundary with explicit simulated trading consent.

No user password is accepted by these endpoints. Login, MFA and explicit
same-origin consent use the existing Web session; machine endpoints never do.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote

import anyio
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_oauth import OAUTH_SCOPES, OAuthError, PostgresOAuthRepository
from gatescope.security import SlidingWindowLimiter, error_response

OAUTH_COOKIE = "gatescope_oauth_browser"
CONSENT_PATH = "/integrations/mcp/authorize"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _oauth_error(exc: OAuthError) -> JSONResponse:
    headers = dict(NO_STORE)
    if exc.error == "invalid_client":
        headers["WWW-Authenticate"] = 'Basic realm="GateScope OAuth"'
    if exc.status_code == 429:
        headers["Retry-After"] = "60"
    return JSONResponse({"error": exc.error, "error_description": exc.description},
                        status_code=exc.status_code, headers=headers)


def _unique_pairs(pairs: list[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result or len(key) > 100 or len(value) > 4096:
            raise OAuthError("invalid_request", "Duplicate or oversized parameter")
        result[key] = value
    return result


async def _body(request: Request, *, as_json: bool = False) -> dict[str, Any]:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16_384:
            raise OAuthError("invalid_request", "Request body too large", 413)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if as_json:
            if content_type != "application/json":
                raise OAuthError("invalid_request", "Expected application/json", 415)
            def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                if len({key for key, _ in pairs}) != len(pairs):
                    raise OAuthError("invalid_request", "Duplicate JSON parameter")
                return dict(pairs)
            parsed = json.loads(data, object_pairs_hook=object_pairs)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        if content_type != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request", "Expected form-encoded request", 415)
        return _unique_pairs(parse_qsl(data.decode("utf-8"), keep_blank_values=True,
                                       strict_parsing=True, max_num_fields=24))
    except (UnicodeError, ValueError):
        raise OAuthError("invalid_request", "Malformed request body") from None


def _client_auth(request: Request, fields: dict[str, str]) -> dict[str, Any]:
    authorization = request.headers.get("authorization")
    client_id, secret = fields.get("client_id", ""), fields.get("client_secret")
    method = "client_secret_post" if secret is not None else "none"
    if authorization:
        if not authorization.lower().startswith("basic ") or secret is not None:
            raise OAuthError("invalid_client", "Invalid client authentication", 401)
        try:
            raw = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            basic_id, basic_secret = raw.split(":", 1)
            basic_id, basic_secret = unquote(basic_id), unquote(basic_secret)
            if client_id and client_id != basic_id:
                raise ValueError
            client_id, secret, method = basic_id, basic_secret, "client_secret_basic"
        except (ValueError, UnicodeError, binascii.Error):
            raise OAuthError("invalid_client", "Invalid client authentication", 401) from None
    if not client_id or len(client_id) > 256 or (secret is not None and len(secret) > 512):
        raise OAuthError("invalid_client", "Invalid client authentication", 401)
    return {"client_id": client_id, "client_secret": secret, "auth_method": method}


def build_mcp_oauth_router(
    pool: PostgresPool, *, public_origin: str, cookie_secure: bool,
    require_credential_write: Callable[[Request], JSONResponse | None],
    repository: Any = None, resource_aliases: tuple[str, ...] = (),
) -> APIRouter:
    router = APIRouter(tags=["mcp-oauth"])
    issuer = public_origin.rstrip("/")
    resource = issuer + "/mcp"
    storage = repository if repository is not None else PostgresOAuthRepository(
        pool, public_origin=issuer, resource_aliases=resource_aliases,
    )
    limiter = SlidingWindowLimiter()

    @router.options("/oauth/register", include_in_schema=False)
    @router.options("/oauth/token", include_in_schema=False)
    @router.options("/oauth/revoke", include_in_schema=False)
    @router.options("/.well-known/oauth-authorization-server", include_in_schema=False)
    @router.options("/.well-known/oauth-authorization-server/", include_in_schema=False)
    @router.options("/.well-known/oauth-protected-resource", include_in_schema=False)
    @router.options("/.well-known/oauth-protected-resource/", include_in_schema=False)
    @router.options("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    @router.options("/.well-known/oauth-protected-resource/mcp/", include_in_schema=False)
    def preflight() -> Response:
        return Response(status_code=204, headers={**NO_STORE, "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type"})

    def limit(request: Request, operation: str, count: int) -> Response | None:
        client = request.client.host if request.client else "unknown"
        allowed, retry = limiter.allow(f"oauth:{operation}:{client}", count, 60)
        if not allowed:
            return JSONResponse({"error": "temporarily_unavailable", "error_description": "Please retry later"},
                                status_code=429, headers={**NO_STORE, "Retry-After": str(retry)})
        return None

    @router.get("/.well-known/oauth-authorization-server")
    @router.head("/.well-known/oauth-authorization-server", include_in_schema=False)
    @router.api_route("/.well-known/oauth-authorization-server/", methods=["GET", "HEAD"], include_in_schema=False)
    def authorization_metadata() -> JSONResponse:
        return JSONResponse({
            "issuer": issuer,
            "authorization_endpoint": issuer + "/oauth/authorize",
            "token_endpoint": issuer + "/oauth/token",
            "registration_endpoint": issuer + "/oauth/register",
            "revocation_endpoint": issuer + "/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": OAUTH_SCOPES,
            "authorization_response_iss_parameter_supported": True,
        }, headers={**NO_STORE, "Access-Control-Allow-Origin": "*"})

    @router.get("/.well-known/oauth-protected-resource")
    @router.head("/.well-known/oauth-protected-resource", include_in_schema=False)
    @router.api_route("/.well-known/oauth-protected-resource/", methods=["GET", "HEAD"], include_in_schema=False)
    @router.get("/.well-known/oauth-protected-resource/mcp")
    @router.head("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    @router.api_route("/.well-known/oauth-protected-resource/mcp/", methods=["GET", "HEAD"], include_in_schema=False)
    def resource_metadata() -> JSONResponse:
        return JSONResponse({"resource": resource, "resource_name": "GateScope Market Research",
                             "authorization_servers": [issuer], "scopes_supported": OAUTH_SCOPES,
                             "bearer_methods_supported": ["header"]},
                            headers={**NO_STORE, "Access-Control-Allow-Origin": "*"})

    @router.post("/oauth/register")
    async def register(request: Request) -> Response:
        if limited := limit(request, "register", 10):
            return limited
        try:
            payload = await _body(request, as_json=True)
            result = await anyio.to_thread.run_sync(storage.register_client, payload)
            return JSONResponse(result, status_code=201, headers=NO_STORE)
        except OAuthError as exc:
            return _oauth_error(exc)

    @router.get("/oauth/authorize")
    async def authorize(request: Request) -> Response:
        if limited := limit(request, "authorize", 30):
            return limited
        try:
            query = _unique_pairs(list(request.query_params.multi_items()))
            nonce = request.cookies.get(OAUTH_COOKIE, "")
            if not 40 <= len(nonce) <= 128 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in nonce):
                nonce = secrets.token_urlsafe(32)
            handle = await anyio.to_thread.run_sync(storage.begin_authorization, query, nonce)
            response = RedirectResponse(CONSENT_PATH + "?request=" + handle, status_code=303, headers=NO_STORE)
            response.set_cookie(OAUTH_COOKIE, nonce, secure=cookie_secure, httponly=True,
                                samesite="lax", max_age=600, path="/")
            return response
        except OAuthError as exc:
            # Never redirect on unvalidated client/redirect/PKCE/resource input.
            return _oauth_error(exc)

    @router.get("/api/v2/mcp/oauth/consent")
    def consent(request: Request) -> Response:
        session = request.state.auth_session
        if not session or not session.fully_authenticated:
            return error_response(401, "AUTH_REQUIRED", "请先完成登录验证", request.state.trace_id)
        try:
            query = _unique_pairs(list(request.query_params.multi_items()))
            result = storage.pending_authorization(query.get("request", ""), request.cookies.get(OAUTH_COOKIE, ""),
                                                   session.tenant_id, session.user_id)
            return JSONResponse({**result, "username": session.username}, headers=NO_STORE)
        except OAuthError as exc:
            code = "ROLE_FORBIDDEN" if exc.error == "access_denied" and exc.status_code == 403 else "OAUTH_REQUEST_INVALID"
            return error_response(exc.status_code, code, exc.description, request.state.trace_id)

    @router.post("/api/v2/mcp/oauth/consent")
    async def decide(request: Request) -> Response:
        session = request.state.auth_session
        if not session or not session.fully_authenticated:
            return error_response(401, "AUTH_REQUIRED", "请先完成登录验证", request.state.trace_id)
        if limited := limit(request, "consent", 20):
            return limited
        try:
            fields = await _body(request, as_json=True)
            if (not {"request_id", "decision"} <= set(fields)
                    or not set(fields) <= {"request_id", "decision", "approved_scopes"}
                    or not isinstance(fields["request_id"], str) or fields["decision"] not in ("approve", "deny")):
                raise OAuthError("invalid_request", "Invalid consent decision")
            if "approved_scopes" in fields:
                selected = fields["approved_scopes"]
                if (fields["decision"] != "approve" or not isinstance(selected, list)
                        or not 1 <= len(selected) <= len(OAUTH_SCOPES)
                        or any(not isinstance(scope, str) or scope not in OAUTH_SCOPES for scope in selected)
                        or len(set(selected)) != len(selected) or "mcp:connect" not in selected
                        or ("paper:trade" in selected and "paper:read" not in selected)):
                    raise OAuthError("invalid_scope", "Invalid selected permissions")
            if fields["decision"] == "approve":
                if forbidden := require_credential_write(request):
                    return forbidden
                operation = storage.approve_authorization
            else:
                operation = storage.deny_authorization
            redirect = await anyio.to_thread.run_sync(operation, fields["request_id"],
                                                     request.cookies.get(OAUTH_COOKIE, ""),
                                                     session.tenant_id, session.user_id,
                                                     *([fields["approved_scopes"]] if "approved_scopes" in fields else []))
            return JSONResponse({"redirect_url": redirect}, headers=NO_STORE)
        except OAuthError as exc:
            return error_response(exc.status_code, "OAUTH_REQUEST_INVALID", exc.description, request.state.trace_id)

    @router.post("/oauth/token")
    async def token(request: Request) -> Response:
        if limited := limit(request, "token", 60):
            return limited
        try:
            fields = await _body(request)
            auth = _client_auth(request, fields)
            grant = fields.get("grant_type")
            if grant == "authorization_code":
                result = await anyio.to_thread.run_sync(lambda: storage.exchange_code(
                    **auth, code=fields.get("code", ""), code_verifier=fields.get("code_verifier", ""),
                    redirect_uri=fields.get("redirect_uri", ""), resource=fields.get("resource", "")))
            elif grant == "refresh_token":
                result = await anyio.to_thread.run_sync(lambda: storage.refresh(
                    **auth, refresh_token=fields.get("refresh_token", ""), resource=fields.get("resource", ""),
                    scope=fields.get("scope")))
            else:
                raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")
            return JSONResponse(result, headers=NO_STORE)
        except OAuthError as exc:
            return _oauth_error(exc)

    @router.post("/oauth/revoke")
    async def revoke(request: Request) -> Response:
        if limited := limit(request, "revoke", 60):
            return limited
        try:
            fields = await _body(request)
            auth = _client_auth(request, fields)
            if not fields.get("token"):
                raise OAuthError("invalid_request", "A token is required")
            await anyio.to_thread.run_sync(lambda: storage.revoke(
                **auth, token=fields["token"], token_type_hint=fields.get("token_type_hint")))
            return Response(status_code=200, headers=NO_STORE)
        except OAuthError as exc:
            return _oauth_error(exc)

    return router
