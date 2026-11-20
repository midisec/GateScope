"""Opt-in, auditable PAPER acceptance: one dedicated account and one SOL contract.

Default --read-only uses an existing in-memory/env token and never creates an
account or credential. Only --execute-paper-smoke creates a dedicated account
and one-hour credential. Trading facts/accounts remain; only that credential is
revoked. No private exchange endpoint, real order, raw reply or balance is logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2 as httpx

from gatescope.adapters.gate_paper_market import GatePaperMarket, MarketSnapshot
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_market_export import result_payload, rpc
from verify_deployed_mcp import resolve_owner

READ_TOOLS = {"paper_account_list", "paper_account_snapshot", "paper_order_list", "paper_position_list",
              "paper_fill_list", "paper_order_get", "paper_order_preview", "paper_order_chart", "paper_order_history"}
WRITE_TOOLS = {"paper_order_submit", "paper_order_cancel", "paper_position_close"}


def safe_failure(exc: Exception) -> dict:
    return {"error_type": type(exc).__name__, "script_lines": [
        frame.lineno for frame in traceback.extract_tb(exc.__traceback__)
        if Path(frame.filename).name == "smoke_mcp_paper.py"
    ]}


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (not parsed.hostname or parsed.path != "/mcp" or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError("credential-free MCP endpoint required")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("public MCP requires HTTPS")


def _terms(snapshot: MarketSnapshot) -> tuple[str, str]:
    if snapshot.spec.symbol != "SOL_USDT" or snapshot.book.symbol != "SOL_USDT":
        raise ValueError("smoke is restricted to SOL_USDT")
    if snapshot.spec.min_contracts > 1 or snapshot.spec.max_contracts < 1:
        raise ValueError("one contract is not supported")
    tick, reference = snapshot.spec.price_tick, snapshot.book.best_ask
    stop = (reference * Decimal("0.98") / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    target = (reference * Decimal("1.04") / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if not 0 < stop < reference < target:
        raise ValueError("invalid tick-aligned smoke protection")
    return str(stop), str(target)


def run(
    url: str, bearer: str, account_id: str, *, execute_paper: bool = False,
    instrument_id: str | None = None, snapshot: MarketSnapshot | None = None,
    account_name: str | None = None, run_id: str | None = None, client_factory=None,
) -> dict:
    """Use fake HTTP in unit tests or an explicitly new dedicated PAPER account.

    No returned value contains balances or raw tool bodies. On uncertain submit,
    retry exactly the same request once. Never create a replacement opening key.
    """
    validate_url(url)
    account_id = str(UUID(account_id))
    if execute_paper and (snapshot is None or instrument_id is None or not account_name
                          or not account_name.startswith("MCP模拟验收-")):
        raise ValueError("writes require an explicitly dedicated smoke account and public quote")
    nonce = run_id or uuid4().hex
    if not nonce.isalnum() or not 8 <= len(nonce) <= 64:
        raise ValueError("invalid smoke run identifier")
    report = {"kind": "MCP_PAPER_SMOKE", "mode": "EXECUTE_PAPER" if execute_paper else "READ_ONLY",
              "cases": [], "orders_changed": False, "openai_called": False, "live_exchange_order_sent": False,
              "existing_accounts_modified": False, "facts_deleted": False, "closed": False}
    factory = client_factory if client_factory is not None else httpx.Client
    with factory(headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json, text/event-stream"},
                 timeout=20, follow_redirects=False) as client:
        sequence = 1
        init = rpc(client, url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "gatescope-paper-smoke", "version": "1"}}, sequence)
        client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
        sequence += 1
        tools = rpc(client, url, "tools/list", {}, sequence)["tools"]
        found = {tool["name"]: tool for tool in tools}
        assert READ_TOOLS | WRITE_TOOLS <= found.keys()
        for name in READ_TOOLS | WRITE_TOOLS:
            assert found[name]["annotations"]["readOnlyHint"] is (name in READ_TOOLS)
        report["manifest_verified"] = True

        def call(name: str, arguments: dict) -> dict:
            nonlocal sequence
            if name in WRITE_TOOLS and not execute_paper:
                raise ValueError("read-only smoke cannot mutate")
            sequence += 1
            started = time.monotonic()
            result = result_payload(rpc(client, url, "tools/call", {"name": name, "arguments": arguments}, sequence))
            assert result.get("environment") == "PAPER"
            report["cases"].append({"tool": name, "elapsed_seconds": round(time.monotonic()-started, 3)})
            return result

        accounts = call("paper_account_list", {})["items"]
        selected = [row for row in accounts if row["id"] == account_id]
        assert len(selected) == 1
        if execute_paper:
            assert selected[0]["name"] == account_name and selected[0]["status"] == "ACTIVE"
        initial = call("paper_account_snapshot", {"account_id": account_id})
        assert initial["account"]["id"] == account_id
        initial_cash = Decimal(initial["balance"]["cash_balance"])
        for name in ("paper_order_list", "paper_position_list", "paper_fill_list"):
            rows = call(name, {"account_id": account_id, "limit": 100})["items"]
            assert all(row["account_id"] == account_id for row in rows)
            if execute_paper:
                assert rows == []  # Never touch an account already holding facts.
        if not execute_paper:
            report["ok"] = True
            return report

        stop, target = _terms(snapshot)
        opening_args = {"account_id": account_id, "instrument_id": str(UUID(instrument_id)),
                        "side": "LONG", "contracts": 1, "stop_price": stop, "take_profit_price": target,
                        "idempotency_key": "smoke-open-" + nonce, "timeframe": "15m", "order_type": "MARKET",
                        "time_in_force": "IOC", "max_slippage_bps": "50", "ttl_seconds": 60}
        opening_id = position_id = None
        submit_attempted = False
        close_args = None
        primary_error = None

        def recover_opening() -> None:
            nonlocal opening_id, position_id
            orders = call("paper_order_list", {"account_id": account_id, "limit": 100})["items"]
            matches = [row for row in orders if row["account_id"] == account_id and row.get("effect") == "OPEN"
                       and row.get("idempotency_key") == opening_args["idempotency_key"]]
            assert len(matches) <= 1
            if matches:
                opening_id = matches[0]["id"]
                position_id = matches[0].get("position_id")

        def own_position() -> dict | None:
            values = call("paper_position_list", {"account_id": account_id, "limit": 100})["items"]
            matches = [row for row in values if row["id"] == position_id and row["account_id"] == account_id
                       and row["opening_order_id"] == opening_id]
            assert len(matches) <= 1
            return matches[0] if matches else None

        def close_known() -> None:
            nonlocal close_args
            if not position_id or not opening_id:
                return
            position = own_position()
            if position is None:
                return
            if position["status"] == "CLOSED":
                report["closed"] = True
                return
            assert int(position["remaining_contracts"]) == 1
            close_args = close_args or {"position_id": position_id, "idempotency_key": "smoke-close-" + nonce,
                                        "confirm_paper": True, "order_type": "MARKET", "time_in_force": "IOC",
                                        "max_slippage_bps": "100", "ttl_seconds": 60}
            # The exact same key is used again if an HTTP response is uncertain.
            try:
                closing = call("paper_position_close", close_args)
            except Exception:
                closing = call("paper_position_close", close_args)
            assert closing["order"]["account_id"] == account_id and closing["order"]["position_id"] == position_id
            position = own_position()
            report["closed"] = bool(position and position["status"] == "CLOSED" and position["remaining_contracts"] == 0)

        try:
            preview = call("paper_order_preview", opening_args)
            assert preview["preview_only"] is True and preview["order_created"] is False
            assert preview["estimate"]["filled_contracts"] == 1
            report["preview_executable"] = True
            submit_args = {**opening_args, "confirm_paper": True}
            submit_attempted = True
            report["orders_changed"] = True  # An uncertain request may have committed.
            try:
                submitted = call("paper_order_submit", submit_args)
            except Exception:
                submitted = call("paper_order_submit", submit_args)
                report["opening_response_recovered_by_same_key"] = True
            order = submitted["order"]
            assert order["account_id"] == account_id and order["effect"] == "OPEN"
            opening_id, position_id = order["id"], order.get("position_id")
            assert submitted["accepted"] is True and order["filled_contracts"] == 1
            replay = call("paper_order_submit", submit_args)
            assert replay["order"]["id"] == opening_id and replay["replayed"] is True
            report["idempotency_same_order"] = True
            close_known()
            assert report["closed"]
        except Exception as exc:
            primary_error = safe_failure(exc)
        finally:
            if submit_attempted and not report["closed"]:
                try:
                    if not opening_id:
                        recover_opening()
                    close_known()
                except Exception as exc:
                    report["position_cleanup"] = safe_failure(exc)
            report["opening_order_id"] = opening_id
            report["position_id"] = position_id
            report["partial_close_case"] = "NOT_APPLICABLE_SINGLE_INTEGER_CONTRACT"
        if primary_error:
            report.update(primary_error)
        orders = call("paper_order_list", {"account_id": account_id, "limit": 100})["items"]
        positions = call("paper_position_list", {"account_id": account_id, "limit": 100})["items"]
        fills = call("paper_fill_list", {"account_id": account_id, "limit": 100})["items"]
        final = call("paper_account_snapshot", {"account_id": account_id})
        assert all(row["account_id"] == account_id for row in orders + positions + fills)
        if report["closed"]:
            assert len(positions) == 1 and positions[0]["status"] == "CLOSED"
            assert sum(row["contracts"] for row in fills if row["effect"] == "OPEN") == 1
            assert sum(row["contracts"] for row in fills if row["effect"] == "CLOSE") == 1
            expected_cash = initial_cash + sum((Decimal(row["realized_pnl"])-Decimal(row["fee"]) for row in fills), Decimal(0))
            assert abs(Decimal(final["balance"]["cash_balance"])-expected_cash) <= Decimal("0.000000000001")
            assert Decimal(final["balance"]["position_margin"]) == 0 and Decimal(final["balance"]["order_reserved_margin"]) == 0
            report["balance_reconciled_without_disclosure"] = True
        report["counts"] = {"orders": len(orders), "positions": len(positions), "fills": len(fills),
                            "open_positions": sum(row["status"] == "OPEN" for row in positions)}
        report["ok"] = not primary_error and report["closed"] and report.get("idempotency_same_order", False)
        return report


def database_evidence(pool, tenant, owner, account_id: UUID) -> dict:
    """Bounded aggregate counts for exactly the newly created account only."""
    with pool.read_transaction(tenant, owner) as connection:
        connection.execute("SET LOCAL statement_timeout='3s'")
        counts = {}
        for table, label in (("paper_orders", "orders"), ("paper_positions", "positions"), ("paper_order_fills", "fills")):
            counts[label] = connection.execute(
                f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND owner_user_id=%s AND account_id=%s",
                (tenant, owner, account_id),
            ).fetchone()[0]
        rows = connection.execute("""SELECT coalesce(nd.status,'NOT_DISPATCHED'),count(*)
            FROM outbox_events oe LEFT JOIN notification_deliveries nd ON nd.event_id=oe.event_id AND nd.tenant_id=oe.tenant_id
            WHERE oe.tenant_id=%s AND oe.event_type='paper.order.fill.v1'
              AND oe.payload_json->>'owner_user_id'=%s AND oe.payload_json->>'account_id'=%s
            GROUP BY coalesce(nd.status,'NOT_DISPATCHED')""", (tenant, str(owner), str(account_id))).fetchall()
    return {"web_database_counts": counts, "telegram_delivery_status_counts": {status: count for status, count in rows},
            "telegram_observed_delivery": any(status == "SENT" and count > 0 for status, count in rows)}


def execute(pool, url: str, username: str) -> dict:
    validate_url(url)
    if urlsplit(url).scheme != "https":
        raise ValueError("deployment execution requires HTTPS")
    tenant, owner = resolve_owner(pool, username)
    client_id, nonce = uuid4(), uuid4().hex
    name = "MCP模拟验收-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + nonce[:8]
    created_client = False
    token = None
    account = None
    mcp = PostgresMcpRepository(pool)
    market = GatePaperMarket()
    report = {"kind": "DEDICATED_PAPER_ACCEPTANCE", "ok": False, "existing_accounts_modified": False,
              "facts_deleted": False, "temporary_client_revoked": False, "openai_called": False}
    try:
        # Obtain public conditions before creating even a simulated account.
        quote = market.get("SOL_USDT")
        with pool.public_transaction() as connection:
            found = connection.execute("""SELECT id FROM instruments WHERE symbol='SOL_USDT' AND venue='GATE'
                AND market='FUTURES_USDT' AND settle_asset='USDT' AND quote_asset='USDT' AND status='ACTIVE' LIMIT 2""").fetchall()
        assert len(found) == 1
        account = PostgresPaperRepository(pool).create_account(
            tenant, owner, name=name, base_currency="USDT", initial_equity=Decimal("10000"),
        )
        report["account_id"], report["account_name"] = str(account.id), name
        report["account_and_trade_facts_retained"] = True
        # Allocate ID before INSERT: cleanup never risks another user's client.
        with pool.transaction(tenant, owner) as connection:
            connection.execute("""INSERT INTO mcp_clients(id,tenant_id,owner_user_id,name,status,created_at,updated_at)
                VALUES(%s,%s,%s,%s,'ACTIVE',now(),now())""", (client_id, tenant, owner, "paper-smoke-" + nonce))
        created_client = True
        token = mcp.create_token(tenant, owner, client_id, label="one-hour PAPER acceptance only",
                                 scopes=("mcp:connect", "market:read", "analysis:read", "paper:read", "paper:trade"),
                                 ttl=timedelta(hours=1))
        report["mcp"] = run(url, token.secret, str(account.id), execute_paper=True, instrument_id=str(found[0][0]),
                             snapshot=quote, account_name=name, run_id=nonce)
        report.update(database_evidence(pool, tenant, owner, account.id))
        report["web_records_match_mcp"] = report["web_database_counts"] == {
            key: report["mcp"]["counts"][key] for key in ("orders", "positions", "fills")
        }
    except Exception as exc:
        report.update(safe_failure(exc))
    finally:
        try:
            market.close()
        except Exception as exc:
            report["market_cleanup"] = safe_failure(exc)
        if created_client:
            try:
                revoked = mcp.revoke_client(tenant, owner, client_id)
                report["temporary_client_revoked"] = revoked.status == "REVOKED" and all(
                    item.revoked_at is not None for item in mcp.list_tokens(tenant, owner, client_id))
            except Exception as exc:
                report["credential_cleanup"] = safe_failure(exc)
        if token is not None and report["temporary_client_revoked"]:
            try:
                with httpx.Client(timeout=10, follow_redirects=False) as client:
                    response = client.post(url, headers={"Authorization": f"Bearer {token.secret}",
                        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 999,
                        "method": "tools/list", "params": {}})
                    report["revoked_http_status"] = response.status_code
            except Exception as exc:
                report["revocation_check"] = safe_failure(exc)
    report["ok"] = (report.get("mcp", {}).get("ok", False) and report.get("web_records_match_mcp", False)
                    and report["temporary_client_revoked"] and report.get("revoked_http_status") == 401)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute-paper-smoke", action="store_true", help="Create a retained dedicated PAPER account; open/close exactly one SOL contract")
    mode.add_argument("--read-only", action="store_true", help="Use GATESCOPE_SMOKE_MCP_TOKEN and GATESCOPE_SMOKE_PAPER_ACCOUNT_ID; no account/credential mutation")
    parser.add_argument("--username", default="owner")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    pool = None
    try:
        public_url = read_env(SECRET_DIR / "formal-mcp.env")["GATESCOPE_MCP_PUBLIC_URL"]
        if args.execute_paper_smoke:
            database_url = read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"]
            pool = PostgresPool(database_url, min_size=1, max_size=1)
            pool.open()
            report = execute(pool, public_url, args.username)
        else:
            report = run(public_url, os.environ["GATESCOPE_SMOKE_MCP_TOKEN"], os.environ["GATESCOPE_SMOKE_PAPER_ACCOUNT_ID"])
    except Exception as exc:
        report = {"ok": False, **safe_failure(exc), "openai_called": False}
    finally:
        if pool is not None:
            pool.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
