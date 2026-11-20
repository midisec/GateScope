from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import shadow_trades


def test_shadow_api_read_only_pagination_validation_and_owner_context(monkeypatch):
    owner, tenant = uuid4(), uuid4()
    calls = []

    class Repository:
        def list(self, requested_tenant, requested_owner, **filters):
            calls.append((requested_tenant, requested_owner, filters))
            return {"items": [], "total": 0, "has_more": False, "next_offset": None}

        def summary(self, requested_tenant, requested_owner, **filters):
            return {"environment": "SHADOW", "cohorts": []}

        def get(self, *args, **kwargs):
            raise LookupError("private unavailable details")

    monkeypatch.setattr(shadow_trades, "ShadowTradeRepository", lambda pool: Repository())
    app = FastAPI()

    @app.middleware("http")
    async def context(request: Request, call_next):
        request.state.auth_session = SimpleNamespace(tenant_id=tenant, user_id=owner)
        return await call_next(request)

    app.include_router(shadow_trades.build_shadow_router(None))
    with TestClient(app) as client:
        response = client.get("/api/v2/research/shadow-trades?cohort=DELIVERED&state=CLOSED&limit=10&offset=20")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert calls[0][0:2] == (tenant, owner)
        assert calls[0][2] == {"limit": 10, "offset": 20, "cohort": "DELIVERED", "state": "CLOSED", "instrument_id": None}
        assert client.get("/api/v2/research/shadow-trades?cohort=INVALID").status_code == 422
        assert client.get("/api/v2/research/shadow-trades?limit=10000").status_code == 422
        assert client.get("/api/v2/research/shadow-trades/summary").json()["environment"] == "SHADOW"
        assert client.post("/api/v2/research/shadow-trades", json={}).status_code == 405
        detail = client.get(f"/api/v2/research/shadow-trades/{uuid4()}")
        assert detail.status_code == 404 and "private" not in detail.text
