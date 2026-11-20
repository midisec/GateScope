"""PostgreSQL identity service with tenant-bound opaque sessions and MFA."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from psycopg import Connection
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.auth import normalize_username, token_hash, validate_password
from gatescope.domain.identity import Role
from gatescope.security_secrets import (
    AesGcmSecretProtector,
    ProtectedSecret,
    generate_recovery_codes,
    generate_totp_secret,
    recovery_code_hash,
    verify_totp,
)

TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


class FormalAuthError(RuntimeError):
    pass


class FormalInvalidCredentials(FormalAuthError):
    pass


class MfaRequired(FormalAuthError):
    pass


class PasswordReuse(FormalAuthError):
    pass


class LastOwnerRequired(FormalAuthError):
    pass


class LastMfaMethodRequired(FormalAuthError):
    pass


class IdentityConflict(FormalAuthError):
    pass


class IdentityPreconditionFailed(FormalAuthError):
    pass


@dataclass(frozen=True)
class FormalSession:
    session_id: UUID
    tenant_id: UUID
    user_id: UUID
    username: str
    role: Role
    expires_at: datetime
    recent_auth_at: datetime | None
    mfa_verified_at: datetime | None
    auth_strength: str
    mfa_required: bool

    @property
    def fully_authenticated(self) -> bool:
        return not self.mfa_required or self.mfa_verified_at is not None


@dataclass(frozen=True)
class FormalNewSession:
    session: FormalSession
    token: str
    csrf_token: str


@dataclass(frozen=True)
class TotpEnrollment:
    method_id: UUID
    secret: str
    provisioning_uri: str


class FormalIdentityService:
    MAX_FAILURES = 5
    LOCK_DURATION = timedelta(minutes=15)

    def __init__(
        self,
        pool: PostgresPool,
        protector: AesGcmSecretProtector,
        recovery_pepper: bytes,
        *,
        session_ttl: timedelta = timedelta(hours=12),
    ) -> None:
        if len(recovery_pepper) < 32:
            raise ValueError("recovery pepper must contain at least 32 bytes")
        if not timedelta(minutes=15) <= session_ttl <= timedelta(days=1):
            raise ValueError("session ttl must be between 15 minutes and one day")
        self.pool = pool
        self.protector = protector
        self.recovery_pepper = recovery_pepper
        self.session_ttl = session_ttl
        self.password_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
        self._dummy_hash = self.password_hasher.hash(secrets.token_urlsafe(32))

    def authenticate_password(
        self,
        tenant_slug: str,
        username: str,
        password: str,
        *,
        trace_id: str,
    ) -> FormalNewSession:
        slug = tenant_slug.strip().lower()
        try:
            normalized = normalize_username(username).lower()
        except ValueError:
            normalized = ""
        tenant_id, user_id = self._public_identity(slug, normalized)
        if tenant_id is None or user_id is None:
            self._verify_password(self._dummy_hash, password)
            raise FormalInvalidCredentials("invalid credentials")

        now = datetime.now(timezone.utc)
        denied = False
        new_session: FormalNewSession | None = None
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """SELECT u.id, u.display_username, u.password_hash, u.status,
                          u.mfa_required, u.failed_attempts, u.locked_until,
                          m.role, m.status AS membership_status
                   FROM users u
                   LEFT JOIN memberships m ON m.user_id = u.id AND m.tenant_id = %s
                   WHERE u.id = %s FOR UPDATE OF u""",
                (tenant_id, user_id),
            ).fetchone()
            candidate_hash = str(row[2]) if row is not None else self._dummy_hash
            password_valid = self._verify_password(candidate_hash, password)
            permitted = bool(
                row is not None
                and str(row[3]) == "ACTIVE"
                and str(row[8]) == "ACTIVE"
                and (row[6] is None or row[6] <= now)
                and password_valid
            )
            if not permitted:
                if row is not None and str(row[8]) == "ACTIVE":
                    failures = int(row[5]) + 1
                    locked_until = now + self.LOCK_DURATION if failures >= self.MAX_FAILURES else None
                    connection.execute(
                        "UPDATE users SET failed_attempts=%s, locked_until=%s, updated_at=%s WHERE id=%s",
                        (failures, locked_until, now, user_id),
                    )
                    self._audit(connection, tenant_id, user_id, user_id, "auth.login", "DENIED", trace_id)
                denied = True
            else:
                assert row is not None
                session_id = uuid4()
                secret = secrets.token_urlsafe(32)
                token = _session_token(tenant_id, user_id, session_id, secret)
                csrf_token = secrets.token_urlsafe(32)
                expires_at = now + self.session_ttl
                connection.execute(
                    "UPDATE users SET failed_attempts=0, locked_until=NULL, updated_at=%s WHERE id=%s",
                    (now, user_id),
                )
                connection.execute(
                    """INSERT INTO sessions
                         (id, tenant_id, user_id, token_hash, csrf_hash, authenticated_at,
                          recent_auth_at, expires_at, last_seen_at, created_at, auth_strength)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PASSWORD')""",
                    (
                        session_id, tenant_id, user_id, token_hash(token), token_hash(csrf_token),
                        now, now, expires_at, now, now,
                    ),
                )
                self._audit(connection, tenant_id, user_id, user_id, "auth.login", "SUCCESS", trace_id)
                session = FormalSession(
                    session_id=session_id, tenant_id=tenant_id, user_id=user_id,
                    username=str(row[1]), role=Role(str(row[7])), expires_at=expires_at,
                    recent_auth_at=now, mfa_verified_at=None, auth_strength="PASSWORD",
                    mfa_required=bool(row[4]),
                )
                new_session = FormalNewSession(session, token, csrf_token)
        if denied or new_session is None:
            raise FormalInvalidCredentials("invalid credentials")
        return new_session

    def session(self, token: str | None) -> FormalSession | None:
        parsed = _parse_session_token(token)
        if parsed is None:
            return None
        tenant_id, user_id, session_id = parsed
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """SELECT s.id, s.expires_at, s.recent_auth_at, s.mfa_verified_at,
                          s.auth_strength, u.display_username, u.mfa_required, m.role
                   FROM sessions s
                   JOIN users u ON u.id = s.user_id
                   JOIN memberships m ON m.tenant_id = s.tenant_id AND m.user_id = s.user_id
                   WHERE s.id=%s AND s.tenant_id=%s AND s.user_id=%s AND s.token_hash=%s
                     AND s.revoked_at IS NULL AND s.expires_at>%s
                     AND u.status='ACTIVE' AND m.status='ACTIVE'""",
                (session_id, tenant_id, user_id, token_hash(str(token)), now),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE sessions SET last_seen_at=%s WHERE id=%s", (now, session_id))
            return FormalSession(
                session_id=session_id, tenant_id=tenant_id, user_id=user_id,
                username=str(row[5]), role=Role(str(row[7])), expires_at=row[1],
                recent_auth_at=row[2], mfa_verified_at=row[3], auth_strength=str(row[4]),
                mfa_required=bool(row[6]),
            )

    def valid_csrf(self, session: FormalSession, cookie_token: str | None, header_token: str | None) -> bool:
        if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
            return False
        try:
            digest = token_hash(header_token)
        except (UnicodeError, ValueError):
            return False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            row = connection.execute("SELECT csrf_hash FROM sessions WHERE id=%s", (session.session_id,)).fetchone()
        return row is not None and hmac.compare_digest(str(row[0]), digest)

    def enroll_totp(self, session: FormalSession, *, label: str, issuer: str = "GateScope") -> TotpEnrollment:
        clean_label = label.strip()
        if not 1 <= len(clean_label) <= 80:
            raise ValueError("MFA label must be between 1 and 80 characters")
        if not issuer or len(issuer) > 80:
            raise ValueError("TOTP issuer is invalid")
        method_id = uuid4()
        secret = generate_totp_secret()
        context = _mfa_context(session.tenant_id, session.user_id, method_id)
        protected = self.protector.protect(secret.encode("ascii"), context=context)
        now = datetime.now(timezone.utc)
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            connection.execute(
                """INSERT INTO mfa_methods
                     (id, tenant_id, user_id, method_type, label, status, encrypted_secret,
                      secret_nonce, key_version, created_at)
                   VALUES (%s, %s, %s, 'TOTP', %s, 'PENDING', %s, %s, %s, %s)""",
                (
                    method_id, session.tenant_id, session.user_id, clean_label,
                    protected.ciphertext, protected.nonce, protected.key_version, now,
                ),
            )
        account = quote(f"{session.username}@{session.tenant_id}", safe="")
        encoded_issuer = quote(issuer, safe="")
        uri = f"otpauth://totp/{encoded_issuer}:{account}?secret={secret}&issuer={encoded_issuer}&algorithm=SHA1&digits=6&period=30"
        return TotpEnrollment(method_id, secret, uri)

    def confirm_totp(self, session: FormalSession, method_id: UUID, code: str, *, trace_id: str) -> None:
        now = datetime.now(timezone.utc)
        denied = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            row = connection.execute(
                """SELECT encrypted_secret, secret_nonce, key_version, last_accepted_step
                   FROM mfa_methods WHERE id=%s AND user_id=%s AND status='PENDING' FOR UPDATE""",
                (method_id, session.user_id),
            ).fetchone()
            if row is None:
                raise FormalAuthError("pending MFA method not found")
            secret = self._reveal_totp(session, method_id, row)
            step = verify_totp(secret, code, last_accepted_step=row[3])
            if step is None:
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.confirm", "DENIED", trace_id)
                denied = True
            else:
                connection.execute(
                    """UPDATE mfa_methods SET status='ACTIVE', confirmed_at=%s,
                              last_accepted_step=%s, etag=etag+1 WHERE id=%s""",
                    (now, step, method_id),
                )
                connection.execute(
                    "UPDATE sessions SET mfa_verified_at=%s, auth_strength='MFA' WHERE id=%s",
                    (now, session.session_id),
                )
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.confirm", "SUCCESS", trace_id)
        if denied:
            raise FormalInvalidCredentials("invalid MFA code")

    def verify_session_mfa(self, session: FormalSession, code: str, *, trace_id: str) -> FormalSession:
        now = datetime.now(timezone.utc)
        denied = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            rows = connection.execute(
                """SELECT id, encrypted_secret, secret_nonce, key_version, last_accepted_step
                   FROM mfa_methods WHERE user_id=%s AND status='ACTIVE' FOR UPDATE""",
                (session.user_id,),
            ).fetchall()
            accepted: tuple[UUID, int] | None = None
            for row in rows:
                method_id = row[0]
                secret = self._reveal_totp(session, method_id, row[1:])
                if (step := verify_totp(secret, code, last_accepted_step=row[4])) is not None:
                    accepted = method_id, step
                    break
            if accepted is None:
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.verify", "DENIED", trace_id)
                denied = True
            else:
                connection.execute(
                    "UPDATE mfa_methods SET last_accepted_step=%s, etag=etag+1 WHERE id=%s",
                    (accepted[1], accepted[0]),
                )
                connection.execute(
                    "UPDATE sessions SET mfa_verified_at=%s, auth_strength='MFA' WHERE id=%s",
                    (now, session.session_id),
                )
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.verify", "SUCCESS", trace_id)
        if denied:
            raise FormalInvalidCredentials("invalid MFA code")
        return replace(session, mfa_verified_at=now, auth_strength="MFA")

    def regenerate_recovery_codes(self, session: FormalSession, *, trace_id: str) -> tuple[str, ...]:
        codes = generate_recovery_codes()
        now = datetime.now(timezone.utc)
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM recovery_codes WHERE user_id=%s",
                (session.user_id,),
            ).fetchone()
            generation = int(row[0]) if row else 1
            connection.execute(
                "UPDATE recovery_codes SET used_at=%s WHERE user_id=%s AND used_at IS NULL",
                (now, session.user_id),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO recovery_codes
                         (id, tenant_id, user_id, code_hash, generation, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    [
                        (
                            uuid4(), session.tenant_id, session.user_id,
                            recovery_code_hash(code, self.recovery_pepper), generation, now,
                        )
                        for code in codes
                    ],
                )
            self._audit(connection, session.tenant_id, session.user_id, session.user_id, "recovery.regenerate", "SUCCESS", trace_id)
        return codes

    def consume_recovery_code(self, session: FormalSession, code: str, *, trace_id: str) -> FormalSession:
        digest = recovery_code_hash(code, self.recovery_pepper)
        now = datetime.now(timezone.utc)
        denied = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            row = connection.execute(
                """UPDATE recovery_codes SET used_at=%s
                   WHERE tenant_id=%s AND user_id=%s AND code_hash=%s AND used_at IS NULL
                     AND (expires_at IS NULL OR expires_at>%s)
                   RETURNING id""",
                (now, session.tenant_id, session.user_id, digest, now),
            ).fetchone()
            if row is None:
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "recovery.consume", "DENIED", trace_id)
                denied = True
            else:
                connection.execute(
                    "UPDATE sessions SET mfa_verified_at=%s, auth_strength='RECOVERY' WHERE id=%s",
                    (now, session.session_id),
                )
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "recovery.consume", "SUCCESS", trace_id)
        if denied:
            raise FormalInvalidCredentials("invalid recovery code")
        return replace(session, mfa_verified_at=now, auth_strength="RECOVERY")

    def revoke_session(self, session: FormalSession, target_session_id: UUID, *, trace_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            changed = connection.execute(
                "UPDATE sessions SET revoked_at=%s WHERE id=%s AND user_id=%s AND revoked_at IS NULL RETURNING id",
                (now, target_session_id, session.user_id),
            ).fetchone()
            if changed is None:
                raise FormalAuthError("session not found")
            self._audit(connection, session.tenant_id, session.user_id, session.user_id, "session.revoke", "SUCCESS", trace_id)

    def list_sessions(self, session: FormalSession) -> list[dict[str, Any]]:
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            rows = connection.execute(
                """SELECT id, authenticated_at, last_seen_at, expires_at, revoked_at,
                          mfa_verified_at, auth_strength
                   FROM sessions WHERE user_id=%s ORDER BY created_at DESC, id DESC""",
                (session.user_id,),
            ).fetchall()
        return [
            {
                "id": str(row[0]), "authenticated_at": row[1], "last_seen_at": row[2],
                "expires_at": row[3], "revoked_at": row[4], "mfa_verified_at": row[5],
                "auth_strength": str(row[6]), "current": row[0] == session.session_id,
            }
            for row in rows
        ]

    def change_password(
        self,
        session: FormalSession,
        current_password: str,
        new_password: str,
        *,
        trace_id: str,
    ) -> FormalSession:
        validate_password(new_password)
        now = datetime.now(timezone.utc)
        denied = False
        reused = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            user = connection.execute(
                "SELECT password_hash FROM users WHERE id=%s FOR UPDATE",
                (session.user_id,),
            ).fetchone()
            history = connection.execute(
                """SELECT password_hash FROM password_history
                   WHERE user_id=%s ORDER BY created_at DESC, id DESC LIMIT 5""",
                (session.user_id,),
            ).fetchall()
            if user is None or not self._verify_password(str(user[0]), current_password):
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "password.change", "DENIED", trace_id)
                denied = True
            elif any(self._verify_password(str(row[0]), new_password) for row in history):
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "password.change", "DENIED", trace_id)
                reused = True
            else:
                encoded = self.password_hasher.hash(new_password)
                connection.execute(
                    """UPDATE users SET password_hash=%s, password_changed_at=%s,
                              failed_attempts=0, locked_until=NULL, etag=etag+1, updated_at=%s
                       WHERE id=%s""",
                    (encoded, now, now, session.user_id),
                )
                connection.execute(
                    """INSERT INTO password_history
                         (id, tenant_id, user_id, password_hash, created_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (uuid4(), session.tenant_id, session.user_id, encoded, now),
                )
                connection.execute(
                    "UPDATE sessions SET revoked_at=%s WHERE user_id=%s AND id<>%s AND revoked_at IS NULL",
                    (now, session.user_id, session.session_id),
                )
                connection.execute(
                    "UPDATE sessions SET recent_auth_at=%s WHERE id=%s",
                    (now, session.session_id),
                )
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "password.change", "SUCCESS", trace_id)
        if denied:
            raise FormalInvalidCredentials("invalid credentials")
        if reused:
            raise PasswordReuse("password was recently used")
        return replace(session, recent_auth_at=now)

    def reauthenticate_password(
        self,
        session: FormalSession,
        password: str,
        *,
        trace_id: str,
    ) -> FormalSession:
        """Refresh recent-auth for the current session without rotating credentials."""
        now = datetime.now(timezone.utc)
        denied = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            user = connection.execute(
                "SELECT password_hash FROM users WHERE id=%s FOR UPDATE",
                (session.user_id,),
            ).fetchone()
            if user is None or not self._verify_password(str(user[0]), password):
                self._audit(
                    connection,
                    session.tenant_id,
                    session.user_id,
                    session.user_id,
                    "password.reauthenticate",
                    "DENIED",
                    trace_id,
                )
                denied = True
            else:
                connection.execute(
                    "UPDATE sessions SET recent_auth_at=%s WHERE id=%s AND revoked_at IS NULL",
                    (now, session.session_id),
                )
                self._audit(
                    connection,
                    session.tenant_id,
                    session.user_id,
                    session.user_id,
                    "password.reauthenticate",
                    "SUCCESS",
                    trace_id,
                )
        if denied:
            raise FormalInvalidCredentials("invalid credentials")
        return replace(session, recent_auth_at=now)

    def list_mfa_methods(self, session: FormalSession) -> list[dict[str, Any]]:
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            rows = connection.execute(
                """SELECT id, method_type, label, status, etag, created_at, confirmed_at
                   FROM mfa_methods WHERE user_id=%s AND status<>'REVOKED'
                   ORDER BY created_at, id""",
                (session.user_id,),
            ).fetchall()
        return [
            {
                "id": str(row[0]), "method_type": str(row[1]), "label": str(row[2]),
                "status": str(row[3]), "etag": int(row[4]), "created_at": row[5],
                "confirmed_at": row[6],
            }
            for row in rows
        ]

    def remove_mfa_method(self, session: FormalSession, method_id: UUID, *, trace_id: str) -> None:
        now = datetime.now(timezone.utc)
        denied = False
        missing = False
        with self.pool.transaction(session.tenant_id, session.user_id) as connection:
            methods = connection.execute(
                """SELECT id, status FROM mfa_methods
                   WHERE user_id=%s AND status IN ('PENDING','ACTIVE') FOR UPDATE""",
                (session.user_id,),
            ).fetchall()
            target = next((row for row in methods if row[0] == method_id), None)
            active_count = sum(1 for row in methods if str(row[1]) == "ACTIVE")
            if target is None:
                missing = True
            elif str(target[1]) == "ACTIVE" and session.mfa_required and active_count <= 1:
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.remove", "DENIED", trace_id)
                denied = True
            else:
                connection.execute(
                    "UPDATE mfa_methods SET status='REVOKED', revoked_at=%s, etag=etag+1 WHERE id=%s",
                    (now, method_id),
                )
                self._audit(connection, session.tenant_id, session.user_id, session.user_id, "mfa.remove", "SUCCESS", trace_id)
        if missing:
            raise FormalAuthError("MFA method not found")
        if denied:
            raise LastMfaMethodRequired("last required MFA method cannot be removed")

    def list_tenant_users(self, actor: FormalSession) -> list[dict[str, Any]]:
        self._require_owner(actor, recent=False)
        with self.pool.transaction(actor.tenant_id, actor.user_id) as connection:
            rows = connection.execute(
                """SELECT u.id, u.display_username, u.normalized_username, u.status,
                          u.mfa_required, u.etag, m.id, m.role, m.status, m.etag
                   FROM memberships m JOIN users u ON u.id=m.user_id
                   ORDER BY u.normalized_username, u.id"""
            ).fetchall()
        return [
            {
                "id": str(row[0]), "username": str(row[1]), "normalized_username": str(row[2]),
                "status": str(row[3]), "mfa_required": bool(row[4]), "etag": int(row[5]),
                "membership": {
                    "id": str(row[6]), "role": str(row[7]), "status": str(row[8]), "etag": int(row[9]),
                },
            }
            for row in rows
        ]

    def create_tenant_user(
        self,
        actor: FormalSession,
        username: str,
        password: str,
        role: Role,
        *,
        trace_id: str,
    ) -> dict[str, Any]:
        self._require_owner(actor)
        normalized = normalize_username(username).lower()
        validate_password(password)
        encoded = self.password_hasher.hash(password)
        user_id, membership_id = uuid4(), uuid4()
        now = datetime.now(timezone.utc)
        conflict = False
        with self.pool.transaction(actor.tenant_id, actor.user_id) as connection:
            inserted = connection.execute(
                """INSERT INTO users
                     (id, normalized_username, display_username, password_hash, status,
                      mfa_required, password_changed_at, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, 'ACTIVE', true, %s, %s, %s)
                   ON CONFLICT (normalized_username) DO NOTHING RETURNING id""",
                (user_id, normalized, username.strip(), encoded, now, now, now),
            ).fetchone()
            if inserted is None:
                conflict = True
            else:
                connection.execute(
                    """INSERT INTO memberships
                         (id, tenant_id, user_id, role, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'ACTIVE', %s, %s)""",
                    (membership_id, actor.tenant_id, user_id, role.value, now, now),
                )
                connection.execute(
                    """INSERT INTO password_history
                         (id, tenant_id, user_id, password_hash, created_at)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (uuid4(), actor.tenant_id, user_id, encoded, now),
                )
                self._audit(connection, actor.tenant_id, actor.user_id, user_id, "user.create", "SUCCESS", trace_id)
        if conflict:
            raise IdentityConflict("username is unavailable")
        return {
            "id": str(user_id), "username": username.strip(), "status": "ACTIVE",
            "mfa_required": True,
            "membership": {"id": str(membership_id), "role": role.value, "status": "ACTIVE", "etag": 1},
        }

    def update_membership(
        self,
        actor: FormalSession,
        membership_id: UUID,
        *,
        role: Role,
        status: str,
        expected_etag: int,
        trace_id: str,
    ) -> dict[str, Any]:
        self._require_owner(actor)
        if status not in {"ACTIVE", "REVOKED"} or expected_etag < 1:
            raise ValueError("membership update is invalid")
        now = datetime.now(timezone.utc)
        missing = stale = last_owner = False
        subject_id: UUID | None = None
        new_etag = expected_etag + 1
        with self.pool.transaction(actor.tenant_id, actor.user_id) as connection:
            # Serialize membership changes before taking any membership row
            # locks. Locking each target first lets two OWNER removals deadlock.
            # Keep this namespace separate from the audit-chain lock: other
            # identity operations acquire that lock after their session writes.
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("SET LOCAL statement_timeout='10s'")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"membership:{actor.tenant_id}",),
            )
            now = datetime.now(timezone.utc)
            active_actor = connection.execute(
                """SELECT m.id FROM memberships m
                   JOIN users u ON u.id=m.user_id
                   JOIN tenants t ON t.id=m.tenant_id
                   JOIN sessions s ON s.tenant_id=m.tenant_id AND s.user_id=m.user_id
                   WHERE m.tenant_id=%s AND m.user_id=%s AND m.role='OWNER'
                     AND m.status='ACTIVE' AND u.status='ACTIVE' AND t.status='ACTIVE'
                     AND s.id=%s AND s.revoked_at IS NULL AND s.expires_at>%s
                     AND s.recent_auth_at>=%s
                     AND (NOT u.mfa_required OR s.mfa_verified_at IS NOT NULL)""",
                (actor.tenant_id, actor.user_id, actor.session_id, now, now-timedelta(minutes=10)),
            ).fetchone()
            if active_actor is None:
                raise FormalAuthError("active OWNER with recent authentication required")
            # Count usable administrators, not globally disabled users whose
            # old membership still says OWNER. A stable order also protects
            # callers which subsequently need more than one membership row.
            owners = connection.execute(
                """SELECT m.id FROM memberships m JOIN users u ON u.id=m.user_id
                   WHERE m.tenant_id=%s AND m.role='OWNER' AND m.status='ACTIVE'
                     AND u.status='ACTIVE' ORDER BY m.id FOR UPDATE OF m""",
                (actor.tenant_id,),
            ).fetchall()
            target = connection.execute(
                """SELECT user_id, role, status, etag FROM memberships
                   WHERE id=%s AND tenant_id=%s FOR UPDATE""",
                (membership_id, actor.tenant_id),
            ).fetchone()
            if target is None:
                missing = True
            elif int(target[3]) != expected_etag:
                stale = True
            else:
                subject_id = target[0]
                removes_owner = membership_id in {row[0] for row in owners} and (
                    role is not Role.OWNER or status != "ACTIVE"
                )
                if removes_owner and len(owners) <= 1:
                    self._audit(connection, actor.tenant_id, actor.user_id, subject_id, "membership.update", "DENIED", trace_id)
                    last_owner = True
                else:
                    connection.execute(
                        """UPDATE memberships SET role=%s, status=%s, etag=etag+1, updated_at=%s
                           WHERE id=%s AND tenant_id=%s""",
                        (role.value, status, now, membership_id, actor.tenant_id),
                    )
                    if status == "REVOKED":
                        connection.execute(
                            """UPDATE sessions SET revoked_at=%s
                               WHERE tenant_id=%s AND user_id=%s AND revoked_at IS NULL""",
                            (now, actor.tenant_id, subject_id),
                        )
                    self._audit(connection, actor.tenant_id, actor.user_id, subject_id, "membership.update", "SUCCESS", trace_id)
        if missing:
            raise FormalAuthError("membership not found")
        if stale:
            raise IdentityPreconditionFailed("membership etag changed")
        if last_owner:
            raise LastOwnerRequired("last active OWNER cannot be removed")
        return {
            "id": str(membership_id), "user_id": str(subject_id),
            "role": role.value, "status": status, "etag": new_etag,
        }

    @staticmethod
    def _require_owner(actor: FormalSession, *, recent: bool = True) -> None:
        now = datetime.now(timezone.utc)
        if actor.role is not Role.OWNER or not actor.fully_authenticated:
            raise FormalAuthError("OWNER authorization required")
        if recent and (
            actor.recent_auth_at is None or actor.recent_auth_at < now - timedelta(minutes=10)
        ):
            raise FormalAuthError("recent authentication required")

    def _public_identity(self, slug: str, username: str) -> tuple[UUID | None, UUID | None]:
        if not TENANT_SLUG_RE.fullmatch(slug) or not username:
            return None, None
        with self.pool.public_transaction() as connection:
            tenant = connection.execute("SELECT id FROM tenants WHERE slug=%s AND status='ACTIVE'", (slug,)).fetchone()
            user = connection.execute("SELECT id FROM users WHERE normalized_username=%s", (username,)).fetchone()
        return (tenant[0] if tenant else None), (user[0] if user else None)

    def _reveal_totp(self, session: FormalSession, method_id: UUID, row: Any) -> str:
        protected = ProtectedSecret(bytes(row[0]), bytes(row[1]), str(row[2]))
        return self.protector.reveal(
            protected, context=_mfa_context(session.tenant_id, session.user_id, method_id)
        ).decode("ascii")

    def _verify_password(self, encoded_hash: str, password: str) -> bool:
        candidate = password
        in_bounds = True
        try:
            validate_password(password)
        except ValueError:
            candidate = "invalid-password-padding"
            in_bounds = False
        try:
            verified = bool(self.password_hasher.verify(encoded_hash, candidate))
            return in_bounds and verified
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    @staticmethod
    def _audit(
        connection: Connection[Any],
        tenant_id: UUID,
        actor_id: UUID | None,
        subject_id: UUID | None,
        action: str,
        outcome: str,
        trace_id: str,
    ) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(tenant_id),))
        previous = connection.execute(
            "SELECT fact_hash FROM identity_audit_facts ORDER BY occurred_at DESC, id DESC LIMIT 1"
        ).fetchone()
        previous_hash = str(previous[0]) if previous else None
        fact_id = uuid4()
        occurred_at = datetime.now(timezone.utc)
        payload = {
            "action": action, "actor_id": str(actor_id) if actor_id else None,
            "fact_id": str(fact_id), "occurred_at": occurred_at.isoformat(),
            "outcome": outcome, "previous_hash": previous_hash,
            "subject_id": str(subject_id) if subject_id else None,
            "tenant_id": str(tenant_id), "trace_id": trace_id[:64],
        }
        fact_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        connection.execute(
            """INSERT INTO identity_audit_facts
                 (id, tenant_id, actor_user_id, subject_user_id, action, outcome,
                  occurred_at, trace_id, detail_json, previous_hash, fact_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                fact_id, tenant_id, actor_id, subject_id, action, outcome,
                occurred_at, trace_id[:64], Jsonb({}), previous_hash, fact_hash,
            ),
        )


def _session_token(tenant_id: UUID, user_id: UUID, session_id: UUID, secret: str) -> str:
    return f"{tenant_id.hex}.{user_id.hex}.{session_id.hex}.{secret}"


def _parse_session_token(token: str | None) -> tuple[UUID, UUID, UUID] | None:
    if not token or len(token) > 256:
        return None
    parts = token.split(".")
    if len(parts) != 4 or len(parts[3]) < 32:
        return None
    try:
        return UUID(hex=parts[0]), UUID(hex=parts[1]), UUID(hex=parts[2])
    except ValueError:
        return None


def _mfa_context(tenant_id: UUID, user_id: UUID, method_id: UUID) -> bytes:
    return f"gatescope:mfa:{tenant_id}:{user_id}:{method_id}".encode()
