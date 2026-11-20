"""Temporary Phase 0 single-tenant auth. Replaced by formal identity in Phase 1."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from gatescope.observability import event

USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}$")
PASSWORD_MIN_BYTES = 16
PASSWORD_MAX_BYTES = 256
SESSION_COOKIE = "gatescope_session"
CSRF_COOKIE = "gatescope_csrf"


class AuthError(RuntimeError):
    pass


class InvalidCredentials(AuthError):
    pass


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    username: str
    role: str
    csrf_hash: str
    expires_at: int


@dataclass(frozen=True)
class NewSession:
    authenticated: AuthenticatedSession
    token: str
    csrf_token: str


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip()
    if not USERNAME_RE.fullmatch(normalized):
        raise ValueError("username must be 3-64 ASCII letters, digits, dot, dash or underscore")
    return normalized


def validate_password(password: str) -> None:
    length = len(password.encode("utf-8"))
    if length < PASSWORD_MIN_BYTES or length > PASSWORD_MAX_BYTES:
        raise ValueError(f"password must be {PASSWORD_MIN_BYTES}-{PASSWORD_MAX_BYTES} UTF-8 bytes")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


class AuthService:
    MAX_FAILURES = 5
    LOCK_SECONDS = 15 * 60

    def __init__(self, database: Any, session_ttl_seconds: int = 43_200) -> None:
        self.database = database
        self.session_ttl_seconds = session_ttl_seconds
        self.password_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
        self._dummy_hash = self.password_hasher.hash(secrets.token_urlsafe(32))

    def user_count(self) -> int:
        with self.database.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0])

    def bootstrap(self, username: str, password: str, trace_id: str = "bootstrap") -> bool:
        username = normalize_username(username)
        validate_password(password)
        now = int(time.time())
        with self.database.connect() as conn:
            if int(conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]) > 0:
                return False
            user_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO auth_users
                (id, username, password_hash, role, enabled, failed_attempts, created_at, updated_at)
                VALUES (?, ?, ?, 'OWNER', 1, 0, ?, ?)
                """,
                (user_id, username, self.password_hasher.hash(password), now, now),
            )
            self._audit(conn, "auth.bootstrap", "success", user_id, trace_id)
        event("auth.bootstrap", subject_id=user_id, outcome="success")
        return True

    def authenticate(self, username: str, password: str, trace_id: str) -> NewSession:
        now = int(time.time())
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = ""

        denied = False
        new_session: NewSession | None = None
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_users WHERE username=? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            encoded = password.encode("utf-8", "ignore")
            password_in_bounds = PASSWORD_MIN_BYTES <= len(encoded) <= PASSWORD_MAX_BYTES
            candidate_hash = str(row["password_hash"]) if row is not None else self._dummy_hash
            verified = False
            try:
                verified = self.password_hasher.verify(candidate_hash, password if password_in_bounds else "invalid")
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                verified = False

            if row is None or not bool(row["enabled"]) or int(row["locked_until"] or 0) > now or not verified:
                subject_id = str(row["id"]) if row is not None else None
                if row is not None and bool(row["enabled"]) and int(row["locked_until"] or 0) <= now:
                    failures = int(row["failed_attempts"] or 0) + 1
                    locked_until = now + self.LOCK_SECONDS if failures >= self.MAX_FAILURES else None
                    conn.execute(
                        "UPDATE auth_users SET failed_attempts=?, locked_until=?, updated_at=? WHERE id=?",
                        (failures, locked_until, now, subject_id),
                    )
                self._audit(conn, "auth.login", "denied", subject_id, trace_id)
                denied = True
            else:
                user_id = str(row["id"])
                session_id = str(uuid.uuid4())
                token = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                expires_at = now + self.session_ttl_seconds
                conn.execute(
                    "UPDATE auth_users SET failed_attempts=0, locked_until=NULL, last_login_at=?, updated_at=? WHERE id=?",
                    (now, now, user_id),
                )
                conn.execute(
                    """
                    INSERT INTO auth_sessions
                    (id, user_id, token_hash, csrf_hash, created_at, authenticated_at, last_seen_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, user_id, token_hash(token), token_hash(csrf_token), now, now, now, expires_at),
                )
                self._audit(conn, "auth.login", "success", user_id, trace_id)
                authenticated = AuthenticatedSession(
                    session_id=session_id,
                    user_id=user_id,
                    username=str(row["username"]),
                    role=str(row["role"]),
                    csrf_hash=token_hash(csrf_token),
                    expires_at=expires_at,
                )
                new_session = NewSession(authenticated=authenticated, token=token, csrf_token=csrf_token)
        if denied or new_session is None:
            raise InvalidCredentials("invalid credentials")
        return new_session

    def session(self, token: str | None) -> AuthenticatedSession | None:
        if not token or len(token) > 128:
            return None
        now = int(time.time())
        digest = token_hash(token)
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT s.id AS session_id, s.user_id, s.csrf_hash, s.expires_at,
                       u.username, u.role, u.enabled
                FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?
                """,
                (digest, now),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                return None
            conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (now, row["session_id"]))
        return AuthenticatedSession(
            session_id=str(row["session_id"]),
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            csrf_hash=str(row["csrf_hash"]),
            expires_at=int(row["expires_at"]),
        )

    @staticmethod
    def valid_csrf(session: AuthenticatedSession, cookie_token: str | None, header_token: str | None) -> bool:
        if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
            return False
        try:
            digest = token_hash(header_token)
        except (UnicodeError, ValueError):
            return False
        return hmac.compare_digest(session.csrf_hash, digest)

    def revoke(self, session_id: str, trace_id: str) -> None:
        now = int(time.time())
        with self.database.connect() as conn:
            row = conn.execute("SELECT user_id FROM auth_sessions WHERE id=?", (session_id,)).fetchone()
            conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, session_id))
            self._audit(conn, "auth.logout", "success", str(row["user_id"]) if row else None, trace_id)

    @staticmethod
    def _audit(conn: Any, action: str, outcome: str, subject_id: str | None, trace_id: str) -> None:
        conn.execute(
            """
            INSERT INTO security_audit_events
            (id, occurred_at, action, outcome, subject_id, trace_id, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), int(time.time()), action, outcome, subject_id, trace_id[:64], json.dumps({})),
        )
