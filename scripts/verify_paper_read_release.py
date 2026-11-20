"""Bounded deployed MCP PAPER reads on an explicitly selected smoke account.

Creates one dedicated one-hour mcp:connect + paper:read credential, then revokes
that exact client in finally and checks HTTP 401. Does not create accounts or
trades, request prices, call OpenAI, or send Telegram. MCP read audits are normal.
Only booleans/counts are printed; credentials and returned account data stay in
memory. Example: uv run python scripts/verify_paper_read_release.py --account-id UUID
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2 as httpx

from apps.mcp_server.paper import PAPER_READ_TOOLS, PAPER_WRITE_TOOLS
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_market_export import result_payload, rpc
from verify_deployed_mcp import resolve_owner

SCOPES = ("mcp:connect", "paper:read")
LIST_TOOLS = {"paper_order_list": "created_at", "paper_position_list": "opened_at", "paper_fill_list": "filled_at"}
CALL_TOOLS = {*LIST_TOOLS, "paper_account_snapshot", "paper_order_get", "paper_order_history", "paper_order_chart"}
MAX_PAGES = 3


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    return parsed


def read_checks(url, secret, account_id, *, client_factory=None, report=None):
    """All network calls have a hard allowlist and at most three one-row pages."""
    if not __debug__:
        raise ValueError("verification assertions must be enabled")
    report = {} if report is None else report
    report.update({"version_verified": False, "manifest_verified": False, "snapshot_verified": False,
              "list_types_checked": 0, "cursor_pages_checked": 0, "cursor_continuations_checked": 0,
              "order_detail_verified": False, "history_types_checked": 0,
              "legacy_chart_unavailable_verified": False, "tool_calls": 0})
    factory = client_factory or httpx.Client
    with factory(headers={"Authorization": f"Bearer {secret}", "Accept": "application/json, text/event-stream"},
                 timeout=20, follow_redirects=False) as client:
        sequence = 1
        init = rpc(client, url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "paper-read-release-check", "version": "1"}}, sequence)
        assert init["serverInfo"]["version"] == "1.4.0"
        report["version_verified"] = True
        client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
        sequence += 1
        manifest = rpc(client, url, "tools/list", {}, sequence)["tools"]
        found = {tool["name"]: tool for tool in manifest}
        assert set(PAPER_READ_TOOLS) | set(PAPER_WRITE_TOOLS) <= found.keys()
        for name in (*PAPER_READ_TOOLS, *PAPER_WRITE_TOOLS):
            assert found[name]["annotations"]["readOnlyHint"] is (name in PAPER_READ_TOOLS)
        for name in (*LIST_TOOLS, "paper_order_history"):
            properties = found[name]["inputSchema"]["properties"]
            assert {"cursor", "limit"} <= properties.keys()
            if name in LIST_TOOLS:
                assert "CURSOR" in properties["pagination"]["enum"]
        assert {"order_id", "pattern_index"} <= found["paper_order_chart"]["inputSchema"]["properties"].keys()
        report["manifest_verified"] = True

        def call(name, arguments, *, expected_error=False):
            nonlocal sequence
            if name not in CALL_TOOLS:
                raise ValueError("only ledger reads are allowed")
            sequence += 1
            report["tool_calls"] += 1
            raw = rpc(client, url, "tools/call", {"name": name, "arguments": arguments}, sequence)
            if expected_error:
                assert raw.get("isError", raw.get("is_error")) is True
                assert not any(block.get("type") == "image" for block in raw.get("content", []))
                # Decode the declared error without treating it as a successful
                # chart; never accept today's image as missing historical proof.
                raw = {**raw, "isError": False, "is_error": False}
            result = result_payload(raw)
            assert result["environment"] == "PAPER"
            return result

        snapshot = call("paper_account_snapshot", {"account_id": account_id})
        assert snapshot["account"]["id"] == account_id
        assert snapshot["account"]["name"].startswith("MCP模拟验收-")
        for key in ("cash_balance", "available_balance", "position_margin", "order_reserved_margin"):
            assert Decimal(snapshot["balance"][key]).is_finite()
        health = snapshot["execution_health"]
        assert health["state"] in {"HEALTHY", "STALE", "DEGRADED", "STOPPED", "NO_RECENT_SUCCESS", "UNAVAILABLE"}
        assert health["not_a_hard_realtime_guarantee"] is True
        report["snapshot_verified"] = True

        def pages(name, arguments, time_key, *, descending, order_id=None):
            cursor = snapshot_at = previous = None
            seen, rows = set(), []
            for _ in range(MAX_PAGES):
                request = {**arguments, "limit": 1}
                if cursor is not None:
                    request["cursor"] = cursor
                page = call(name, request)
                assert page["pagination"] == "CURSOR" and page["limit"] == 1
                assert isinstance(page["items"], list) and len(page["items"]) <= 1
                assert type(page["has_more"]) is bool
                assert "next_cursor" in page and bool(page["next_cursor"]) is page["has_more"]
                assert page.get("next_offset") is None
                anchor = _timestamp(page["snapshot_at"])
                if snapshot_at is not None:
                    assert page["snapshot_at"] == snapshot_at
                    report["cursor_continuations_checked"] += 1
                snapshot_at = page["snapshot_at"]
                for row in page["items"]:
                    assert row["id"] not in seen
                    if name in LIST_TOOLS or arguments.get("kind") == "fills":
                        assert row["account_id"] == account_id
                    if arguments.get("kind") == "fills":
                        assert row["order_id"] == order_id
                    identity = (_timestamp(row[time_key]), UUID(row["id"]).int)
                    assert identity[0] <= anchor
                    if previous is not None:
                        assert identity < previous if descending else identity > previous
                    previous = identity
                    seen.add(row["id"])
                    rows.append(row)
                report["cursor_pages_checked"] += 1
                if not page["has_more"]:
                    break
                assert isinstance(page["next_cursor"], str) and page["next_cursor"] != cursor
                cursor = page["next_cursor"]
            return rows

        orders = []
        for name, time_key in LIST_TOOLS.items():
            rows = pages(name, {"account_id": account_id, "pagination": "CURSOR"}, time_key, descending=True)
            if name == "paper_order_list":
                orders = rows
            report["list_types_checked"] += 1
        legacy = next(row for row in orders if row.get("decision_status") == "UNASSOCIATED")
        detail = call("paper_order_get", {"order_id": legacy["id"]})
        assert detail["id"] == legacy["id"] and detail["account_id"] == account_id
        assert detail["decision_status"] == "UNASSOCIATED" and detail["decision_snapshot"] is None
        assert detail["decision_snapshot_id"] is None and detail["history_limit"] == 100
        for kind in ("fills", "events"):
            assert isinstance(detail[kind], list) and len(detail[kind]) <= 100
            assert bool(detail[kind + "_next_cursor"]) is detail[kind + "_has_more"]
        report["order_detail_verified"] = True
        for kind, time_key in (("fills", "filled_at"), ("events", "created_at")):
            pages("paper_order_history", {"order_id": legacy["id"], "kind": kind},
                  time_key, descending=False, order_id=legacy["id"])
            report["history_types_checked"] += 1
        unavailable = call("paper_order_chart", {"order_id": legacy["id"], "pattern_index": 0}, expected_error=True)
        assert unavailable["error"] == "PAPER_DECISION_UNASSOCIATED"
        report["legacy_chart_unavailable_verified"] = True
    return report


def verify(pool, url, username, account_id, *, client_factory=None):
    report = {"ok": False, "dedicated_account_verified": False, "read_only_scopes_verified": False,
              "temporary_client_revoked": False, "revoked_token_denied": False, "check_failed": False,
              "cleanup_failed": False, "trade_requests": 0, "price_queries": 0, "telegram_messages_sent": 0,
              "existing_clients_modified": False}
    repository = PostgresMcpRepository(pool)
    client_id, issued, attempted = uuid4(), None, False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp" or parsed.query
                or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("credential-free HTTPS MCP endpoint required")
        account_id = str(UUID(str(account_id)))
        tenant, owner = resolve_owner(pool, username)
        with pool.read_transaction(tenant, owner) as connection:
            selected = connection.execute("""SELECT name FROM paper_accounts
                WHERE id=%s AND tenant_id=%s AND owner_user_id=%s AND status='ACTIVE'""",
                (UUID(account_id), tenant, owner)).fetchone()
        if not selected or not selected[0].startswith("MCP模拟验收-"):
            raise ValueError("an owned dedicated smoke account is required")
        report["dedicated_account_verified"] = True
        # Allocate before INSERT and attempt exact-ID cleanup even if commit
        # acknowledgement fails. Never inspect/revoke an existing credential.
        attempted = True
        with pool.transaction(tenant, owner) as connection:
            connection.execute("""INSERT INTO mcp_clients(id,tenant_id,owner_user_id,name,status,created_at,updated_at)
                VALUES(%s,%s,%s,%s,'ACTIVE',now(),now())""",
                (client_id, tenant, owner, f"deployment-paper-read-{client_id.hex}"))
        issued = repository.create_token(tenant, owner, client_id, label="one-hour PAPER read release check",
                                         scopes=SCOPES, ttl=timedelta(hours=1))
        assert set(issued.token.scopes) == set(SCOPES)
        report["read_only_scopes_verified"] = True
        read_checks(url, issued.secret, account_id, client_factory=client_factory, report=report)
    except Exception:
        report["check_failed"] = True
    finally:
        if attempted:
            try:
                revoked = repository.revoke_client(tenant, owner, client_id)
                tokens = repository.list_tokens(tenant, owner, client_id)
                report["temporary_client_revoked"] = revoked.status == "REVOKED" and all(token.revoked_at is not None for token in tokens)
            except Exception:
                report["cleanup_failed"] = True
        if issued is not None and report["temporary_client_revoked"]:
            try:
                factory = client_factory or httpx.Client
                with factory(headers={"Authorization": f"Bearer {issued.secret}", "Accept": "application/json, text/event-stream"},
                             timeout=15, follow_redirects=False) as client:
                    response = client.post(url, json={"jsonrpc": "2.0", "id": 999, "method": "tools/list", "params": {}})
                    report["revoked_token_denied"] = response.status_code == 401
            except Exception:
                report["cleanup_failed"] = True
    report["ok"] = (not report["check_failed"] and report["temporary_client_revoked"] and report["revoked_token_denied"]
                    and report.get("legacy_chart_unavailable_verified", False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="owner")
    parser.add_argument("--account-id", type=UUID, required=True, help="Existing dedicated MCP模拟验收- account only")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    pool = None
    try:
        database = read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"]
        url = read_env(SECRET_DIR / "formal-mcp.env")["GATESCOPE_MCP_PUBLIC_URL"]
        pool = PostgresPool(database, min_size=1, max_size=1)
        pool.open()
        report = verify(pool, url, args.username, args.account_id)
    except Exception:
        report = {"ok": False, "check_failed": True}
    finally:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                report.update(ok=False, cleanup_failed=True)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
