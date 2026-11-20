"""Actual HTTP routing and session boundaries; no exchange or database writes."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.paper_orders import build_paper_orders_router
from gatescope.adapters.postgres_paper import PaperNotFound
from gatescope.security import SecurityMiddleware, SlidingWindowLimiter

ORIGIN = "https://paper.example"
ACCOUNT = UUID("00000000-0000-4000-8000-000000000011")
INSTRUMENT = UUID("00000000-0000-4000-8000-000000000012")
ORDER = UUID("00000000-0000-4000-8000-000000000013")
POSITION = UUID("00000000-0000-4000-8000-000000000014")
HEADERS = {"X-CSRF-Token": "paper-csrf-test"}
OPEN_REQUEST = {
    "account_id": str(ACCOUNT), "instrument_id": str(INSTRUMENT), "timeframe": "5m",
    "side": "LONG", "order_type": "MARKET", "time_in_force": "IOC", "contracts": 2,
    "stop_price": "90", "take_profit_price": "120", "max_slippage_bps": "50",
    "ttl_seconds": 3600, "idempotency_key": "paper-http-test-key-1",
}


class Identity:
    current = None

    def session(self, _token):
        return self.current

    def valid_csrf(self, _session, cookie, header):
        return cookie == header == "paper-csrf-test"


class PaperService:
    def __init__(self):
        self.repository = self
        self.calls = []
        self.failure = None

    def record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.failure:
            raise self.failure
        return {"environment": "PAPER", "items": [], "has_more": False, "accepted": True}

    def account_snapshot(self, *args, **kwargs):
        return self.record("snapshot", *args, **kwargs)

    def list_orders(self, *args, **kwargs):
        return self.record("orders", *args, **kwargs)

    def list_positions(self, *args, **kwargs):
        return self.record("positions", *args, **kwargs)

    def list_fills(self, *args, **kwargs):
        return self.record("fills", *args, **kwargs)

    def get_order(self, *args, **kwargs):
        return self.record("order", *args, **kwargs)

    def submit(self, *args, **kwargs):
        return self.record("submit", *args, **kwargs)

    def preview(self, *args, **kwargs):
        return self.record("preview", *args, **kwargs)

    def cancel(self, *args, **kwargs):
        return self.record("cancel", *args, **kwargs)

    def close(self, *args, **kwargs):
        return self.record("close", *args, **kwargs)


@pytest.fixture
def paper_http(monkeypatch):
    service, identity = PaperService(), Identity()
    monkeypatch.setattr("apps.api.paper_orders.build_paper_execution", lambda _pool: service)
    application = FastAPI()
    application.add_middleware(SecurityMiddleware, auth=identity, public_origin=ORIGIN,
                               cookie_secure=True, limiter=SlidingWindowLimiter())
    application.include_router(build_paper_orders_router(None))
    with TestClient(application, base_url=ORIGIN) as client:
        yield client, service, identity


def sign_in(client, identity, *, role="OWNER", recent=True, full=True):
    now = datetime.now(timezone.utc)
    identity.current = SimpleNamespace(
        session_id=uuid4(), tenant_id=uuid4(), user_id=uuid4(), role=role,
        fully_authenticated=full, recent_auth_at=now if recent else now - timedelta(hours=2),
        mfa_verified_at=now,
    )
    client.cookies.set("gatescope_csrf", "paper-csrf-test")
    return identity.current


@pytest.mark.parametrize("kind", ["snapshot", "orders", "positions", "fills"])
def test_reads_bind_session_owner_and_account_with_no_store(paper_http, kind):
    client, service, identity = paper_http
    session = sign_in(client, identity, role="VIEWER")
    path = f"/api/v2/paper/accounts/{ACCOUNT}/{kind}"
    response = client.get(path, params={"limit": 10, "offset": 20, "owner_user_id": str(uuid4())})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["environment"] == "PAPER" and response.json()["trace_id"]
    name, args, kwargs = service.calls[-1]
    assert name == kind and args[:2] == (session.tenant_id, session.user_id)
    if kind == "snapshot":
        assert args[2] == ACCOUNT
    else:
        assert kwargs["account_id"] == ACCOUNT
        assert kwargs["limit"] == 10 and kwargs["offset"] == 20
        assert "owner_user_id" not in kwargs


def test_order_detail_is_owner_bound(paper_http):
    client, service, identity = paper_http
    session = sign_in(client, identity)
    response = client.get(f"/api/v2/paper/orders/{ORDER}")
    assert response.status_code == 200
    assert service.calls[-1] == ("order", (session.tenant_id, session.user_id, ORDER), {})


def test_preview_is_owner_bound_read_only_and_does_not_require_recent_password(paper_http):
    client, service, identity = paper_http
    session = sign_in(client, identity, role="VIEWER", recent=False)
    response = client.post("/api/v2/paper/orders:preview", json=OPEN_REQUEST, headers=HEADERS)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    name, args, kwargs = service.calls[-1]
    assert name == "preview" and args[:2] == (session.tenant_id, session.user_id)
    assert args[2]["account_id"] == str(ACCOUNT)
    assert not kwargs


def test_preview_cannot_skip_csrf_or_mfa(paper_http):
    client, service, identity = paper_http
    sign_in(client, identity)
    assert client.post("/api/v2/paper/orders:preview", json=OPEN_REQUEST).status_code == 403
    sign_in(client, identity, full=False)
    assert client.post("/api/v2/paper/orders:preview", json=OPEN_REQUEST, headers=HEADERS).status_code in (401,403)
    assert not service.calls


def test_original_chart_authenticates_owner_and_returns_private_png(paper_http, monkeypatch):
    client, service, identity = paper_http
    session = sign_in(client, identity, role="VIEWER")
    rendered = []
    monkeypatch.setattr("gatescope.application.paper_decision_chart.render_paper_decision_chart",
        lambda order, *, pattern_index: rendered.append(pattern_index) or (b"\x89PNGfixture", {}))
    response = client.get(f"/api/v2/paper/orders/{ORDER}/chart?pattern_index=2")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store" and rendered == [2]
    assert service.calls[-1][1] == (session.tenant_id, session.user_id, ORDER)
    service.failure = PaperNotFound("foreign private order")
    assert client.get(f"/api/v2/paper/orders/{ORDER}/chart").status_code == 404
    assert rendered == [2]


@pytest.mark.parametrize("kind,state,expected", [("orders", "ALL", None), ("positions", "ALL", None),
    ("positions", "CLOSED", "CLOSED"), ("positions", "OPEN", "OPEN"),
    ("orders", "PARTIALLY_FILLED", "PARTIALLY_FILLED")])
def test_all_state_means_no_filter_and_specific_state_is_preserved(paper_http, kind, state, expected):
    client, service, identity = paper_http
    sign_in(client, identity)
    assert client.get(f"/api/v2/paper/accounts/{ACCOUNT}/{kind}", params={"state": state}).status_code == 200
    assert service.calls[-1][2]["status"] == expected


@pytest.mark.parametrize("kind,query", [("orders", {"limit": 101}), ("orders", {"offset": -1}),
    ("positions", {"offset": 10001}), ("fills", {"limit": 0}),
    ("orders", {"state": "UNEXPECTED"}), ("positions", {"state": "FILLED"})])
def test_invalid_pagination_and_state_never_reach_storage(paper_http, kind, query):
    client, service, identity = paper_http
    sign_in(client, identity)
    assert client.get(f"/api/v2/paper/accounts/{ACCOUNT}/{kind}", params=query).status_code == 422
    assert not service.calls


@pytest.mark.parametrize("role", ["OWNER", "OPERATOR"])
def test_open_submission_binds_owner_and_forces_web_source(paper_http, role):
    client, service, identity = paper_http
    session = sign_in(client, identity, role=role)
    response = client.post("/api/v2/paper/orders", json=OPEN_REQUEST, headers=HEADERS)
    assert response.status_code == 200
    name, args, kwargs = service.calls[-1]
    assert name == "submit" and args[:2] == (session.tenant_id, session.user_id)
    assert args[2]["idempotency_key"] == OPEN_REQUEST["idempotency_key"]
    assert args[2]["contracts"] == 2 and kwargs == {"source": "WEB"}


@pytest.mark.parametrize("role", ["VIEWER", "RESEARCHER"])
@pytest.mark.parametrize("kind", ["open", "close", "cancel"])
def test_read_only_roles_cannot_submit_any_mutation(paper_http, role, kind):
    client, service, identity = paper_http
    sign_in(client, identity, role=role)
    path = {"open": "/api/v2/paper/orders", "close": f"/api/v2/paper/positions/{POSITION}:close", "cancel": f"/api/v2/paper/orders/{ORDER}:cancel"}[kind]
    payload = OPEN_REQUEST if kind == "open" else {"idempotency_key": "paper-read-only-test"}
    response = client.post(path, json=payload, headers=HEADERS)
    assert response.status_code == 403 and response.json()["error"]["code"] == "PAPER_ROLE_FORBIDDEN"
    assert not service.calls


def test_open_requires_recent_password_but_close_and_cancel_remain_available(paper_http):
    client, service, identity = paper_http
    session = sign_in(client, identity, recent=False)
    response = client.post("/api/v2/paper/orders", json=OPEN_REQUEST, headers=HEADERS)
    assert response.status_code == 403 and response.json()["error"]["code"] == "RECENT_AUTH_REQUIRED"
    assert not service.calls
    for path, kind, extra in [(f"/api/v2/paper/positions/{POSITION}:close", "close", {"contracts": 1}),
                             (f"/api/v2/paper/orders/{ORDER}:cancel", "cancel", {})]:
        response = client.post(path, json={"idempotency_key": "paper-risk-reduction-1", **extra}, headers=HEADERS)
        assert response.status_code == 200
        name, args, kwargs = service.calls[-1]
        assert name == kind and args[:2] == (session.tenant_id, session.user_id)
        assert args[2] == (POSITION if kind == "close" else ORDER)
        if kind == "close":
            assert args[3]["contracts"] == 1 and kwargs == {"source": "WEB"}


@pytest.mark.parametrize("changes", [
    {"contracts": 0}, {"contracts": "1"}, {"contracts": 1.5}, {"contracts": True},
    {"max_slippage_bps": "101"}, {"max_slippage_bps": "NaN"}, {"ttl_seconds": 86401},
    {"time_in_force": "FOK"}, {"order_type": "LIMIT"}, {"stop_price": "0"},
    {"take_profit_price": "Infinity"}, {"idempotency_key": "short"}, {"timeframe": "3h"},
    {"environment": "LIVE"}, {"owner_user_id": str(uuid4())}, {"source": "MCP"},
])
def test_invalid_or_authority_expanding_input_never_reaches_service(paper_http, changes):
    client, service, identity = paper_http
    sign_in(client, identity)
    assert client.post("/api/v2/paper/orders", json={**OPEN_REQUEST, **changes}, headers=HEADERS).status_code == 422
    assert not service.calls


def test_global_login_mfa_and_csrf_protection_still_applies(paper_http):
    client, service, identity = paper_http
    assert client.get(f"/api/v2/paper/accounts/{ACCOUNT}/snapshot").status_code == 401
    sign_in(client, identity, full=False)
    response = client.post("/api/v2/paper/orders", json=OPEN_REQUEST, headers=HEADERS)
    assert response.status_code == 403 and response.json()["error"]["code"] == "MFA_REQUIRED"
    sign_in(client, identity)
    response = client.post("/api/v2/paper/orders", json=OPEN_REQUEST)
    assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_REJECTED"
    assert not service.calls


def test_not_found_and_uncertain_outcome_never_leak_raw_storage_errors(paper_http):
    client, service, identity = paper_http
    sign_in(client, identity)
    service.failure = PaperNotFound("PAPER_RECORD_NOT_FOUND")
    response = client.get(f"/api/v2/paper/orders/{ORDER}")
    assert response.status_code == 404 and response.json()["error"]["code"] == "PAPER_NOT_FOUND"
    service.failure = RuntimeError("private-database-host-and-query-fixture")
    response = client.post("/api/v2/paper/orders", json=OPEN_REQUEST, headers=HEADERS)
    assert response.status_code == 503 and response.json()["error"]["code"] == "PAPER_OUTCOME_UNKNOWN"
    assert "private-database" not in response.text
