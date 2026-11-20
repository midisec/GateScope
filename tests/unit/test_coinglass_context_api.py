from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.coinglass_context import build_coinglass_context_router


def test_chart_refreshes_do_not_consume_review_submission_budget(monkeypatch):
    repo = Mock()
    repo.get.return_value = {"status": "QUEUED"}
    repo.review.return_value = {"review_id": str(uuid4()), "review": {}}
    monkeypatch.setattr("apps.api.coinglass_context.PostgresCoinglassContextRepository", lambda _: repo)
    app = FastAPI()
    user = uuid4()
    tenant = uuid4()
    instrument = uuid4()

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        request.state.auth_session = SimpleNamespace(user_id=user, tenant_id=tenant)
        return await call_next(request)

    app.include_router(build_coinglass_context_router(None))
    with TestClient(app) as client:
        for _ in range(20):
            assert (
                client.get(f"/api/v2/coinglass/chart?instrument_id={instrument}&timeframe=1m").status_code
                == 200
            )
        payload = {
            "instrument_id": str(instrument),
            "timeframe": "1m",
            "context_id": "a" * 64,
            "candidate_id": "b" * 64,
        }
        for _ in range(12):
            assert client.post("/api/v2/coinglass/reviews", json=payload).status_code == 201
        assert client.post("/api/v2/coinglass/reviews", json=payload).status_code == 429
