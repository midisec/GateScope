from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.mcp_server.paper import PAPER_READ_TOOLS, PAPER_WRITE_TOOLS, register_paper_tools
from gatescope.adapters.postgres_mcp import VerifiedMcpToken


class Registry:
    def __init__(self):
        self.tools, self.annotations = {}, {}

    def tool(self, name, annotations, **kwargs):
        def register(function):
            self.tools[name], self.annotations[name] = function, annotations
            return function
        return register


def setup(scopes=("paper:read", "paper:trade"), failure=None):
    calls, audits = [], []
    identity = VerifiedMcpToken(uuid4(), uuid4(), uuid4(), uuid4(), tuple(scopes),
                               datetime.now(timezone.utc)+timedelta(hours=1))

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        if failure:
            raise failure
        return {"environment": "PAPER", "items": [], "accepted": True}

    def auditing(principal, **kwargs):
        assert principal == identity
        audits.append(kwargs)

    repository = SimpleNamespace(**{name: operation for name in ("list_orders", "list_positions", "list_fills", "get_order")})
    service = SimpleNamespace(repository=repository, **{name: operation for name in (
        "account_list", "account_snapshot", "preview", "submit", "cancel", "close")})
    server = Registry()
    register_paper_tools(server, service, lambda: identity, SimpleNamespace(audit_invocation=auditing), "https://example.test")
    return server, calls, audits, identity


def opening():
    return {"account_id": str(uuid4()), "instrument_id": str(uuid4()), "side": "LONG", "contracts": 1,
            "stop_price": "90", "take_profit_price": "120", "idempotency_key": "paper-test-order-001"}


def test_exact_manifest_mutability_and_read_default_do_not_gain_write():
    server, calls, _, _ = setup(("market:read", "analysis:read"))
    assert set(server.tools) == set(PAPER_READ_TOOLS) | set(PAPER_WRITE_TOOLS)
    for name, annotations in server.annotations.items():
        assert annotations.read_only_hint == (name in PAPER_READ_TOOLS)
        assert annotations.destructive_hint == (name in PAPER_WRITE_TOOLS)
        assert annotations.idempotent_hint
    denied = server.tools["paper_order_submit"](**opening(), confirm_paper=True)
    assert denied.is_error and denied.structured_content["error"] == "PAPER_SCOPE_REQUIRED"
    assert 'scope="mcp:connect paper:read paper:trade"' in denied.meta["mcp/www_authenticate"][0]
    assert not calls


def test_paper_read_cannot_submit_and_confirmation_is_mandatory():
    server, calls, _, _ = setup(("paper:read",))
    assert server.tools["paper_order_submit"](**opening(), confirm_paper=True).is_error
    assert not calls
    server, calls, _, _ = setup()
    result = server.tools["paper_order_submit"](**opening(), confirm_paper=False)
    assert result.structured_content["error"] == "PAPER_CONFIRMATION_REQUIRED" and not calls


@pytest.mark.parametrize("name,kwargs", [
    ("paper_account_list", {}), ("paper_account_snapshot", {"account_id": str(uuid4())}),
    ("paper_order_list", {"account_id": str(uuid4()), "status": "ACTIVE"}),
    ("paper_position_list", {"account_id": str(uuid4()), "status": "OPEN"}),
    ("paper_fill_list", {"account_id": str(uuid4())}), ("paper_order_get", {"order_id": str(uuid4())}),
    ("paper_order_preview", opening()), ("paper_order_submit", {**opening(), "confirm_paper": True}),
    ("paper_order_cancel", {"order_id": str(uuid4()), "idempotency_key": "paper-test-cancel-01", "confirm_paper": True}),
    ("paper_position_close", {"position_id": str(uuid4()), "idempotency_key": "paper-test-close-01", "confirm_paper": True}),
])
def test_all_tool_handlers_bind_owner_and_only_explicit_request_arguments(name, kwargs):
    server, calls, audits, identity = setup()
    result = server.tools[name](**kwargs)
    assert not result.is_error, result.structured_content
    assert len(calls) == 1 and calls[0][0][:2] == (identity.tenant_id, identity.subject_user_id)
    assert len(audits) == 2 and [a["outcome"] for a in audits] == ["REQUESTED", "SUCCESS"]
    assert not {"invoke", "service", "listing"} & audits[0]["arguments"].keys()
    if name == "paper_order_submit":
        assert calls[0][1] == {"source": "MCP"}
        assert calls[0][0][2]["contracts"] == 1 and "confirm_paper" not in calls[0][0][2]


def test_internal_error_does_not_leak_and_describes_uncertainty():
    server, _, _, _ = setup(failure=RuntimeError("SECRET database error"))
    result = server.tools["paper_order_submit"](**opening(), confirm_paper=True)
    assert result.is_error and result.structured_content["error"] == "PAPER_OUTCOME_UNKNOWN"
    assert "SECRET" not in result.content[0].text


def test_post_commit_audit_failure_keeps_success_not_a_fake_rejection():
    server = Registry()
    calls = []
    def audit(*args, **kwargs):
        if kwargs["outcome"] == "SUCCESS":
            raise RuntimeError("unavailable")
    identity = SimpleNamespace(scopes=("paper:read", "paper:trade"), tenant_id=uuid4(), subject_user_id=uuid4())
    service = SimpleNamespace(account_list=lambda *args: calls.append(args) or {"items": []})
    register_paper_tools(server, service, lambda: identity, SimpleNamespace(audit_invocation=audit), "https://example.test")
    result = server.tools["paper_account_list"]()
    assert not result.is_error and result.structured_content["audit_warning"]
    assert len(calls) == 1


def test_original_paper_chart_returns_real_image_content_and_owner_bound_lookup(monkeypatch):
    import base64
    import gatescope.application.paper_decision_chart as drawing
    server, calls, audits, identity = setup(("paper:read",))
    png = b"\x89PNG\r\n\x1a\nfixture"
    monkeypatch.setattr(drawing, "render_paper_decision_chart", lambda order, pattern_index:
        (png, {"decision_snapshot_id": "frozen-id", "pattern_index": pattern_index}))
    result = server.tools["paper_order_chart"](str(uuid4()), pattern_index=1)
    assert not result.is_error and result.structured_content["image_status"] == "READY"
    assert [content.type for content in result.content] == ["text", "image"]
    assert base64.b64decode(result.content[1].data) == png
    assert "_image_base64" not in result.content[0].text
    assert calls[0][0][:2] == (identity.tenant_id, identity.subject_user_id)
    assert audits[0]["tool_name"] == "paper_order_chart"
