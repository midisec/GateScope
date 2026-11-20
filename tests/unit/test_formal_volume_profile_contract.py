"""Formal route wiring and real HTTP security middleware, without a database."""
from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import fastapi.routing as fastapi_routing
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.api.formal_app import create_formal_app
from apps.api.volume_profile import build_volume_profile_router
from gatescope.auth import SESSION_COOKIE
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter


PATH = "/api/v2/market/volume-profile"


def effective_routes(routes):
    # Newer FastAPI keeps include_router branches lazy; their effective
    # contexts contain the combined mount prefix, methods and real endpoint.
    # Older versions expose already-flattened APIRoute objects directly.
    iterator = getattr(fastapi_routing, "iter_route_contexts", None)
    if iterator is not None:
        yield from iterator(routes)
        return
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            yield from contexts()
        else:
            yield route


def test_formal_app_registers_only_read_volume_profile_route_without_opening_database(monkeypatch):
    # Do not inherit deployment credentials or optional provider configuration.
    for name in tuple(os.environ):
        if name.startswith("GATESCOPE_"):
            monkeypatch.delenv(name)
    encoded = base64.urlsafe_b64encode(b"k"*32).decode()
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused:unused@localhost/unused")
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEK", encoded)
    monkeypatch.setenv("GATESCOPE_RECOVERY_PEPPER", encoded)
    monkeypatch.setenv("GATESCOPE_CURSOR_SIGNING_KEY", encoded)
    monkeypatch.setenv("GATESCOPE_IDENTITY_KEY_VERSION", "vpvr-route-test-v1")
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://testserver")
    forbidden = Mock(side_effect=AssertionError("contract test must not open or query a database"))
    for method in ("open", "transaction", "public_transaction", "read_transaction"):
        monkeypatch.setattr(f"apps.api.formal_app.PostgresPool.{method}", forbidden)

    # Deliberately do not enter TestClient/lifespan: this checks formal assembly,
    # and the explicit guards fail if assembly ever starts touching a database.
    app = create_formal_app()
    matching = [route for route in effective_routes(app.routes) if getattr(route, "path", None) == PATH]
    assert len(matching) == 1
    assert isinstance(getattr(matching[0], "original_route", matching[0]), APIRoute)
    assert matching[0].methods == {"GET"}
    assert matching[0].endpoint.__module__ == "apps.api.volume_profile"
    schema_operations = app.openapi()["paths"][PATH]
    assert set(schema_operations) & {"get", "post", "put", "patch", "delete", "head", "options", "trace"} == {"get"}
    assert PATH not in SecurityMiddleware.PUBLIC_EXACT
    assert PATH not in SecurityMiddleware.PARTIAL_AUTH_EXACT
    forbidden.assert_not_called()


@pytest.mark.parametrize("token,status,code", [
    (None, 401, "AUTH_REQUIRED"),
    ("fixture-partial-session", 403, "MFA_REQUIRED"),
    ("fixture-complete-session", 200, None),
])
def test_security_middleware_blocks_anonymous_and_incomplete_mfa_before_repository(monkeypatch, token, status, code):
    tenant, user, instrument = uuid4(), uuid4(), uuid4()
    sessions = {
        "fixture-partial-session": SimpleNamespace(session_id=uuid4(), tenant_id=tenant, user_id=user, fully_authenticated=False),
        "fixture-complete-session": SimpleNamespace(session_id=uuid4(), tenant_id=tenant, user_id=user, fully_authenticated=True),
    }
    identity = Mock()
    identity.session.side_effect = sessions.get
    identity.valid_csrf.return_value = False
    repository = Mock()
    repository.get.return_value = {"version": "fine-ohlcv-profile-1", "expires_at": int(time.time())+30}
    monkeypatch.setattr("apps.api.volume_profile.PostgresVolumeProfileRepository", lambda _: repository)
    app = FastAPI()
    app.include_router(build_volume_profile_router(None))
    app.add_middleware(SecurityMiddleware, auth=identity, public_origin="http://testserver",
                       cookie_secure=False, limiter=SlidingWindowLimiter())
    with TestClient(app, base_url="http://testserver") as client:
        if token is not None:
            client.cookies.set(SESSION_COOKIE, token)
        response = client.get(PATH, params={"instrument_id": str(instrument), "timeframe": "1h",
                                           "from_time": 1_700_000_040, "to_time": 1_700_000_340,
                                           "rows": 48, "value_area": 70})
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    identity.session.assert_called_once_with(token)
    identity.valid_csrf.assert_not_called()  # A read, not a hidden write/CSRF bypass.
    if code is not None:
        assert response.json()["error"]["code"] == code
        repository.get.assert_not_called()
    else:
        assert response.json()["version"] == "fine-ohlcv-profile-1"
        repository.get.assert_called_once()
        assert repository.get.call_args.args[0] == instrument
