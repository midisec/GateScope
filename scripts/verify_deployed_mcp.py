"""Production MCP check with a new one-hour token, then exact-client revocation.

Creates only this run's dedicated client/token. Never reuses or revokes an
existing client. No raw credential, HTTP response or account value is printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import traceback
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx2 as httpx

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import PostgresMcpRepository
from prepare_service_envs import SECRET_DIR, read_env
from smoke_mcp_market_export import run as smoke_run


def safe_failure(exc: Exception) -> dict:
    return {"error_type": type(exc).__name__, "script_lines": [
        {"file": Path(frame.filename).name, "line": frame.lineno}
        for frame in traceback.extract_tb(exc.__traceback__)
        if Path(frame.filename).name in {"verify_deployed_mcp.py", "smoke_mcp_market_export.py"}
    ]}


def resolve_owner(pool: PostgresPool, username: str) -> tuple[UUID, UUID]:
    """Select only identifiers; do not load passwords, MFA or existing tokens."""
    with pool.public_transaction() as connection:
        tenant = connection.execute("SELECT id FROM tenants WHERE slug='default' AND status='ACTIVE'").fetchone()
        user = connection.execute("SELECT id FROM users WHERE normalized_username=%s AND status='ACTIVE'", (username.lower(),)).fetchone()
    if not tenant or not user:
        raise ValueError("active owner identity not found")
    with pool.transaction(tenant[0], user[0]) as connection:
        membership = connection.execute(
            "SELECT 1 FROM memberships WHERE tenant_id=%s AND user_id=%s AND role='OWNER' AND status='ACTIVE'",
            (tenant[0], user[0]),
        ).fetchone()
    if not membership:
        raise ValueError("default workspace owner membership is required")
    return tenant[0], user[0]


def verify(
    pool: PostgresPool, url: str, username: str, *, coinglass_recovery: bool = False,
    coinglass_queue: bool = False,
) -> dict:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp" or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("deployed MCP URL must be a credential-free HTTPS /mcp endpoint")
    tenant, user = resolve_owner(pool, username)
    repository = PostgresMcpRepository(pool)
    # Allocate locally before INSERT so even a later token/read failure cannot
    # leave us unable to identify the exact new object requiring cleanup.
    client_id = uuid4()
    client_name = f"deployment-market-smoke-{client_id.hex}"
    created = False
    issued = None
    report = {"kind": "DEDICATED_TEMPORARY_MCP_DEPLOYMENT_CHECK", "cases": [],
              "temporary_client_revoked": False, "revoked_http_status": None,
              "existing_clients_modified": False, "openai_called": False}
    try:
        with pool.transaction(tenant, user) as connection:
            connection.execute(
                """INSERT INTO mcp_clients(id,tenant_id,owner_user_id,name,status,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,'ACTIVE',now(),now())""",
                (client_id, tenant, user, client_name),
            )
        created = True
        issued = repository.create_token(
            tenant, user, client_id, label="one-hour deployment validation",
            scopes=("mcp:connect", "market:read", "analysis:read"), ttl=timedelta(hours=1),
        )
        cases = (("BTC_USDT", "1h"), ("ETH_USDT", "1d")) if coinglass_recovery else (
            ("BTC_USDT", "1m"), ("龙虾_USDT", "15m"),
        )
        if coinglass_queue:
            cases = (("SOL_USDT", "30m"), ("SOL_USDT", "8h"))
        for symbol, timeframe in cases:
            try:
                result = smoke_run(url, issued.secret, symbol, "futures:usdt", timeframe, 100)
            except Exception as exc:
                result = {"ok": False, **safe_failure(exc)}
            report["cases"].append({"symbol": symbol, "timeframe": timeframe, **result})
    except Exception as exc:
        report.update(safe_failure(exc))
    finally:
        if created:
            try:
                revoked = repository.revoke_client(tenant, user, client_id)
                remaining = repository.list_tokens(tenant, user, client_id)
                report["temporary_client_revoked"] = revoked.status == "REVOKED" and all(token.revoked_at is not None for token in remaining)
            except Exception as exc:
                report["cleanup_error_type"] = type(exc).__name__
        if issued is not None and report["temporary_client_revoked"]:
            try:
                with httpx.Client(timeout=15, follow_redirects=False) as client:
                    response = client.post(
                        url, headers={"Authorization": f"Bearer {issued.secret}", "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
                    )
                    report["revoked_http_status"] = response.status_code
            except Exception as exc:
                report["revocation_check_error_type"] = type(exc).__name__
    report["ok"] = (len(report["cases"]) == 2 and all(case["ok"] for case in report["cases"])
                    and report["temporary_client_revoked"] and report["revoked_http_status"] == 401)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="owner")
    cases = parser.add_mutually_exclusive_group()
    cases.add_argument("--coinglass-recovery", action="store_true",
                        help="Check and asynchronously warm BTC 1h / ETH 1d caches")
    cases.add_argument("--coinglass-queue", action="store_true",
                       help="Check progressive cache collection for SOL 30m / SOL 8h")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    pool = None
    try:
        database_url = read_env(SECRET_DIR / "formal-api.env")["GATESCOPE_DATABASE_URL"]
        public_url = read_env(SECRET_DIR / "formal-mcp.env")["GATESCOPE_MCP_PUBLIC_URL"]
        pool = PostgresPool(database_url, min_size=1, max_size=1)
        pool.open()
        report = verify(pool, public_url, args.username, coinglass_recovery=args.coinglass_recovery,
                        coinglass_queue=args.coinglass_queue)
    except Exception as exc:
        report = {"ok": False, **safe_failure(exc), "openai_called": False}
    finally:
        if pool is not None:
            pool.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
