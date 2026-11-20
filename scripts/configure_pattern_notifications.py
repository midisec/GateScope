#!/usr/bin/env python3
"""Configure only the recorded owner's shape notifications through authenticated API.

Default is inspect-only. --apply creates named, idempotent rules; existing user
rules are never overwritten. No exchange calls or synthetic trading signals.
Credentials stay in memory, and errors never print response bodies or URLs.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import logging
import os
import secrets
import time

import requests
from cryptography.hazmat.primitives import serialization

from prepare_service_envs import SECRET_DIR, read_env
from bootstrap_formal_owner_mfa import recover_active_enrollment
from verify_formal_runtime import verify_mfa
from gatescope.auth import CSRF_COOKIE
from gatescope.submission_crypto import encrypt_submission_for_test

PREFIX = "TG形态做单观察"
PERIODS = ("1h", "4h", "8h", "1d")


def planned_rules(watched, patterns, channel_id, periods=PERIODS):
    unique = {item["instrument_id"]: item for item in watched}
    return [{
        "name": f"{PREFIX} · {item['symbol']} · {period}",
        "condition": {"schema_version": 1, "instrument_id": iid, "timeframe": period,
                      "pattern_ids": sorted(patterns), "match_mode": "ANY",
                      "minimum_confidence": 0, "confirmed_only": True,
                      "coincidence_bars": 1, "cooldown_seconds": 0},
        "routing": {"schema_version": 1, "channel_ids": [channel_id]},
    } for iid, item in sorted(unique.items()) for period in periods]


@contextmanager
def owner_session():
    logging.disable(logging.CRITICAL)
    env = read_env(SECRET_DIR / "formal-runtime.env")
    credentials = json.loads((SECRET_DIR / "owner-login.json").read_text())
    origin = env["GATESCOPE_PUBLIC_ORIGIN"].rstrip("/")
    session = requests.Session()
    session.trust_env = False
    protected = {"Origin": origin}
    logged_in = False

    def request(method, path, body=None):
        response = session.request(method, origin + path, headers=protected,
                                   json=body, timeout=(5, 30))
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f"API_STATUS_{response.status_code}")
        return response.json()

    try:
        key = request("GET", "/api/v2/security/submission-key")
        encoded = key["public_key_spki"]
        public = serialization.load_der_public_key(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        envelope = encrypt_submission_for_test(public, key_version=key["key_version"],
            purpose="auth_login", request_id=secrets.token_urlsafe(24),
            issued_at=int(time.time() * 1000), payload={"tenant": "default",
                "username": credentials["username"], "password": credentials["password"]})
        request("POST", "/auth/login", {"encrypted_submission": envelope.model_dump()})
        logged_in = True
        protected["X-CSRF-Token"] = session.cookies.get(CSRF_COOKIE) or ""
        for key_name in ("GATESCOPE_MIGRATION_DATABASE_URL", "GATESCOPE_IDENTITY_KEK", "GATESCOPE_IDENTITY_KEY_VERSION"):
            os.environ[key_name] = env[key_name]
        enrolled = recover_active_enrollment(credentials["username"])
        if enrolled:
            verify_mfa(session, origin, protected, enrolled["secret"])
        yield request
    finally:
        if logged_in:
            request("POST", "/auth/logout", {})
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--test", action="store_true", help="Queue one clearly labelled channel connectivity test")
    parser.add_argument("--periods", nargs="+", choices=PERIODS, default=list(PERIODS))
    args = parser.parse_args()
    with owner_session() as request:
        watchlists = request("GET", "/api/v2/watchlists")["items"]
        watched = [item for watchlist in watchlists for item in watchlist["items"]]
        channels = [c for c in request("GET", "/api/v2/notification-channels")["items"] if c["status"] == "ACTIVE"]
        rules = request("GET", "/api/v2/alert-rules")["items"]
        routes = request("GET", "/api/v2/notification-routes")["items"]
        patterns = [p["pattern_id"] for p in request("GET", "/api/v2/patterns/catalog")["items"]]
        print(json.dumps({"watched": sorted({i["symbol"] for i in watched}),
            "active_channels": len(channels), "rules": [{"name": r["name"], "status": r["status"],
                "period": r["version"]["condition"]["timeframe"], "patterns": len(r["version"]["condition"]["pattern_ids"]),
                "telegram": bool(r["version"]["routing"]["channel_ids"])} for r in rules],
            "active_routes": sum(bool(r.get("enabled")) for r in routes),
            "catalog_count": len(patterns), "requested_periods": args.periods}, ensure_ascii=False))
        if not args.apply and not args.test:
            return
        if len(channels) != 1 or not watched or len(rules) >= 90:
            raise RuntimeError("CONFIGURATION_SCOPE_REQUIRES_REVIEW")
        planned = planned_rules(watched, patterns, channels[0]["id"], tuple(dict.fromkeys(args.periods)))
        existing = {r["name"]: r for r in rules}
        # Preflight every collision before performing any writes.
        for spec in planned:
            old = existing.get(spec["name"])
            if old and (old["status"] != "ACTIVE" or any(
                old["version"][key].get(field) != value
                for key in ("condition", "routing") for field, value in spec[key].items()
                if field != "schema_version"
            )):
                raise RuntimeError("EXISTING_NAMED_RULE_DIFFERS_NO_OVERWRITE")
        created = 0
        if args.apply:
            for spec in planned:
                if spec["name"] not in existing:
                    request("POST", "/api/v2/alert-rules", spec)
                    created += 1
            actual = {r["name"]: r for r in request("GET", "/api/v2/alert-rules")["items"]}
            if any(spec["name"] not in actual or actual[spec["name"]]["status"] != "ACTIVE" for spec in planned):
                raise RuntimeError("CONFIGURATION_READBACK_FAILED")
            print(json.dumps({"created": created, "active_managed_rules": len(planned),
                "confirmed_only": True, "minimum_geometry": 0, "cooldown_seconds": 0,
                "auto_order": False, "scope": "CURRENT_WATCHLIST_SNAPSHOT"}))
        if args.test:
            result = request("POST", f"/api/v2/notification-channels/{channels[0]['id']}/test", {})
            operation = result["operation_id"]
            for _ in range(20):
                status = request("GET", f"/api/v2/operations/{operation}")
                state = status.get("state", status.get("status"))
                if state in ("SUCCEEDED", "FAILED", "CANCELLED", "COMPLETED"):
                    print(json.dumps({"channel_test_state": state}))
                    if state not in ("SUCCEEDED", "COMPLETED"):
                        raise RuntimeError("CHANNEL_TEST_FAILED")
                    break
                time.sleep(1)
            else:
                print(json.dumps({"channel_test_state": "PENDING_NOT_YET_VERIFIED"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Notification configuration stopped: {type(exc).__name__}; private details suppressed")
        raise SystemExit(1) from None
