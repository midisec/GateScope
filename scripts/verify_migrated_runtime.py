#!/usr/bin/env python3
"""Private authenticated, read-only business checks after server migration.

Uses the previously recorded OWNER credentials, never resets MFA or passwords.
Existing MFA is decrypted in memory only for this authorized deployment check.
The test creates and then logs out its own session; it sends no trade commands.
"""
import json
import os
import base64
import secrets
import time
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import serialization

from prepare_service_envs import SECRET_DIR, read_env
from bootstrap_formal_owner_mfa import recover_active_enrollment
from verify_formal_runtime import verify_mfa
from gatescope.auth import CSRF_COOKIE
from gatescope.submission_crypto import encrypt_submission_for_test


def main():
    env = read_env(SECRET_DIR / "formal-runtime.env")
    origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
    credentials = json.loads((SECRET_DIR / "owner-login.json").read_text())
    session = requests.Session()
    session.trust_env = False
    headers = {"Origin": origin}
    key_response = session.get(origin + "/api/v2/security/submission-key", timeout=(5, 15))
    key_response.raise_for_status()
    key_info = key_response.json()
    encoded = key_info["public_key_spki"]
    public_key = serialization.load_der_public_key(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    envelope = encrypt_submission_for_test(public_key, key_version=key_info["key_version"], purpose="auth_login", request_id=secrets.token_urlsafe(24), issued_at=int(time.time() * 1000), payload={"tenant": "default", "username": credentials["username"], "password": credentials["password"]})
    response = session.post(origin + "/auth/login", headers=headers, json={"encrypted_submission": envelope.model_dump()}, timeout=(5, 20))
    print("Recorded account login HTTP status:", response.status_code)
    if response.status_code != 200:
        raise RuntimeError(f"Existing credentials login returned HTTP {response.status_code}; no reset attempted")
    csrf = session.cookies.get(CSRF_COOKIE)
    if not csrf:
        raise RuntimeError("Login CSRF cookie is missing")
    protected = {**headers, "X-CSRF-Token": csrf}
    try:
        for key in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key] = env[key]
        enrolled = recover_active_enrollment(credentials["username"])
        if enrolled:
            verify_mfa(session, origin, protected, enrolled["secret"])

        def get(path, params=None):
            result = session.get(origin + path, headers=headers, params=params, timeout=(5, 30))
            if result.status_code != 200:
                raise RuntimeError(f"Read check failed: {path} HTTP {result.status_code}")
            return result.json()

        me = get("/auth/me")
        print("Existing account login verified; auth strength:", me.get("auth_strength"))
        watchlists = get("/api/v2/watchlists")
        watched = [item for watchlist in watchlists["items"] for item in watchlist["items"]]
        print(f"Watchlists={len(watchlists['items'])}; watched instruments={len(watched)}")
        for item in watched:
            for timeframe in ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"):
                payload = get("/api/v2/bars", {"instrument_id": item["instrument_id"], "timeframe": timeframe, "limit": 100, "latest": "true"})
                times = [datetime.fromisoformat(bar["open_time"]) for bar in payload["items"]]
                if len(times) != 100 or times != sorted(set(times)):
                    raise RuntimeError("Chart bars must be populated, unique and ordered")
                age = round((datetime.now(timezone.utc) - times[-1]).total_seconds())
                period = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"8h":28800,"1d":86400}[timeframe]
                if age > period * 2 + 300:
                    raise RuntimeError("Current chart data is stale")
                print(f"Chart {item['symbol']} {timeframe}: bars=100, latest_bar_age_seconds={age}")
        for path in ("/api/v2/patterns/watchlist-progress", "/api/v2/research/strategies", "/api/v2/research/backtests"):
            payload = get(path)
            print("Module read check passed:", path, "items=", len(payload.get("items", [])))
        policy = get("/api/v2/research/strategies").get("performance_policy", {})
        if policy.get("version") != "performance-observation-2" or policy.get("thresholds_enforced") is not False:
            raise RuntimeError("Withdrawn performance policy is not served by the deployed API")
        observed = get("/api/v2/research/backtests", {"screening": "OBSERVE_ONLY"})
        if any(item.get("screening", {}).get("status") != "OBSERVE_ONLY" for item in observed["items"]):
            raise RuntimeError("Observation-only history filtering is inconsistent")
        catalog = get("/api/v2/patterns/catalog")
        if len(catalog.get("items", [])) != 28:
            raise RuntimeError("Current structure rule catalog was not published")
        print("Event release verified: four performance floors withdrawn, observation filter available, 28 structure rules")
        sources = get("/api/v2/data-sources")
        print("Market worker healthy:", sources["market_worker"].get("healthy"))
        if not sources["market_worker"].get("healthy"):
            raise RuntimeError("Market worker not healthy")
        exchange = get("/api/v2/exchange-accounts")
        if exchange.get("live_trading_enabled") is not False:
            raise RuntimeError("LIVE hard gate is not closed")
        print("Exchange configuration readable; LIVE trading remains disabled")
        telegram = get("/api/v2/telegram/connections")
        print("Telegram configuration readable; configured:", bool(telegram.get("configured")))
        for path in ("/markets/chart", "/strategies/alerts", "/strategies/backtests", "/trading/paper", "/trading/exchange-accounts", "/integrations/mcp", "/integrations/data-sources", "/notifications/inbox", "/settings/account", "/settings/notifications"):
            page = session.get(origin + path, headers=headers, allow_redirects=False, timeout=(5, 15))
            if page.status_code != 200 or "text/html" not in page.headers.get("Content-Type", "") or 'id="root"' not in page.text:
                raise RuntimeError("Authenticated Web route did not return its built application")
        print("All 10 authenticated Web module routes return the built application")
        print("Authenticated deployment verification passed; no business settings changed")
    finally:
        logout = session.post(origin + "/auth/logout", headers=protected, timeout=(5, 15))
        print("Verification session logout status:", logout.status_code)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not expose requests/SQL exception strings: they can contain tokens.
        print(f"Deployment verification failed: {type(exc).__name__}; private details suppressed")
        raise SystemExit(1) from None
