#!/usr/bin/env python3
"""Authenticated post-deploy smoke test without printing credentials or MFA material."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from gatescope.auth import CSRF_COOKIE
from gatescope.security_secrets import totp_code


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    credentials = json.loads(
        (root / "data" / "runtime-secrets" / "owner-login.json").read_text(encoding="utf-8")
    )
    mfa = json.loads(
        (root / "data" / "runtime-secrets" / "formal-owner-mfa.json").read_text(encoding="utf-8")
    )
    public_origin = os.environ.get("GATESCOPE_PUBLIC_ORIGIN", "http://43.155.154.25:17758").rstrip("/")
    local_origin = os.environ.get("GATESCOPE_VERIFY_LOCAL_ORIGIN", "http://127.0.0.1:17758").rstrip("/")
    session = requests.Session()
    session.trust_env = False
    headers = {"Host": urlparse(public_origin).netloc, "Origin": public_origin}
    login = session.post(
        f"{local_origin}/auth/login",
        headers=headers,
        json={"tenant": mfa["tenant"], "username": credentials["username"], "password": credentials["password"]},
        timeout=(3, 10),
    )
    login.raise_for_status()
    csrf = session.cookies.get(CSRF_COOKIE)
    if not csrf:
        raise RuntimeError("CSRF cookie missing")
    protected = {**headers, "X-CSRF-Token": csrf}
    verify_mfa(session, local_origin, protected, mfa["totp_secret"])

    me = checked_get(session, f"{local_origin}/auth/me", headers)
    if me.get("auth_strength") != "MFA" or me["user"].get("role") != "OWNER":
        raise RuntimeError("OWNER session did not reach MFA strength")
    sources = checked_get(session, f"{local_origin}/api/v2/data-sources", headers)
    watchlists = checked_get(session, f"{local_origin}/api/v2/watchlists", headers)
    watched = [item for watchlist in watchlists["items"] for item in watchlist["items"]]
    btc = next((item for item in watched if item["symbol"] == "BTC_USDT"), None)
    if btc is None:
        raise RuntimeError("BTC_USDT is missing from the formal watchlist")
    bars = checked_get(
        session,
        f"{local_origin}/api/v2/bars",
        headers,
        params={"instrument_id": btc["instrument_id"], "timeframe": "15m", "limit": 100, "latest": "true"},
    )
    if len(bars["items"]) != 100 or bars.get("has_more"):
        raise RuntimeError("latest chart window contract failed")
    opened = [datetime.fromisoformat(item["open_time"]) for item in bars["items"]]
    if opened != sorted(opened) or datetime.now(timezone.utc) - opened[-1] > timedelta(hours=1):
        raise RuntimeError("latest chart data is not current and ascending")
    exchange = checked_get(session, f"{local_origin}/api/v2/exchange-accounts", headers)
    telegram = checked_get(session, f"{local_origin}/api/v2/telegram/connections", headers)
    if exchange.get("live_trading_enabled") is not False:
        raise RuntimeError("LIVE hard gate is not active")
    worker = sources["market_worker"]
    if not worker.get("healthy"):
        raise RuntimeError("formal market worker is unhealthy")
    print(
        "Formal runtime verification passed: "
        f"auth=MFA sources={len(sources['items'])} watchlists={len(watchlists['items'])} "
        f"BTC_USDT_latest_bars={len(bars['items'])} worker={worker['state']} "
        f"telegram_configured={str(bool(telegram.get('configured'))).lower()} "
        f"credential_https_gate={str(not bool(exchange.get('credential_admission_configured'))).lower()}"
    )
    return 0


def verify_mfa(
    session: requests.Session,
    origin: str,
    headers: dict[str, str],
    secret: str,
) -> None:
    for attempt in range(2):
        code, _ = totp_code(secret)
        response = session.post(
            f"{origin}/auth/mfa/verify", headers=headers, json={"code": code}, timeout=(3, 10)
        )
        if response.status_code == 200:
            return
        if attempt == 0 and response.status_code == 401:
            time.sleep(31 - int(time.time()) % 30)
            continue
        response.raise_for_status()
    raise RuntimeError("MFA verification failed")


def checked_get(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    *,
    params: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    response = session.get(url, headers=headers, params=params, timeout=(3, 10))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("API returned a non-object response")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
