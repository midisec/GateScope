from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.volume_profile import ProfileBusy, ProfileResultCache, build_volume_profile_router


def app_client(monkeypatch, *, authenticated=True, repository=None):
    repository = repository or Mock()
    repository.get.return_value = {"version": "fine-ohlcv-profile-1", "expires_at": 2_000_000_030}
    monkeypatch.setattr("apps.api.volume_profile.PostgresVolumeProfileRepository", lambda _: repository)
    monkeypatch.setattr("apps.api.volume_profile.time.time", lambda: 2_000_000_000)
    app = FastAPI()
    user, tenant = uuid4(), uuid4()
    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if authenticated:
            request.state.auth_session = SimpleNamespace(user_id=request.headers.get("x-test-user", str(user)), tenant_id=tenant)
        return await call_next(request)
    app.include_router(build_volume_profile_router(None))
    return TestClient(app), repository


def params(**changes):
    return {"instrument_id": str(uuid4()), "timeframe": "1h", "from_time": 1_800_000_000,
            "to_time": 1_800_000_300, "rows": 48, "value_area": 70, **changes}


def test_auth_required_even_if_route_mounted_without_security_middleware(monkeypatch):
    client, repository = app_client(monkeypatch, authenticated=False)
    with client:
        assert client.get("/api/v2/market/volume-profile", params=params()).status_code == 401
    repository.get.assert_not_called()


def test_get_query_integer_rows_cache_binding_and_read_only_methods(monkeypatch):
    client, repository = app_client(monkeypatch)
    query = params()
    with client:
        first = client.get("/api/v2/market/volume-profile", params=query)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-store"
        assert client.get("/api/v2/market/volume-profile", params=query).status_code == 200
        assert repository.get.call_count == 1
        assert client.get("/api/v2/market/volume-profile", params={**query, "rows": 96}).status_code == 200
        assert repository.get.call_count == 2
        assert client.post("/api/v2/market/volume-profile", json=query).status_code == 405


@pytest.mark.parametrize("changes", [{"instrument_id": "bad"}, {"rows": 25}, {"rows": 500}, {"timeframe": "2m"},
                                    {"value_area": 96}, {"from_time": -1}, {"to_time": 0},
                                    {"to_time": 2_000_100_000}, {"from_time": "1e999"}])
def test_invalid_queries_never_reach_database(monkeypatch, changes):
    client, repository = app_client(monkeypatch)
    with client:
        assert client.get("/api/v2/market/volume-profile", params=params(**changes)).status_code == 422
    repository.get.assert_not_called()


def test_user_quota_applies_before_cache_hit_and_is_user_scoped(monkeypatch):
    client, repository = app_client(monkeypatch)
    query = params()
    with client:
        for _ in range(60):
            assert client.get("/api/v2/market/volume-profile", params=query).status_code == 200
        limited = client.get("/api/v2/market/volume-profile", params=query)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
        assert client.get("/api/v2/market/volume-profile", params=query, headers={"x-test-user": str(uuid4())}).status_code == 200
    assert repository.get.call_count == 1


@pytest.mark.parametrize("exception,status", [(LookupError("private instrument sql"), 404), (RuntimeError("password=secret SELECT private"), 503)])
def test_errors_are_safe_and_failed_jobs_are_released(monkeypatch, exception, status):
    client, repository = app_client(monkeypatch)
    repository.get.side_effect = exception
    query = params()
    with client:
        response = client.get("/api/v2/market/volume-profile", params=query)
        assert response.status_code == status
        assert "secret" not in response.text and "SELECT" not in response.text and "sql" not in response.text
        repository.get.side_effect = None
        assert client.get("/api/v2/market/volume-profile", params=query).status_code == 200


def test_cache_capacity_ttl_same_key_dedup_and_two_concurrent_jobs(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("apps.api.volume_profile.time.time", lambda: clock[0])
    monkeypatch.setattr("apps.api.volume_profile.time.monotonic", lambda: clock[0])
    cache = ProfileResultCache()
    calls = []
    def factory():
        calls.append(1)
        return {"expires_at": clock[0]+30}
    for index in range(70):
        cache.get((index,), factory)
    assert len(cache._cache) == 64
    cache.get((69,), factory)
    assert len(calls) == 70
    clock[0] += 31
    cache.get((69,), factory)
    assert len(calls) == 71 and len(cache._cache) == 1
    started_one, started_two, release = Event(), Event(), Event()
    def blocked(started):
        started.set()
        assert release.wait(2)
        return {"expires_at": clock[0]+30}
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(cache.get, ("one",), lambda: blocked(started_one))
        two = executor.submit(cache.get, ("two",), lambda: blocked(started_two))
        try:
            assert started_one.wait(2) and started_two.wait(2)
            with pytest.raises(ProfileBusy):
                cache.get(("one",), factory)
            with pytest.raises(ProfileBusy):
                cache.get(("three",), factory)
        finally:
            release.set()
        one.result(timeout=2)
        two.result(timeout=2)
    assert not cache._pending
