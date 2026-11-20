from __future__ import annotations

import base64
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from starlette.responses import HTMLResponse, RedirectResponse

from apps.api.formal_app import create_formal_app
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter


CANONICAL_WEB_ROUTES = {
    "/markets/chart",
    "/strategies/alerts",
    "/strategies/backtests",
    "/trading/paper",
    "/trading/exchange-accounts",
    "/integrations/mcp",
    "/integrations/data-sources",
    "/notifications/inbox",
    "/settings/account",
    "/settings/notifications",
}

LEGACY_WEB_ROUTES = {
    "/": "/markets/chart",
    "/markets": "/markets/chart",
    "/alerts": "/strategies/alerts",
    "/paper": "/trading/paper",
    "/exchange": "/trading/exchange-accounts",
    "/mcp": "/integrations/mcp",
    "/data-sources": "/integrations/data-sources",
    "/notifications": "/notifications/inbox",
    "/settings": "/settings/account",
    "/settings/security": "/settings/account",
}


class AnonymousAuth:
    def session(self, token: str | None) -> None:
        return None

    def valid_csrf(self, session: Any, cookie_token: str | None, header_token: str | None) -> bool:
        return False


def test_anonymous_module_and_legacy_pages_enter_login_before_routing() -> None:
    app = FastAPI()

    def index() -> HTMLResponse:
        return HTMLResponse('<div id="root"></div>')

    for path in (*CANONICAL_WEB_ROUTES, *LEGACY_WEB_ROUTES):
        app.add_api_route(path, index, methods=["GET"])
    app.add_middleware(
        SecurityMiddleware,
        auth=AnonymousAuth(),
        public_origin="http://testserver",
        cookie_secure=False,
        limiter=SlidingWindowLimiter(),
    )

    with TestClient(app, base_url="http://testserver") as client:
        for path in (*CANONICAL_WEB_ROUTES, *LEGACY_WEB_ROUTES):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"


def test_formal_web_routes_are_module_scoped_and_legacy_routes_are_permanent_redirects(
    monkeypatch,
) -> None:
    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused:unused@localhost/unused")
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEK", encoded)
    monkeypatch.setenv("GATESCOPE_RECOVERY_PEPPER", encoded)
    monkeypatch.setenv("GATESCOPE_CURSOR_SIGNING_KEY", encoded)
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEY_VERSION", "route-test-v1")
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.delenv("GATESCOPE_CREDENTIAL_WRAP_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("GATESCOPE_CREDENTIAL_KEY_VERSION", raising=False)
    monkeypatch.delenv("GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("GATESCOPE_TELEGRAM_KEY_VERSION", raising=False)

    app = create_formal_app()
    routes = {
        route.path: route
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    }

    assert CANONICAL_WEB_ROUTES <= routes.keys()
    assert CANONICAL_WEB_ROUTES <= SecurityMiddleware.UI_EXACT
    assert CANONICAL_WEB_ROUTES <= SecurityMiddleware.PARTIAL_AUTH_EXACT
    assert all(routes[path].endpoint.__name__ == "formal_web" for path in CANONICAL_WEB_ROUTES)

    assert LEGACY_WEB_ROUTES.keys() <= SecurityMiddleware.UI_EXACT
    for legacy_path, canonical_path in LEGACY_WEB_ROUTES.items():
        response = routes[legacy_path].endpoint()
        assert isinstance(response, RedirectResponse)
        assert response.status_code == 308
        assert response.headers["location"] == canonical_path
