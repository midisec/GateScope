import importlib
import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

ACCOUNT, OTHER = str(UUID(int=1)), str(UUID(int=2))
OWNER, TENANT = UUID(int=3), UUID(int=4)
ORDER_A, ORDER_B = str(UUID(int=5)), str(UUID(int=6))
ANCHOR = "2026-09-14T10:00:00+00:00"
NAME = "MCP模拟验收-fixture"
SECRET = "secret-must-not-appear"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("verify_paper_read_release")


class FakeHttp:
    def __init__(self, module, *, corrupt=None):
        self.module, self.corrupt = module, corrupt
        self.headers, self.calls = {}, []
        self.revoked = False
        self.revoked_probes = 0

    def factory(self, **kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["headers"]["Authorization"] == f"Bearer {SECRET}"
        self.headers = kwargs["headers"]
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, *, json):
        assert url == "https://example.test/mcp"
        if self.revoked:
            assert json["method"] == "tools/list"
            self.revoked_probes += 1
            return SimpleNamespace(status_code=200 if self.corrupt == "revocation" else 401)
        value = self.reply(json)
        return SimpleNamespace(status_code=200, json=lambda: {"result": value}, raise_for_status=lambda: None)

    def reply(self, request):
        method = request["method"]
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "serverInfo": {
                "version": "1.3.0" if self.corrupt == "version" else "1.4.0"}}
        if method == "notifications/initialized":
            return {}
        if method == "tools/list":
            tools = []
            for name in (*self.module.PAPER_READ_TOOLS, *self.module.PAPER_WRITE_TOOLS):
                if self.corrupt == "missing_tool" and name == "paper_order_history":
                    continue
                props = {"order_id": {}, "pattern_index": {}, "cursor": {}, "limit": {},
                         "pagination": {"enum": ["OFFSET", "CURSOR"]}}
                readonly = name in self.module.PAPER_READ_TOOLS
                if self.corrupt == "annotation" and name == "paper_order_chart":
                    readonly = False
                tools.append({"name": name, "annotations": {"readOnlyHint": readonly},
                              "inputSchema": {"properties": props}})
            return {"tools": tools}
        assert method == "tools/call"
        name, args = request["params"]["name"], request["params"]["arguments"]
        assert name in self.module.CALL_TOOLS
        assert not name.startswith("market_") and name != "paper_order_preview"
        self.calls.append((name, args))
        body = {"environment": "PAPER"}
        if name == "paper_account_snapshot":
            assert args == {"account_id": ACCOUNT}
            body.update(account={"id": ACCOUNT, "name": NAME},
                        balance={key: "92731.12345678" for key in (
                            "cash_balance", "available_balance", "position_margin", "order_reserved_margin")},
                        execution_health={"state": "STALE", "not_a_hard_realtime_guarantee": True})
        elif name in self.module.LIST_TOOLS or name == "paper_order_history":
            assert args["limit"] == 1
            assert args.get("account_id", ACCOUNT) == ACCOUNT
            history = name == "paper_order_history"
            time_key = ("filled_at" if args.get("kind") == "fills" else "created_at") if history else self.module.LIST_TOOLS[name]
            identities = [ORDER_B, ORDER_A] if name == "paper_order_list" else [str(UUID(int=10)), str(UUID(int=11))]
            if name == "paper_position_list":
                identities = identities[:1]
            rows = [{"id": value, "account_id": ACCOUNT, "order_id": ORDER_B, "decision_status": "UNASSOCIATED",
                     time_key: f"2026-09-14T09:00:0{i if history else 2-i}+00:00"}
                    for i, value in enumerate(identities)]
            index = int(args.get("cursor", "0"))
            items = rows[index:index+1]
            if self.corrupt == "owner" and name == "paper_order_list":
                items[0]["account_id"] = OTHER
            more = index + 1 < len(rows)
            anchor = ANCHOR if not (self.corrupt == "moving_anchor" and index) else "2026-09-14T10:00:01+00:00"
            body.update(items=items, limit=1, pagination="CURSOR", snapshot_at=anchor,
                        has_more=more, next_cursor=str(index+1) if more else None, next_offset=None)
        elif name == "paper_order_get":
            assert args["order_id"] == ORDER_B
            body.update(id=ORDER_B, account_id=ACCOUNT, decision_status="UNASSOCIATED", decision_snapshot=None,
                        decision_snapshot_id=None, history_limit=100, fills=[], events=[],
                        fills_next_cursor=None, events_next_cursor=None, fills_has_more=False, events_has_more=False)
        elif name == "paper_order_chart":
            assert args == {"order_id": ORDER_B, "pattern_index": 0}
            body["error"] = "PAPER_DECISION_UNASSOCIATED"
            if self.corrupt == "wrong_error":
                body["error"] = "PAPER_OUTCOME_UNKNOWN"
            return {"structuredContent": body, "isError": self.corrupt != "chart_success",
                    "content": [{"type": "image", "data": "fake-current-image"}] if self.corrupt == "current_image" else []}
        else:
            raise AssertionError("unlisted tool")
        return {"structuredContent": body, "content": []}


class FakePool:
    def __init__(self, name=NAME):
        self.name, self.inserted = name, []

    @contextmanager
    def read_transaction(self, tenant, owner):
        assert (tenant, owner) == (TENANT, OWNER)
        yield self

    transaction = read_transaction

    def execute(self, sql, values):
        if "SELECT name FROM paper_accounts" in sql:
            assert values == (UUID(ACCOUNT), TENANT, OWNER)
            assert "tenant_id=%s" in sql and "owner_user_id=%s" in sql
            return SimpleNamespace(fetchone=lambda: (self.name,) if self.name else None)
        assert "INSERT INTO mcp_clients" in sql
        assert values[1:3] == (TENANT, OWNER)
        assert values[3].startswith("deployment-paper-read-")
        self.inserted.append(values[0])
        return None


def harness(module, monkeypatch, *, corrupt=None, name=NAME, token_failure=False):
    pool, http = FakePool(name), FakeHttp(module, corrupt=corrupt)
    issued, revoked = [], []

    class Repository:
        def __init__(self, selected):
            assert selected is pool

        def create_token(self, tenant, owner, client_id, *, label, scopes, ttl):
            assert (tenant, owner) == (TENANT, OWNER)
            assert scopes == ("mcp:connect", "paper:read") and ttl == timedelta(hours=1)
            assert client_id == pool.inserted[0]
            issued.append(client_id)
            if token_failure:
                raise RuntimeError(SECRET)
            return SimpleNamespace(secret=SECRET, token=SimpleNamespace(scopes=scopes))

        def revoke_client(self, tenant, owner, client_id):
            assert (tenant, owner) == (TENANT, OWNER)
            assert client_id == pool.inserted[0]
            revoked.append(client_id)
            http.revoked = True
            return SimpleNamespace(status="REVOKED")

        def list_tokens(self, tenant, owner, client_id):
            assert client_id in revoked
            return [SimpleNamespace(revoked_at=ANCHOR)]

    monkeypatch.setattr(module, "resolve_owner", lambda p, username: (TENANT, OWNER))
    monkeypatch.setattr(module, "PostgresMcpRepository", Repository)
    return pool, http, issued, revoked


def test_read_release_uses_only_scoped_reads_and_redacts_all_values(module, monkeypatch):
    pool, http, issued, revoked = harness(module, monkeypatch)
    report = module.verify(pool, "https://example.test/mcp", "owner", ACCOUNT, client_factory=http.factory)
    assert report["ok"] and report["read_only_scopes_verified"]
    assert report["list_types_checked"] == 3 and report["history_types_checked"] == 2
    assert report["cursor_pages_checked"] == 9 and report["cursor_continuations_checked"] == 4
    assert report["legacy_chart_unavailable_verified"] and report["revoked_token_denied"]
    assert len(pool.inserted) == 1 and pool.inserted == issued == revoked
    assert http.revoked_probes == 1
    assert all(type(value) in (bool, int) for value in report.values())
    text = json.dumps(report)
    assert all(secret not in text for secret in (SECRET, "92731.12345678", ACCOUNT, ORDER_B, NAME))
    assert not ({name for name, _ in http.calls} & set(module.PAPER_WRITE_TOOLS))


@pytest.mark.parametrize("corrupt", ["version", "missing_tool", "annotation", "owner", "moving_anchor",
                                    "wrong_error", "chart_success", "current_image", "revocation"])
def test_failure_still_revokes_exact_temporary_client_without_exposing_reply(module, monkeypatch, corrupt):
    pool, http, issued, revoked = harness(module, monkeypatch, corrupt=corrupt)
    report = module.verify(pool, "https://example.test/mcp", "owner", ACCOUNT, client_factory=http.factory)
    assert report["ok"] is False and report["temporary_client_revoked"]
    assert len(revoked) == 1 and revoked == issued and http.revoked_probes == 1
    assert all(type(value) in (bool, int) for value in report.values())
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("name", [None, "用户的模拟账户"])
def test_non_smoke_or_foreign_account_fails_before_credential_or_http(module, monkeypatch, name):
    pool, http, issued, revoked = harness(module, monkeypatch, name=name)
    report = module.verify(pool, "https://example.test/mcp", "owner", ACCOUNT, client_factory=http.factory)
    assert not report["ok"] and not report["dedicated_account_verified"]
    assert not pool.inserted and not issued and not revoked and not http.calls


def test_token_creation_failure_still_revokes_new_client(module, monkeypatch):
    pool, http, _, revoked = harness(module, monkeypatch, token_failure=True)
    report = module.verify(pool, "https://example.test/mcp", "owner", ACCOUNT, client_factory=http.factory)
    assert not report["ok"] and report["temporary_client_revoked"] and len(revoked) == 1
    assert not http.calls and SECRET not in json.dumps(report)


@pytest.mark.parametrize("url", ["http://example.test/mcp", "https://example.test/mcp?key=secret",
                                 "https://user:secret@example.test/mcp", "https://example.test/other"])
def test_bad_endpoint_rejected_before_side_effects(module, monkeypatch, url):
    pool, http, issued, revoked = harness(module, monkeypatch)
    report = module.verify(pool, url, "owner", ACCOUNT, client_factory=http.factory)
    assert not report["ok"] and not pool.inserted and not issued and not revoked and not http.calls
