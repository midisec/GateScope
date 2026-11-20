from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    path = tmp_path_factory.mktemp("api") / "api.db"
    env = {
        "GATESCOPE_DATABASE_URL": "",  # Always use this fixture's isolated SQLite file.
        "GATESCOPE_DB_PATH": str(path),
        "GATESCOPE_BOOTSTRAP_USERNAME": "owner",
        "GATESCOPE_BOOTSTRAP_PASSWORD": "correct-horse-battery-staple",
        "GATESCOPE_PUBLIC_ORIGIN": "http://testserver",
        "GATESCOPE_COOKIE_SECURE": "false",
        "GATESCOPE_START_WORKERS": "false",
        "LIVE_TRADING_ENABLED": "false",
        "GATESCOPE_EXECUTION_ENVIRONMENT": "PAPER",
    }
    import os

    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    with TestClient(module.app, base_url="http://testserver") as test_client:
        yield test_client
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def login(client: TestClient) -> None:
    response = client.post(
        "/auth/login",
        json={"username": "owner", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200


def test_business_api_denies_anonymous(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert "database" not in response.text.lower()


def test_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/health/live", headers={"Host": "attacker.invalid"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "HOST_REJECTED"


def test_health_is_public_and_discloses_no_path(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert "/" not in str(response.json().get("checks", {}).values())


def test_login_is_generic_and_sets_hardened_cookie(client: TestClient) -> None:
    denied = client.post("/auth/login", json={"username": "missing", "password": "not-the-correct-password"})
    assert denied.status_code == 401
    assert denied.json()["error"]["message"] == "账号或密码错误"

    login(client)
    assert client.cookies.get("gatescope_session")
    assert client.cookies.get("gatescope_csrf")
    headers = client.get("/api/status").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"


def test_csrf_blocks_state_change_then_allows_logout(client: TestClient) -> None:
    response = client.post("/auth/logout")
    assert response.status_code == 403
    csrf = client.cookies.get("gatescope_csrf")
    response = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert client.get("/api/status").status_code == 401


def test_status_is_redacted(client: TestClient) -> None:
    login(client)
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert "database" not in payload
    assert payload["runtime"] == {"environment": "PAPER", "live_trading_enabled": False}
