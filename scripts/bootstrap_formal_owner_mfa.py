#!/usr/bin/env python3
"""Enroll the initial formal OWNER and persist one-time recovery material safely."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import psycopg
import requests

from gatescope.auth import CSRF_COOKIE
from gatescope.security_secrets import AesGcmSecretProtector, ProtectedSecret, totp_code


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    credentials_path = root / "data" / "runtime-secrets" / "owner-login.json"
    output_path = root / "data" / "runtime-secrets" / "formal-owner-mfa.json"
    public_origin = os.environ.get("GATESCOPE_PUBLIC_ORIGIN", "").rstrip("/")
    local_origin = os.environ.get("GATESCOPE_BOOTSTRAP_LOCAL_ORIGIN", "http://127.0.0.1:17759").rstrip("/")
    if not public_origin or urlparse(public_origin).scheme not in {"http", "https"}:
        raise SystemExit("GATESCOPE_PUBLIC_ORIGIN is required")
    if output_path.exists():
        raise SystemExit("Formal OWNER MFA recovery material already exists; refusing to overwrite it")

    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    session = requests.Session()
    session.trust_env = False
    headers = {"Host": urlparse(public_origin).netloc, "Origin": public_origin}
    login = session.post(
        f"{local_origin}/auth/login",
        headers=headers,
        json={
            "tenant": "default",
            "username": credentials["username"],
            "password": credentials["password"],
        },
        timeout=(3, 10),
    )
    login.raise_for_status()
    if not login.json().get("mfa_required"):
        raise SystemExit("Formal OWNER already has MFA; no new secret was created")
    csrf = session.cookies.get(CSRF_COOKIE)
    if not csrf:
        raise RuntimeError("login response did not set a CSRF cookie")
    protected_headers = {**headers, "X-CSRF-Token": csrf}
    enrolled = recover_active_enrollment(credentials["username"])
    if enrolled is None:
        enrollment = session.post(
            f"{local_origin}/auth/mfa-methods:enroll",
            headers=protected_headers,
            json={"label": "Primary"},
            timeout=(3, 10),
        )
        enrollment.raise_for_status()
        enrolled = enrollment.json()
        code, _ = totp_code(enrolled["secret"])
        confirmed = session.post(
            f"{local_origin}/auth/mfa-methods/{enrolled['method_id']}:confirm",
            headers=protected_headers,
            json={"code": code},
            timeout=(3, 10),
        )
        confirmed.raise_for_status()
    else:
        # The preceding enrollment may have completed before a later acceptance
        # check failed. A one-step-ahead code is within the RFC 6238 drift window
        # and cannot replay the already consumed confirmation step.
        code, _ = totp_code(enrolled["secret"], at=int(time.time()) + 30)
        verified = session.post(
            f"{local_origin}/auth/mfa/verify",
            headers=protected_headers,
            json={"code": code},
            timeout=(3, 10),
        )
        verified.raise_for_status()
    recovery = session.post(
        f"{local_origin}/auth/recovery-codes:regenerate",
        headers=protected_headers,
        timeout=(3, 10),
    )
    recovery.raise_for_status()
    payload = {
        "tenant": "default",
        "username": credentials["username"],
        "method_id": enrolled["method_id"],
        "totp_secret": enrolled["secret"],
        "provisioning_uri": enrolled["provisioning_uri"],
        "recovery_codes": recovery.json()["codes"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warning": "敏感的一次性 MFA 资料：导入验证器并离线保存恢复码后删除此文件。",
    }
    write_private_json(output_path, payload)
    me = session.get(f"{local_origin}/auth/me", headers=headers, timeout=(3, 10))
    me.raise_for_status()
    if me.json().get("auth_strength") != "MFA":
        raise RuntimeError("formal session did not advance to MFA")
    sources = session.get(f"{local_origin}/api/v2/data-sources", headers=headers, timeout=(3, 10))
    sources.raise_for_status()
    instrument_params: dict[str, str | int] = {
        "q": "BTC_USDT",
        "market": "futures:usdt",
        "limit": 20,
    }
    instruments = session.get(
        f"{local_origin}/api/v2/instruments",
        headers=headers,
        params=instrument_params,
        timeout=(3, 10),
    )
    instruments.raise_for_status()
    candidates = instruments.json()["items"]
    instrument = next((item for item in candidates if item["symbol"] == "BTC_USDT"), None)
    if instrument is None:
        raise RuntimeError("BTC_USDT was not found in the formal catalog")
    watchlists = session.get(f"{local_origin}/api/v2/watchlists", headers=headers, timeout=(3, 10))
    watchlists.raise_for_status()
    lists = watchlists.json()["items"]
    if lists:
        watchlist = lists[0]
    else:
        created = session.post(
            f"{local_origin}/api/v2/watchlists",
            headers=protected_headers,
            json={"name": "主要观察"},
            timeout=(3, 10),
        )
        created.raise_for_status()
        watchlist = created.json()
    if not any(item["instrument_id"] == instrument["id"] for item in watchlist["items"]):
        added = session.post(
            f"{local_origin}/api/v2/watchlists/{watchlist['id']}/items",
            headers={**protected_headers, "If-Match": f'"{watchlist["etag"]}"'},
            json={"instrument_id": instrument["id"], "default_timeframe": "15m"},
            timeout=(3, 10),
        )
        added.raise_for_status()
    bars_params: dict[str, str | int] = {
        "instrument_id": str(instrument["id"]),
        "timeframe": "15m",
        "limit": 100,
        "latest": "true",
    }
    bars = session.get(
        f"{local_origin}/api/v2/bars",
        headers=headers,
        params=bars_params,
        timeout=(3, 10),
    )
    bars.raise_for_status()
    if not bars.json()["items"]:
        raise RuntimeError("formal BTC_USDT chart returned no bars")
    print(f"Formal OWNER MFA enrolled; recovery material saved with mode 0600 at {output_path}")
    print(
        "Formal API acceptance passed: "
        f"auth=MFA data_sources={len(sources.json()['items'])} "
        f"watchlist=ready BTC_USDT_bars={len(bars.json()['items'])}"
    )
    return 0


def write_private_json(output_path: Path, payload: dict[str, object]) -> None:
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent, prefix=".formal-mfa-", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output_path)


def recover_active_enrollment(username: str) -> dict[str, str] | None:
    database_url = os.environ.get("GATESCOPE_MIGRATION_DATABASE_URL", "")
    encoded_key = os.environ.get("GATESCOPE_IDENTITY_KEK", "")
    key_version = os.environ.get("GATESCOPE_IDENTITY_KEY_VERSION", "")
    if not database_url or not encoded_key or not key_version:
        return None
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://", 1)) as connection:
        row = connection.execute(
            """SELECT t.id,u.id,m.id,m.encrypted_secret,m.secret_nonce,m.key_version
                 FROM tenants t
                 JOIN memberships ms ON ms.tenant_id=t.id AND ms.status='ACTIVE'
                 JOIN users u ON u.id=ms.user_id
                 JOIN mfa_methods m ON m.tenant_id=t.id AND m.user_id=u.id AND m.status='ACTIVE'
                WHERE t.slug='default' AND u.normalized_username=%s
                ORDER BY m.confirmed_at DESC LIMIT 1""",
            (username.strip().lower(),),
        ).fetchone()
    if row is None:
        return None
    padding = "=" * (-len(encoded_key) % 4)
    key = base64.urlsafe_b64decode(encoded_key + padding)
    protector = AesGcmSecretProtector(key, key_version)
    protected = ProtectedSecret(bytes(row[3]), bytes(row[4]), str(row[5]))
    context = f"gatescope:mfa:{row[0]}:{row[1]}:{row[2]}".encode()
    secret = protector.reveal(protected, context=context).decode("ascii")
    account = quote(f"{username}@{row[0]}", safe="")
    issuer = quote("GateScope", safe="")
    return {
        "method_id": str(row[2]),
        "secret": secret,
        "provisioning_uri": (
            f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}"
            "&algorithm=SHA1&digits=6&period=30"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
