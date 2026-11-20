from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api import pattern_studies
from gatescope.domain.current_patterns import summarize_current_records


INSTRUMENT = UUID(int=1)


@pytest.fixture
def api_client(monkeypatch):
    calls = []

    class Repository:
        def current(self, instrument, timeframe, **filters):
            calls.append((instrument, timeframe, filters))
            if filters["query"] == "history-gap":
                raise ValueError("PATTERN_HISTORY_GAP")
            return {"items": [], "total": 0, "summary": summarize_current_records([])}

    monkeypatch.setattr(pattern_studies, "PatternStudyRepository", lambda pool: Repository())
    app = FastAPI()

    @app.middleware("http")
    async def context(request: Request, call_next):
        request.state.auth_session = SimpleNamespace(tenant_id=UUID(int=2), user_id=UUID(int=3))
        return await call_next(request)

    app.include_router(pattern_studies.build_pattern_study_router(None))
    with TestClient(app) as client:
        yield client, calls


def test_current_api_forwards_inclusive_range_exact_pattern_and_pagination(api_client):
    client, calls = api_client
    result = client.get("/api/v2/patterns/current", params={"instrument_id": str(INSTRUMENT), "timeframe": "1h",
        "confidence_min": 0, "confidence_max": 1, "pattern": "future_v2", "family": "wave",
        "phase": "FORMING", "offset": 96, "limit": 12, "q": "wave"})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["summary"]["total"] == 0
    assert calls == [(INSTRUMENT, "1h", {"offset": 96, "limit": 12, "phase": "FORMING", "family": "wave",
        "query": "wave", "pattern": "future_v2", "confidence_min": 0., "confidence_max": 1., "unscored_only": False})]


@pytest.mark.parametrize("params", [
    {"confidence_min": "nan"}, {"confidence_min": "NaN"}, {"confidence_max": "Infinity"},
    {"confidence_min": "-inf"}, {"confidence_max": "inf"}, {"confidence_min": "-0.01"},
    {"confidence_max": "1.01"}, {"confidence_min": ".9", "confidence_max": ".8"},
    {"unscored_only": "true", "confidence_min": "0"}, {"unscored_only": "true", "confidence_max": "1"},
    {"unscored_only": "invalid"}, {"pattern": ""}, {"pattern": "a" * 81},
])
def test_invalid_current_filters_are_422_never_history_409_or_repo_calls(api_client, params):
    client, calls = api_client
    response = client.get("/api/v2/patterns/current", params={"instrument_id": str(INSTRUMENT), "timeframe": "1h", **params})
    assert response.status_code == 422
    assert calls == []
    assert "历史 K 线存在缺口" not in response.text


def test_default_unscored_mode_and_equal_range_endpoints_are_valid(api_client):
    client, calls = api_client
    base = {"instrument_id": str(INSTRUMENT), "timeframe": "1h"}
    for options in ({}, {"unscored_only": "true"}, {"confidence_min": "0", "confidence_max": "0"},
                    {"confidence_min": "1", "confidence_max": "1"}):
        assert client.get("/api/v2/patterns/current", params={**base, **options}).status_code == 200
    assert calls[0][2]["confidence_min"] is calls[0][2]["confidence_max"] is None
    assert calls[0][2]["unscored_only"] is False
    assert calls[1][2]["unscored_only"] is True


def test_unrelated_history_error_retains_existing_409(api_client):
    client, calls = api_client
    response = client.get("/api/v2/patterns/current", params={"instrument_id": str(INSTRUMENT), "timeframe": "1h", "q": "history-gap"})
    assert response.status_code == 409 and len(calls) == 1
