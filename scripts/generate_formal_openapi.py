#!/usr/bin/env python3
"""Generate the PostgreSQL/V2 API contract without opening a database connection."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def generate() -> dict:
    os.environ.setdefault("GATESCOPE_DATABASE_URL", "postgresql://contract:contract@127.0.0.1/gatescope_contract")
    os.environ.setdefault("GATESCOPE_IDENTITY_KEK", _encoded(b"k" * 32))
    os.environ.setdefault("GATESCOPE_IDENTITY_KEY_VERSION", "contract-v1")
    os.environ.setdefault("GATESCOPE_RECOVERY_PEPPER", _encoded(b"p" * 32))
    os.environ.setdefault("GATESCOPE_CURSOR_SIGNING_KEY", _encoded(b"c" * 32))
    os.environ.setdefault("GATESCOPE_PUBLIC_ORIGIN", "http://127.0.0.1:8777")
    from apps.api.formal_app import create_formal_app

    contract = create_formal_app().openapi()
    contract.setdefault("components", {}).setdefault("securitySchemes", {})["SessionCookie"] = {
        "type": "apiKey", "in": "cookie", "name": "gatescope_session",
    }
    public = {
        "/health/live", "/health/ready", "/auth/login", "/auth/workspaces",
        "/api/v2/security/submission-key",
        "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp", "/oauth/authorize", "/oauth/register",
        "/oauth/token", "/oauth/revoke",
    }
    rbac = {
        ("get", "/api/v2/admin/overview"): ["role:owner", "tenant:current", "session:fully-authenticated"],
        ("get", "/api/v2/admin/users"): ["role:owner", "tenant:current", "session:fully-authenticated"],
        ("get", "/api/v2/admin/resources"): ["role:owner", "tenant:current", "shared-storage:metadata-only"],
        ("get", "/api/v2/admin/audit"): ["role:owner", "tenant:current", "session:fully-authenticated"],
        ("post", "/api/v2/admin/users/{user_id}/sessions:revoke"): ["role:owner", "tenant:current", "recent-auth:required", "non-self"],
        ("get", "/api/v2/paper/accounts/{account_id}/snapshot"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/accounts/{account_id}/orders"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/accounts/{account_id}/positions"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/accounts/{account_id}/fills"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/orders/{order_id}"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/orders/{order_id}/fills"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/orders/{order_id}/events"): ["session:own", "mfa:verified"],
        ("get", "/api/v2/paper/orders/{order_id}/chart"): ["session:own", "mfa:verified"],
        ("post", "/api/v2/paper/orders:preview"): ["session:own", "mfa:verified"],
        ("post", "/api/v2/paper/orders"): ["proposal:approve", "session:own", "mfa:verified", "recent-auth:required"],
        ("post", "/api/v2/paper/orders/{order_id}:cancel"): ["risk:reduce", "session:own", "mfa:verified"],
        ("post", "/api/v2/paper/positions/{position_id}:close"): ["risk:reduce", "session:own", "mfa:verified"],
        ("get", "/api/v2/mcp/oauth/consent"): ["session:own", "mfa:verified", "role:owner", "oauth:browser-bound"],
        ("post", "/api/v2/mcp/oauth/consent"): ["session:own", "mfa:verified", "role:owner", "oauth:browser-bound", "recent-auth:approve-only"],
        ("post", "/auth/reauthenticate"): ["session:own", "mfa:verified", "password:current"],
        ("get", "/api/v2/research/strategies"): ["analysis:read"],
        ("get", "/api/v2/research/trend"): ["analysis:read"],
        ("get", "/api/v2/research/backtests"): ["analysis:read:own"],
        ("post", "/api/v2/research/backtests"): ["analysis:write:own"],
        ("get", "/api/v2/research/backtests/{run_id}"): ["analysis:read:own"],
        ("get", "/api/v2/research/backtests/{run_id}/cases"): ["analysis:read:own"],
        ("get", "/api/v2/research/backtests/{run_id}/events"): ["analysis:read:own"],
        ("get", "/api/v2/research/backtests/{run_id}/inputs"): ["analysis:read:own"],
        ("get", "/api/v2/research/backtests/{run_id}/indicator/chart"): ["analysis:read:own"],
        ("get", "/api/v2/research/backtests/{run_id}/indicator/{kind}"): ["analysis:read:own"],
        ("get", "/api/v2/research/portfolios"): ["analysis:read:own"],
        ("post", "/api/v2/research/portfolios"): ["analysis:write:own"],
        ("get", "/api/v2/research/portfolios/{run_id}"): ["analysis:read:own"],
        ("get", "/api/v2/research/portfolios/{run_id}/{kind}"): ["analysis:read:own"],
        ("get", "/api/v2/research/portfolios/{run_id}/details/{kind}"): ["analysis:read:own"],
        ("get", "/api/v2/research/shadow-trades"): ["analysis:read:own"],
        ("get", "/api/v2/research/shadow-trades/summary"): ["analysis:read:own"],
        ("get", "/api/v2/research/shadow-trades/{identifier}"): ["analysis:read:own"],
        ("get", "/api/v2/users"): ["user:manage:read"],
        ("post", "/api/v2/users"): ["user:manage", "role:owner", "recent-auth:required", "encrypted-submission:user_create"],
        ("get", "/api/v2/memberships"): ["user:manage:read"],
        ("patch", "/api/v2/memberships/{membership_id}"): ["user:manage"],
        ("get", "/api/v2/instruments"): ["market:read"],
        ("get", "/api/v2/bars"): ["market:read"],
        ("get", "/api/v2/market/volume-profile"): ["market:read"],
        ("get", "/api/v2/market/watchlist-cache"): ["market:read:own"],
        ("get", "/api/v2/research/coverage"): ["analysis:read"],
        ("get", "/api/v2/indicators/catalog"): ["analysis:read"],
        ("post", "/api/v2/indicators/preview"): ["analysis:read"],
        ("get", "/api/v2/patterns/catalog"): ["analysis:read"],
        ("get", "/api/v2/chart-layouts"): ["analysis:read"],
        ("post", "/api/v2/chart-layouts"): ["analysis:write:own"],
        ("get", "/api/v2/chart-layouts/{resource_id}"): ["analysis:read"],
        ("patch", "/api/v2/chart-layouts/{resource_id}"): ["analysis:write:own"],
        ("delete", "/api/v2/chart-layouts/{resource_id}"): ["analysis:write:own"],
        ("get", "/api/v2/indicator-templates"): ["analysis:read"],
        ("post", "/api/v2/indicator-templates"): ["analysis:write:own"],
        ("get", "/api/v2/indicator-templates/{resource_id}"): ["analysis:read"],
        ("patch", "/api/v2/indicator-templates/{resource_id}"): ["analysis:write:own"],
        ("delete", "/api/v2/indicator-templates/{resource_id}"): ["analysis:write:own"],
        ("get", "/api/v2/watchlists"): ["market:read"],
        ("post", "/api/v2/watchlists"): ["watchlist:write:own"],
        ("get", "/api/v2/watchlists/{watchlist_id}"): ["market:read"],
        ("patch", "/api/v2/watchlists/{watchlist_id}"): ["watchlist:write:own"],
        ("post", "/api/v2/watchlists/{watchlist_id}/items"): ["watchlist:write:own"],
        ("delete", "/api/v2/watchlists/{watchlist_id}/items/{instrument_id}"): ["watchlist:write:own"],
        ("get", "/api/v2/alert-rules"): ["analysis:read"],
        ("get", "/api/v2/strategy-events"): ["analysis:read"],
        ("post", "/api/v2/alert-rules"): ["alert:write"],
        ("get", "/api/v2/alert-rules/{rule_id}"): ["analysis:read"],
        ("patch", "/api/v2/alert-rules/{rule_id}"): ["alert:write"],
        ("post", "/api/v2/alert-rules/{rule_id}/versions"): ["alert:write"],
        ("post", "/api/v2/alert-rules/{rule_id}/pause"): ["alert:write"],
        ("get", "/api/v2/data-sources"): ["credential:read:own", "role:owner"],
        ("get", "/api/v2/data-sources/coinglass/config"): ["credential:read:own", "role:owner"],
        ("put", "/api/v2/data-sources/coinglass/config"): ["credential:write", "recent-auth", "mfa:fresh", "role:owner"],
        ("post", "/api/v2/data-sources/coinglass:test"): ["credential:read:own", "role:owner"],
        ("get", "/api/v2/data-sources/coinglass/tests/{job_id}"): ["credential:read:own", "role:owner"],
        ("post", "/api/v2/data-sources/coinglass/config:disable"): ["credential:write", "recent-auth", "mfa:fresh", "role:owner"],
        ("get", "/api/v2/telegram/bot-config"): ["credential:read:own", "role:owner"],
        ("put", "/api/v2/telegram/bot-config"): ["credential:write", "recent-auth", "mfa:fresh", "role:owner"],
        ("post", "/api/v2/telegram/bot-config:disable"): ["credential:write", "recent-auth", "mfa:fresh", "role:owner"],
        ("get", "/api/v2/telegram/connections"): ["notification:read:own"],
        ("post", "/api/v2/telegram/link-tokens"): ["notification:write:own"],
        ("post", "/api/v2/telegram/connections/{connection_id}:confirm"): ["notification:write:own", "mfa:fresh"],
        ("get", "/api/v2/notification-channels"): ["notification:read:own"],
        ("post", "/api/v2/notification-channels"): ["notification:write:own"],
        ("get", "/api/v2/notification-channels/{channel_id}"): ["notification:read:own"],
        ("post", "/api/v2/notification-channels/{channel_id}/test"): ["notification:write:own"],
        ("delete", "/api/v2/notification-channels/{channel_id}"): ["notification:write:own"],
        ("get", "/api/v2/notification-routes"): ["notification:read:own"],
        ("get", "/api/v2/notifications"): ["notification:read:own"],
        ("post", "/api/v2/notification-routes"): ["notification:write:own"],
        ("get", "/api/v2/notification-routes/{route_id}"): ["notification:read:own"],
        ("patch", "/api/v2/notification-routes/{route_id}"): ["notification:write:own"],
        ("delete", "/api/v2/notification-routes/{route_id}"): ["notification:write:own"],
        ("get", "/api/v2/exchange-accounts"): ["credential:read:own", "role:owner"],
        ("post", "/api/v2/exchange-accounts"): ["credential:write", "recent-auth", "role:owner"],
        ("get", "/api/v2/exchange-accounts/{account_id}"): ["credential:read:own", "role:owner"],
        ("post", "/api/v2/exchange-accounts/{account_id}/credentials:rotate"): ["credential:write", "recent-auth", "role:owner"],
        ("post", "/api/v2/exchange-accounts/{account_id}/validate"): ["credential:write", "recent-auth", "role:owner"],
        ("delete", "/api/v2/exchange-accounts/{account_id}"): ["credential:write", "recent-auth", "role:owner"],
        ("post", "/api/v2/exchange-accounts/{account_id}/testnet:arm"): ["credential:write", "recent-auth", "mfa:fresh", "role:owner", "environment:testnet"],
        ("post", "/api/v2/exchange-accounts/{account_id}/testnet:disarm"): ["credential:write", "recent-auth", "role:owner", "environment:testnet"],
        ("get", "/api/v2/testnet-drills"): ["execution:read:own", "role:owner", "environment:testnet"],
        ("get", "/api/v2/execution-certifications"): ["execution:read:own", "role:owner", "evidence:live-eligible"],
        ("post", "/api/v2/testnet-drills"): ["execution:testnet", "recent-auth", "mfa:fresh", "role:owner", "frozen-terms-only"],
        ("get", "/api/v2/testnet-drills/{public_drill_id}"): ["execution:read:own", "role:owner", "environment:testnet"],
        ("get", "/api/v2/testnet-drills/{public_drill_id}/timeline"): ["execution:read:own", "role:owner", "environment:testnet"],
        ("get", "/api/v2/mcp/clients"): ["credential:read:own", "role:owner"],
        ("post", "/api/v2/mcp/clients"): ["credential:write", "recent-auth"],
        ("post", "/api/v2/mcp/clients/{client_id}:revoke"): ["credential:write", "recent-auth", "mfa:fresh"],
        ("post", "/api/v2/mcp/clients/{client_id}/tokens"): ["credential:write", "recent-auth", "mfa:fresh"],
        ("post", "/api/v2/mcp/clients/{client_id}/tokens/{token_id}:revoke"): ["credential:write", "recent-auth", "mfa:fresh"],
        ("get", "/api/v2/paper-accounts"): ["analysis:read"],
        ("post", "/api/v2/paper-accounts"): ["proposal:create"],
        ("get", "/api/v2/signals"): ["analysis:read"],
        ("get", "/api/v2/signals/{signal_id}"): ["analysis:read"],
        ("get", "/api/v2/signals/{signal_id}/gates"): ["analysis:read"],
        ("get", "/api/v2/signals/{signal_id}/chart"): ["analysis:read"],
        ("post", "/api/v2/signals/{signal_id}/execution-decisions"): ["proposal:create"],
        ("get", "/api/v2/execution-decisions/{decision_id}"): ["analysis:read"],
        ("get", "/api/v2/proposals"): ["analysis:read"],
        ("post", "/api/v2/proposals"): ["proposal:create"],
        ("get", "/api/v2/proposals/{proposal_id}"): ["analysis:read"],
        ("post", "/api/v2/proposals/{proposal_id}/cancel"): ["proposal:create"],
        ("post", "/api/v2/proposals/{proposal_id}/approve"): ["proposal:approve"],
        ("get", "/api/v2/trades"): ["analysis:read"],
        ("get", "/api/v2/trades/{public_trade_id}"): ["analysis:read"],
        ("get", "/api/v2/trades/{public_trade_id}/timeline"): ["analysis:read"],
        ("get", "/api/v2/trades/{public_trade_id}/chart"): ["analysis:read"],
        ("post", "/api/v2/trades/{public_trade_id}:reconcile-paper"): ["proposal:approve"],
        ("get", "/api/v2/operations/{operation_id}"): ["operation:read:own"],
    }
    for path, methods in contract.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path not in public:
                operation["security"] = [{"SessionCookie": []}]
            if method.lower() in {"post", "put", "patch", "delete"} and path not in {
                "/auth/login", "/oauth/register", "/oauth/token", "/oauth/revoke",
            }:
                operation["x-csrf-header"] = "X-CSRF-Token"
            if path in {"/oauth/token", "/oauth/revoke"}:
                operation["x-oauth-client-auth"] = ["none", "client_secret_post", "client_secret_basic"]
            actions = rbac.get((method.lower(), path))
            if actions:
                operation["x-rbac-actions"] = actions
    return contract


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="contracts/openapi/v2.json")
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(generate(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
