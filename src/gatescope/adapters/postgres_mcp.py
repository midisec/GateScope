"""Persistence and authentication boundary for tenant-scoped MCP access."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from gatescope.adapters.postgres import PostgresPool
from gatescope.auth import token_hash

MCP_SCOPES = frozenset({"mcp:connect", "market:read", "analysis:read", "alerts:read", "research:read", "paper:read", "paper:trade"})
MCP_TOKEN_RE = re.compile(r"^gsmcp_[A-Za-z0-9_-]{40,80}$")


class McpNotFound(LookupError):
    pass


class McpConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class McpClient:
    id: UUID
    name: str
    status: str
    etag: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class McpToken:
    id: UUID
    client_id: UUID
    label: str
    token_hint: str
    scopes: tuple[str, ...]
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class NewMcpToken:
    token: McpToken
    secret: str


@dataclass(frozen=True)
class VerifiedMcpToken:
    id: UUID
    tenant_id: UUID
    subject_user_id: UUID
    client_id: UUID
    scopes: tuple[str, ...]
    expires_at: datetime


class PostgresMcpRepository:
    """MCP credentials are shown once; persistence contains only a SHA-256 hash."""

    MAX_TOKEN_TTL = timedelta(days=90)

    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def list_clients(self, tenant_id: UUID, owner_id: UUID) -> list[McpClient]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                """SELECT id, name, status, etag, created_at, updated_at
                   FROM mcp_clients WHERE owner_user_id=%s
                   ORDER BY created_at DESC, id DESC""",
                (owner_id,),
            ).fetchall()
        return [self._client(row) for row in rows]

    def create_client(self, tenant_id: UUID, owner_id: UUID, *, name: str) -> McpClient:
        client_id = uuid4()
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                connection.execute(
                    """INSERT INTO mcp_clients
                         (id, tenant_id, owner_user_id, name, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 'ACTIVE', now(), now())""",
                    (client_id, tenant_id, owner_id, _label(name, "client name", 120)),
                )
        except UniqueViolation as exc:
            raise McpConflict("MCP client name already exists") from exc
        return self.get_client(tenant_id, owner_id, client_id)

    def get_client(self, tenant_id: UUID, owner_id: UUID, client_id: UUID) -> McpClient:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """SELECT id, name, status, etag, created_at, updated_at
                   FROM mcp_clients WHERE id=%s AND owner_user_id=%s""",
                (client_id, owner_id),
            ).fetchone()
        if row is None:
            raise McpNotFound("MCP client not found")
        return self._client(row)

    def revoke_client(self, tenant_id: UUID, owner_id: UUID, client_id: UUID) -> McpClient:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """UPDATE mcp_clients SET status='REVOKED', etag=etag+1, updated_at=now()
                   WHERE id=%s AND owner_user_id=%s AND status='ACTIVE'
                   RETURNING id""",
                (client_id, owner_id),
            ).fetchone()
            if row is None:
                found = connection.execute(
                    "SELECT id FROM mcp_clients WHERE id=%s AND owner_user_id=%s",
                    (client_id, owner_id),
                ).fetchone()
                if found is None:
                    raise McpNotFound("MCP client not found")
                raise McpConflict("MCP client is already revoked")
            connection.execute(
                """UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at, now())
                   WHERE client_id=%s""",
                (client_id,),
            )
            connection.execute(
                "SELECT gatescope_security.remove_mcp_client_locators(%s)",
                (client_id,),
            )
        return self.get_client(tenant_id, owner_id, client_id)

    def list_tokens(self, tenant_id: UUID, owner_id: UUID, client_id: UUID) -> list[McpToken]:
        self.get_client(tenant_id, owner_id, client_id)
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                """SELECT t.id, t.client_id, t.label, t.token_hint, t.scopes,
                          t.expires_at, t.last_used_at, t.revoked_at, t.created_at
                   FROM mcp_tokens t
                   JOIN mcp_clients c ON c.id=t.client_id AND c.tenant_id=t.tenant_id
                   WHERE t.client_id=%s AND c.owner_user_id=%s
                   ORDER BY t.created_at DESC, t.id DESC""",
                (client_id, owner_id),
            ).fetchall()
        return [self._token(row) for row in rows]

    def create_token(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        client_id: UUID,
        *,
        label: str,
        scopes: Iterable[str],
        ttl: timedelta,
    ) -> NewMcpToken:
        normalized_scopes = _scopes(scopes)
        if not timedelta(hours=1) <= ttl <= self.MAX_TOKEN_TTL:
            raise ValueError("MCP token TTL must be between one hour and 90 days")
        token_id = uuid4()
        secret = f"gsmcp_{secrets.token_urlsafe(36)}"
        hashed = token_hash(secret)
        hint = f"{secret[:10]}...{secret[-4:]}"
        expires_at = datetime.now(timezone.utc) + ttl
        with self.pool.transaction(tenant_id, owner_id) as connection:
            client = connection.execute(
                """SELECT id FROM mcp_clients
                   WHERE id=%s AND owner_user_id=%s AND status='ACTIVE' FOR UPDATE""",
                (client_id, owner_id),
            ).fetchone()
            if client is None:
                raise McpNotFound("active MCP client not found")
            connection.execute(
                """INSERT INTO mcp_tokens
                     (id, tenant_id, client_id, created_by, label, token_hash, token_hint,
                      scopes, expires_at, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
                (
                    token_id, tenant_id, client_id, owner_id,
                    _label(label, "token label", 80), hashed, hint,
                    list(normalized_scopes), expires_at,
                ),
            )
            connection.execute(
                "SELECT gatescope_security.register_mcp_token_locator(%s, %s)",
                (hashed, token_id),
            )
        created = next(
            item for item in self.list_tokens(tenant_id, owner_id, client_id) if item.id == token_id
        )
        return NewMcpToken(created, secret)

    def revoke_token(
        self, tenant_id: UUID, owner_id: UUID, client_id: UUID, token_id: UUID,
    ) -> McpToken:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """UPDATE mcp_tokens t SET revoked_at=now()
                   FROM mcp_clients c
                   WHERE t.id=%s AND t.client_id=%s AND t.revoked_at IS NULL
                     AND c.id=t.client_id AND c.tenant_id=t.tenant_id AND c.owner_user_id=%s
                   RETURNING t.id""",
                (token_id, client_id, owner_id),
            ).fetchone()
            if row is None:
                found = connection.execute(
                    """SELECT t.id FROM mcp_tokens t JOIN mcp_clients c ON c.id=t.client_id
                       WHERE t.id=%s AND t.client_id=%s AND c.owner_user_id=%s""",
                    (token_id, client_id, owner_id),
                ).fetchone()
                if found is None:
                    raise McpNotFound("MCP token not found")
                raise McpConflict("MCP token is already revoked")
            connection.execute(
                "SELECT gatescope_security.remove_mcp_token_locator(%s)",
                (token_id,),
            )
        return next(
            item for item in self.list_tokens(tenant_id, owner_id, client_id) if item.id == token_id
        )

    def verify_token(self, raw_token: str) -> VerifiedMcpToken | None:
        if MCP_TOKEN_RE.fullmatch(raw_token) is None:
            return None
        hashed = token_hash(raw_token)
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT token_id, tenant_id, subject_user_id, client_id, scopes, expires_at
                   FROM gatescope_security.resolve_mcp_token(%s)""",
                (hashed,),
            ).fetchone()
        if row is None:
            return None
        verified = VerifiedMcpToken(
            id=row[0], tenant_id=row[1], subject_user_id=row[2], client_id=row[3],
            scopes=tuple(str(item) for item in row[4]), expires_at=row[5],
        )
        with self.pool.transaction(verified.tenant_id, verified.subject_user_id) as connection:
            connection.execute(
                "UPDATE mcp_tokens SET last_used_at=now() WHERE id=%s AND revoked_at IS NULL",
                (verified.id,),
            )
        return verified

    def audit_invocation(
        self,
        principal: VerifiedMcpToken,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        outcome: str,
        trace_id: str,
        safe_error_code: str | None = None,
    ) -> None:
        serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        argument_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        with self.pool.transaction(principal.tenant_id, principal.subject_user_id) as connection:
            connection.execute(
                """INSERT INTO mcp_audit_facts
                     (id, tenant_id, token_id, client_id, subject_user_id, tool_name,
                      argument_hash, outcome, safe_error_code, trace_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
                (
                    uuid4(), principal.tenant_id, principal.id, principal.client_id,
                    principal.subject_user_id, tool_name, argument_hash, outcome,
                    safe_error_code, trace_id,
                ),
            )

    @staticmethod
    def _client(row: Any) -> McpClient:
        return McpClient(
            id=row[0], name=str(row[1]), status=str(row[2]), etag=int(row[3]),
            created_at=row[4], updated_at=row[5],
        )

    @staticmethod
    def _token(row: Any) -> McpToken:
        return McpToken(
            id=row[0], client_id=row[1], label=str(row[2]), token_hint=str(row[3]),
            scopes=tuple(str(item) for item in row[4]), expires_at=row[5],
            last_used_at=row[6], revoked_at=row[7], created_at=row[8],
        )


def _label(value: str, field: str, maximum: int) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= maximum:
        raise ValueError(f"{field} must be between 1 and {maximum} characters")
    return cleaned


def _scopes(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if "mcp:connect" not in normalized:
        raise ValueError("mcp:connect scope is required")
    if not normalized or not set(normalized) <= MCP_SCOPES:
        raise ValueError("MCP token contains an unsupported scope")
    if "paper:trade" in normalized and "paper:read" not in normalized:
        raise ValueError("paper:trade requires paper:read")
    return normalized
