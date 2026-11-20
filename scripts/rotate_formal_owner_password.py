#!/usr/bin/env python3
"""Rotate one formal OWNER password without exposing it on argv or stdout."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb

from gatescope.auth import normalize_username, validate_password


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "runtime-secrets" / "owner-login.json"


def main() -> int:
    if os.environ.get("GATESCOPE_CONFIRM_OWNER_PASSWORD_ROTATION") != "ROTATE":
        raise SystemExit("set GATESCOPE_CONFIRM_OWNER_PASSWORD_ROTATION=ROTATE")
    database_url = os.environ.get("GATESCOPE_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("GATESCOPE_DATABASE_URL is required")
    public_origin = os.environ.get("GATESCOPE_PUBLIC_ORIGIN", "").rstrip("/")
    parsed_origin = urlparse(public_origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.netloc
        or parsed_origin.path
        or parsed_origin.params
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise SystemExit("GATESCOPE_PUBLIC_ORIGIN must be an HTTPS origin")
    tenant_slug = os.environ.get("GATESCOPE_OWNER_TENANT", "default").strip().lower()
    username = normalize_username(
        os.environ.get("GATESCOPE_OWNER_USERNAME", "owner"),
    ).lower()
    password = secrets.token_urlsafe(32)
    validate_password(password)
    encoded = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(password)
    connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    trace_id = f"admin-password-rotate-{uuid4().hex}"
    display_username: str
    now = datetime.now(timezone.utc)

    with psycopg.connect(connection_url) as connection:
        row = connection.execute(
            """SELECT u.id, u.display_username, m.tenant_id
                 FROM users u
                 JOIN memberships m ON m.user_id=u.id
                 JOIN tenants t ON t.id=m.tenant_id
                WHERE u.normalized_username=%s AND t.slug=%s
                  AND m.role='OWNER' AND m.status='ACTIVE'
                FOR UPDATE OF u""",
            (username, tenant_slug),
        ).fetchone()
        if row is None:
            raise SystemExit("active formal OWNER was not found")
        user_id, display_username, tenant_id = UUID(str(row[0])), str(row[1]), UUID(str(row[2]))
        connection.execute(
            """UPDATE users
                  SET password_hash=%s, password_changed_at=%s, failed_attempts=0,
                      locked_until=NULL, etag=etag+1, updated_at=%s
                WHERE id=%s""",
            (encoded, now, now, user_id),
        )
        connection.execute(
            """INSERT INTO password_history
                 (id, tenant_id, user_id, password_hash, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (uuid4(), tenant_id, user_id, encoded, now),
        )
        connection.execute(
            "UPDATE sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
            (now, user_id),
        )
        _append_audit(connection, tenant_id, user_id, trace_id, now)

    _write_credentials(display_username, password, public_origin, now)
    password = ""
    print("Formal OWNER password rotated; sessions revoked; restricted login file restored")
    return 0


def _append_audit(
    connection: psycopg.Connection[tuple[object, ...]],
    tenant_id: UUID,
    user_id: UUID,
    trace_id: str,
    occurred_at: datetime,
) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(tenant_id),))
    previous = connection.execute(
        """SELECT fact_hash FROM identity_audit_facts
            WHERE tenant_id=%s ORDER BY occurred_at DESC, id DESC LIMIT 1""",
        (tenant_id,),
    ).fetchone()
    previous_hash = str(previous[0]) if previous else None
    fact_id = uuid4()
    payload = {
        "action": "password.admin_rotate",
        "actor_id": None,
        "fact_id": str(fact_id),
        "occurred_at": occurred_at.isoformat(),
        "outcome": "SUCCESS",
        "previous_hash": previous_hash,
        "subject_id": str(user_id),
        "tenant_id": str(tenant_id),
        "trace_id": trace_id[:64],
    }
    fact_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    connection.execute(
        """INSERT INTO identity_audit_facts
             (id, tenant_id, actor_user_id, subject_user_id, action, outcome,
              occurred_at, trace_id, detail_json, previous_hash, fact_hash)
           VALUES (%s, %s, NULL, %s, %s, 'SUCCESS', %s, %s, %s, %s, %s)""",
        (
            fact_id,
            tenant_id,
            user_id,
            "password.admin_rotate",
            occurred_at,
            trace_id[:64],
            Jsonb({"source": "local-admin"}),
            previous_hash,
            fact_hash,
        ),
    )


def _write_credentials(
    username: str,
    password: str,
    public_origin: str,
    created_at: datetime,
) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.chmod(0o700)
    document = {
        "username": username,
        "password": password,
        "login_url": f"{public_origin}/login",
        "created_at": created_at.isoformat(),
        "warning": (
            "Use HTTPS only. Keep this file mode 0600. Never send the password "
            "to chat, logs, tickets, or Git."
        ),
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=OUTPUT.parent,
        prefix=".owner-login-",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(OUTPUT)


if __name__ == "__main__":
    raise SystemExit(main())
