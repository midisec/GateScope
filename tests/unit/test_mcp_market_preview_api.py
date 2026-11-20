from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.errors import QueryCanceled

import apps.api.mcp_market_preview as preview


IDENTIFIER = "30000000-0000-0000-0000-000000000033"


def client(monkeypatch: pytest.MonkeyPatch, session: object = None, timeout: bool = False) -> tuple[TestClient, list[tuple]]:
    calls: list[tuple] = []

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        def market_preview(self, tenant, user, **kwargs):
            calls.append((tenant, user, kwargs))
            if timeout:
                raise QueryCanceled("private query details")
            return {"version": "market-analysis-1", "research_only": True, "items": [], "coinglass": {"status": "NOT_CACHED"}}

    monkeypatch.setattr(preview, "McpAnalysisService", Service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        if session is not None:
            request.state.auth_session = session
        request.state.trace_id = "safe-test-trace"
        return await call_next(request)

    app.include_router(preview.build_mcp_market_preview_router(object()))
    return TestClient(app), calls


@pytest.mark.parametrize("session", [None, SimpleNamespace(fully_authenticated=False)])
def test_preview_requires_fully_authenticated_session(monkeypatch, session):
    browser, calls = client(monkeypatch, session)
    assert browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}").status_code == 401
    assert calls == []


def test_preview_reads_current_owner_projection_not_synthetic_token(monkeypatch):
    session = SimpleNamespace(fully_authenticated=True, tenant_id=UUID(int=2), user_id=UUID(int=3))
    browser, calls = client(monkeypatch, session)
    response = browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}&timeframe=1h&limit=100")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls == [(session.tenant_id, session.user_id, {"instrument_id": IDENTIFIER, "timeframe": "1h", "bar_limit": 100})]
    assert response.json()["access_mode"] == "AUTHENTICATED_WEB_PREVIEW_NOT_EXTERNAL_MCP_AUTH"
    assert response.json()["trace_id"] == "safe-test-trace"


@pytest.mark.parametrize("suffix", ["limit=49", "limit=2001", "timeframe=3m"])
def test_preview_validates_window_before_read(monkeypatch, suffix):
    session = SimpleNamespace(fully_authenticated=True, tenant_id=UUID(int=2), user_id=UUID(int=3))
    browser, calls = client(monkeypatch, session)
    assert browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}&{suffix}").status_code == 422
    assert calls == []


def test_preview_has_bounded_per_user_rate_limit(monkeypatch):
    session = SimpleNamespace(fully_authenticated=True, tenant_id=UUID(int=2), user_id=UUID(int=3))
    browser, calls = client(monkeypatch, session)
    for _ in range(12):
        assert browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}").status_code == 200
    denied = browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}")
    assert denied.status_code == 429 and int(denied.headers["retry-after"]) > 0
    assert len(calls) == 12


def test_preview_timeout_has_safe_retry_response(monkeypatch):
    session = SimpleNamespace(fully_authenticated=True, tenant_id=UUID(int=2), user_id=UUID(int=3))
    browser, _ = client(monkeypatch, session, timeout=True)
    response = browser.get(f"/api/v2/mcp/market-preview?instrument_id={IDENTIFIER}")
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "private query" not in response.text
