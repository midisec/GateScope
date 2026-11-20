"""OAuth 2.1 / S256, user-bound MCP grants with no persisted bearer secrets.

HTTP/session/CSRF handling belongs to the API. This boundary validates every
OAuth credential binding again, under row locks, including refresh-token reuse.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_mcp import VerifiedMcpToken

OAUTH_DEFAULT_SCOPES = ("mcp:connect", "market:read", "analysis:read")
OAUTH_SCOPES = (*OAUTH_DEFAULT_SCOPES, "research:read", "paper:read", "paper:trade")
OAUTH_EXPLICIT_SCOPES = frozenset({"research:read", "paper:read", "paper:trade"})
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
ACCESS_RE = re.compile(r"^gsoat_[A-Za-z0-9_-]{48}$")
CALLBACK_RE = re.compile(r"^https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,160}$")
STABLE_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
TUNNEL_RESOURCE_ALIAS_RE = re.compile(
    r"https://tunnel-service\.gateway\.unified-0\.internal\.api\.openai\.org/v1/mcp/tunnel_[0-9a-f]{32}"
)


class OAuthError(ValueError):
    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        self.error = error
        self.description = description
        self.status_code = status_code
        super().__init__(description)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(error: str = "invalid_request", description: str = "Invalid OAuth request.", status: int = 400) -> NoReturn:
    raise OAuthError(error, description, status)


def _text(value: Any, minimum: int, maximum: int) -> bool:
    return isinstance(value, str) and minimum <= len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _scopes(value: str | None, allowed: tuple[str, ...] = OAUTH_SCOPES, *,
            default: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if value is not None and not _text(value, 1, 256):
        _fail("invalid_scope", "Only supported MCP and simulated trading scopes are allowed.")
    scopes = (allowed if default is None else default) if value is None else tuple(dict.fromkeys(value.split()))
    if (not scopes or "mcp:connect" not in scopes
            or not set(scopes) <= set(allowed) & set(OAUTH_SCOPES)
            or ("paper:trade" in scopes and "paper:read" not in scopes)):
        _fail("invalid_scope", "Only supported MCP scopes are allowed; paper:trade requires paper:read.")
    return scopes


def _approved_scopes(requested: tuple[str, ...], selected: list[str] | None) -> tuple[str, ...]:
    """Grant a requested subset only; old payloads cannot grant private or trading access."""
    if selected is None:
        return _scopes(None, requested, default=tuple(scope for scope in requested if scope not in OAUTH_EXPLICIT_SCOPES))
    if (not isinstance(selected, list) or not 1 <= len(selected) <= len(OAUTH_SCOPES)
            or any(not isinstance(scope, str) or scope not in OAUTH_SCOPES for scope in selected)
            or len(set(selected)) != len(selected)):
        _fail("invalid_scope", "Select only permissions requested by this client.")
    return _scopes(" ".join(selected), requested)


class PostgresOAuthRepository:
    ACCESS_TTL = timedelta(minutes=15)
    GRANT_TTL = timedelta(days=30)
    PENDING_TTL = timedelta(minutes=10)
    CODE_TTL = timedelta(minutes=5)

    def __init__(self, pool: PostgresPool, *, public_origin: str, resource_aliases: tuple[str, ...] = ()) -> None:
        origin = public_origin.rstrip("/")
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("OAUTH_PUBLIC_ORIGIN_MUST_BE_HTTPS_ORIGIN") from None
        loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if ((parsed.scheme != "https" and not loopback) or not parsed.hostname or not parsed.netloc
                or parsed.path or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("OAUTH_PUBLIC_ORIGIN_MUST_BE_HTTPS_ORIGIN")
        self.pool = pool
        self.issuer = origin
        self.resource = f"{origin}/mcp"
        if (not isinstance(resource_aliases, tuple) or len(resource_aliases) > 1
                or any(not isinstance(alias, str) or TUNNEL_RESOURCE_ALIAS_RE.fullmatch(alias) is None
                       for alias in resource_aliases)):
            raise ValueError("OAUTH_TUNNEL_RESOURCE_ALIAS_CONFIGURATION_INVALID")
        self.resource_aliases = resource_aliases

    def canonical_resource(self, resource: str | None) -> str:
        """Map one operator-approved tunnel transport alias to this same resource.

        This is not audience expansion: pending requests, grants and token
        verification retain the canonical resource. Never derive aliases from
        request headers, registration metadata or a supplied tunnel identifier.
        """
        if isinstance(resource, str) and (resource == self.resource or resource in self.resource_aliases):
            return self.resource
        _fail("invalid_target", "The resource must exactly match this MCP endpoint.")

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects = metadata.get("redirect_uris")
        if (not isinstance(redirects, list) or not 1 <= len(redirects) <= 5
                or any(not isinstance(uri, str) or not (uri == STABLE_CALLBACK or CALLBACK_RE.fullmatch(uri)) for uri in redirects)
                or len(set(redirects)) != len(redirects)):
            _fail("invalid_redirect_uri", "Only the exact ChatGPT HTTPS callback is supported.")
        method = metadata.get("token_endpoint_auth_method", "none")
        if method not in AUTH_METHODS:
            _fail("invalid_client_metadata", "Unsupported client authentication method.")
        if metadata.get("response_types", ["code"]) != ["code"]:
            _fail("invalid_client_metadata", "Only the authorization code flow is supported.")
        grants = metadata.get("grant_types", ["authorization_code", "refresh_token"])
        if (not isinstance(grants, list) or not all(isinstance(item, str) for item in grants)
                or "authorization_code" not in grants or len(grants) != len(set(grants))
                or not set(grants) <= {"authorization_code", "refresh_token"}):
            _fail("invalid_client_metadata", "Unsupported grant type.")
        scopes = _scopes(metadata.get("scope"))
        name = metadata.get("client_name", "ChatGPT")
        if not _text(name, 1, 120):
            _fail("invalid_client_metadata", "Client name must be 1-120 characters.")
        client_id = f"gso_client_{secrets.token_urlsafe(24)}"
        secret = secrets.token_urlsafe(36) if method != "none" else None
        created_at = _now()
        with self.pool.public_transaction() as connection:
            # Serialize a hard registration cap across API processes/restarts.
            connection.execute("SELECT pg_advisory_xact_lock(728429110)")
            connection.execute("SELECT gatescope_security.cleanup_oauth_ephemeral_credentials()")
            count = connection.execute("SELECT count(*) FROM gatescope_security.oauth_registered_clients").fetchone()[0]
            if count >= 10000:
                _fail("temporarily_unavailable", "Client registration capacity reached.", 503)
            connection.execute("""INSERT INTO gatescope_security.oauth_registered_clients
              (client_id,client_name,redirect_uris,auth_method,secret_hash,created_at,grant_types,scopes)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
              (client_id, name, redirects, method, _hash(secret) if secret else None, created_at, grants, list(scopes)))
        result: dict[str, Any] = {
            "client_id": client_id, "client_id_issued_at": int(created_at.timestamp()),
            "client_name": name, "redirect_uris": redirects, "token_endpoint_auth_method": method,
            "grant_types": grants, "response_types": ["code"], "scope": " ".join(scopes),
        }
        if secret is not None:
            result.update(client_secret=secret, client_secret_expires_at=0)
        return result

    def begin_authorization(self, query: dict[str, str], browser_nonce: str) -> str:
        self._nonce(browser_nonce)
        if query.get("response_type") != "code":
            _fail("unsupported_response_type", "Only authorization code is supported.")
        if query.get("code_challenge_method") != "S256" or not CHALLENGE_RE.fullmatch(query.get("code_challenge", "")):
            _fail("invalid_request", "PKCE S256 is required.")
        self.canonical_resource(query.get("resource"))
        if not _text(query.get("state"), 1, 2048):
            _fail("invalid_request", "An opaque state parameter is required.")
        handle = secrets.token_urlsafe(36)
        with self.pool.public_transaction() as connection:
            client = self._client(connection, query.get("client_id", ""))
            # Registration describes capability, not user consent. Omitting
            # scope retains the original defaults, even for newly registered clients.
            scopes = _scopes(query.get("scope"), tuple(client[6]),
                             default=tuple(scope for scope in client[6] if scope in OAUTH_DEFAULT_SCOPES))
            if query.get("redirect_uri") not in client[2]:
                _fail("invalid_request", "The redirect URI must match the registered callback.")
            connection.execute("SELECT pg_advisory_xact_lock(728429111)")
            connection.execute("""DELETE FROM gatescope_security.oauth_pending_requests
              WHERE handle_hash IN (SELECT handle_hash FROM gatescope_security.oauth_pending_requests
              WHERE expires_at<now() ORDER BY expires_at LIMIT 500)""")
            counts = connection.execute("""SELECT count(*),count(*) FILTER(WHERE client_id=%s)
              FROM gatescope_security.oauth_pending_requests WHERE expires_at>now()""", (client[0],)).fetchone()
            if counts[0] >= 5000 or counts[1] >= 30:
                _fail("temporarily_unavailable", "Too many pending authorizations. Try later.", 429)
            connection.execute("""INSERT INTO gatescope_security.oauth_pending_requests
              (handle_hash,nonce_hash,client_id,redirect_uri,state,resource,challenge,scopes,expires_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (_hash(handle), _hash(browser_nonce), client[0], query["redirect_uri"], query["state"],
               self.resource, query["code_challenge"], list(scopes), _now() + self.PENDING_TTL))
        return handle

    def pending_authorization(self, handle: str, browser_nonce: str, tenant_id: UUID, user_id: UUID) -> dict[str, Any]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            pending = self._pending(connection, handle, browser_nonce, tenant_id, user_id)
            client = self._client(connection, pending[0])
            return {"request_id": handle, "client_name": client[1], "scopes": list(pending[5]),
                    "resource": pending[3], "expires_at": pending[6].isoformat()}

    def approve_authorization(self, handle: str, browser_nonce: str, tenant_id: UUID, user_id: UUID,
                              approved_scopes: list[str] | None = None) -> str:
        code = f"gsoc_{secrets.token_urlsafe(36)}"
        grant_id, mcp_client_id = uuid4(), uuid4()
        with self.pool.transaction(tenant_id, user_id) as connection:
            pending = self._pending(connection, handle, browser_nonce, tenant_id, user_id)
            scopes = _approved_scopes(tuple(pending[5]), approved_scopes)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,728429112))", (f"{tenant_id}:{user_id}",))
            active_count = connection.execute("""SELECT count(*) FROM oauth_grants
              WHERE user_id=%s AND revoked_at IS NULL AND expires_at>now()""", (user_id,)).fetchone()[0]
            if active_count >= 100:
                _fail("access_denied", "Revoke an existing ChatGPT connection before adding another.", 403)
            connection.execute("""INSERT INTO mcp_clients
              (id,tenant_id,owner_user_id,name,status,created_at,updated_at)
              VALUES (%s,%s,%s,%s,'ACTIVE',now(),now())""",
              (mcp_client_id, tenant_id, user_id, f"ChatGPT OAuth · {str(grant_id)[:8]}"))
            connection.execute("""INSERT INTO oauth_grants
              (id,tenant_id,user_id,mcp_client_id,oauth_client_id,issuer,resource,scopes,expires_at)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (grant_id, tenant_id, user_id, mcp_client_id, pending[0], self.issuer,
               self.resource, list(scopes), _now() + self.GRANT_TTL))
            connection.execute("""INSERT INTO oauth_codes
              (code_hash,tenant_id,grant_id,redirect_uri,challenge,expires_at)
              VALUES (%s,%s,%s,%s,%s,%s)""",
              (_hash(code), tenant_id, grant_id, pending[1], pending[4], _now() + self.CODE_TTL))
            connection.execute("UPDATE gatescope_security.oauth_pending_requests SET consumed_at=now() WHERE handle_hash=%s", (_hash(handle),))
        return self._redirect(pending, code=code)

    def deny_authorization(self, handle: str, browser_nonce: str, tenant_id: UUID, user_id: UUID) -> str:
        with self.pool.transaction(tenant_id, user_id) as connection:
            pending = self._pending(connection, handle, browser_nonce, tenant_id, user_id)
            connection.execute("UPDATE gatescope_security.oauth_pending_requests SET consumed_at=now() WHERE handle_hash=%s", (_hash(handle),))
        return self._redirect(pending, error="access_denied", error_description="Authorization was declined.")

    def exchange_code(self, *, client_id: str, code: str, code_verifier: str, redirect_uri: str,
                      resource: str, client_secret: str | None = None, auth_method: str = "none") -> dict[str, Any]:
        self._authenticate(client_id, client_secret, auth_method)
        if not _text(code, 40, 100) or not VERIFIER_RE.fullmatch(code_verifier):
            _fail("invalid_grant", "The authorization code is invalid or expired.")
        context = self._locate(_hash(code), "code")
        if context is None:
            _fail("invalid_grant", "The authorization code is invalid or expired.")
        replay = False
        result: dict[str, Any] = {}
        with self.pool.transaction(context[0], context[1]) as connection:
            grant = self._grant(connection, context[2], client_id, resource)
            row = connection.execute("""SELECT redirect_uri,challenge,expires_at,used_at FROM oauth_codes
              WHERE code_hash=%s AND grant_id=%s FOR UPDATE""", (_hash(code), context[2])).fetchone()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            if row is None or row[0] != redirect_uri or not secrets.compare_digest(row[1], challenge):
                _fail("invalid_grant", "Authorization code binding is invalid.")
            if row[3] is not None:
                self._revoke_grant(connection, grant)
                replay = True
            elif row[2] <= _now():
                _fail("invalid_grant", "The authorization code is invalid or expired.")
            else:
                connection.execute("UPDATE oauth_codes SET used_at=now() WHERE code_hash=%s", (_hash(code),))
                result = self._issue(connection, grant, tuple(grant[6]))
        # Raise AFTER committing revocation; exceptions inside transaction roll it back.
        if replay:
            _fail("invalid_grant", "The authorization code was already used.")
        return result

    def refresh(self, *, client_id: str, refresh_token: str, resource: str,
                client_secret: str | None = None, auth_method: str = "none", scope: str | None = None) -> dict[str, Any]:
        client = self._authenticate(client_id, client_secret, auth_method)
        if "refresh_token" not in client[5]:
            _fail("unauthorized_client", "This client is not registered for refresh tokens.")
        if not _text(refresh_token, 40, 100):
            _fail("invalid_grant", "The refresh token is invalid or expired.")
        context = self._locate(_hash(refresh_token), "refresh_token")
        if context is None:
            _fail("invalid_grant", "The refresh token is invalid or expired.")
        replay = False
        result: dict[str, Any] = {}
        with self.pool.transaction(context[0], context[1]) as connection:
            grant = self._grant(connection, context[2], client_id, resource)
            row = connection.execute("""SELECT expires_at,used_at,scopes FROM oauth_refresh_tokens
              WHERE token_hash=%s AND grant_id=%s FOR UPDATE""", (_hash(refresh_token), context[2])).fetchone()
            if row is None or row[0] <= _now():
                _fail("invalid_grant", "The refresh token is invalid or expired.")
            if row[1] is not None:
                self._revoke_grant(connection, grant)
                replay = True
            else:
                scopes = _scopes(scope, tuple(row[2]))
                connection.execute("UPDATE oauth_refresh_tokens SET used_at=now() WHERE token_hash=%s", (_hash(refresh_token),))
                result = self._issue(connection, grant, scopes)
        if replay:
            _fail("invalid_grant", "Refresh token reuse revoked this connection. Reconnect ChatGPT.")
        return result

    def revoke(self, *, client_id: str, token: str, client_secret: str | None = None,
               auth_method: str = "none", token_type_hint: str | None = None) -> None:
        self._authenticate(client_id, client_secret, auth_method)
        if not _text(token, 1, 256):
            return
        kinds = ["access_token", "refresh_token"]
        if token_type_hint in kinds:
            kinds.remove(token_type_hint)
            kinds.insert(0, token_type_hint)
        for kind in kinds:
            context = self._locate(_hash(token), kind)
            if context is None:
                continue
            with self.pool.transaction(context[0], context[1]) as connection:
                row = connection.execute("""SELECT id,tenant_id,user_id,mcp_client_id,oauth_client_id,
                  resource,scopes,expires_at,revoked_at FROM oauth_grants WHERE id=%s FOR UPDATE""", (context[2],)).fetchone()
                if row is not None and row[4] == client_id:
                    self._revoke_grant(connection, row)
            return

    def verify_access(self, raw_token: str) -> VerifiedMcpToken | None:
        if not isinstance(raw_token, str) or not ACCESS_RE.fullmatch(raw_token):
            return None
        with self.pool.public_transaction() as connection:
            row = connection.execute("""SELECT token_id,tenant_id,subject_user_id,client_id,scopes,expires_at
              FROM gatescope_security.resolve_oauth_access(%s,%s,%s)""", (_hash(raw_token), self.issuer, self.resource)).fetchone()
        if row is None:
            return None
        try:
            _scopes(None, tuple(row[4]))
        except OAuthError:
            return None
        principal = VerifiedMcpToken(row[0], row[1], row[2], row[3], tuple(row[4]), row[5])
        with self.pool.transaction(principal.tenant_id, principal.subject_user_id) as connection:
            connection.execute("UPDATE mcp_tokens SET last_used_at=now() WHERE id=%s AND revoked_at IS NULL", (principal.id,))
        return principal

    def _authenticate(self, client_id: str, client_secret: str | None, method: str) -> Any:
        with self.pool.public_transaction() as connection:
            row = self._client(connection, client_id)
        if row[3] != method or (method == "none" and client_secret is not None):
            _fail("invalid_client", "Client authentication failed.", 401)
        if method != "none" and (not _text(client_secret, 40, 128) or not secrets.compare_digest(row[4], _hash(client_secret))):
            _fail("invalid_client", "Client authentication failed.", 401)
        return row

    @staticmethod
    def _client(connection: Any, client_id: str) -> Any:
        if not _text(client_id, 20, 100):
            _fail("invalid_client", "Unknown OAuth client.", 401)
        row = connection.execute("""SELECT client_id,client_name,redirect_uris,auth_method,secret_hash,grant_types,scopes
          FROM gatescope_security.oauth_registered_clients WHERE client_id=%s""", (client_id,)).fetchone()
        if row is None:
            _fail("invalid_client", "Unknown OAuth client.", 401)
        return row

    @staticmethod
    def _nonce(nonce: str) -> None:
        if not _text(nonce, 32, 128):
            _fail("invalid_request", "Authorization browser session is missing.")

    def _pending(self, connection: Any, handle: str, nonce: str, tenant: UUID, user: UUID) -> Any:
        self._nonce(nonce)
        if not _text(handle, 40, 100):
            _fail("invalid_request", "Authorization request is invalid or expired.")
        self._owner(connection, tenant, user)
        row = connection.execute("""SELECT client_id,redirect_uri,state,resource,challenge,scopes,
          expires_at,nonce_hash,tenant_id,user_id,consumed_at FROM gatescope_security.oauth_pending_requests
          WHERE handle_hash=%s FOR UPDATE""", (_hash(handle),)).fetchone()
        if (row is None or row[6] <= _now() or row[10] is not None
                or not secrets.compare_digest(row[7], _hash(nonce)) or row[3] != self.resource
                or (row[8] is not None and (row[8] != tenant or row[9] != user))):
            _fail("invalid_request", "Authorization request is invalid or expired.")
        if row[8] is None:
            connection.execute("""UPDATE gatescope_security.oauth_pending_requests SET tenant_id=%s,user_id=%s
              WHERE handle_hash=%s""", (tenant, user, _hash(handle)))
        return row

    @staticmethod
    def _owner(connection: Any, tenant: UUID, user: UUID) -> None:
        row = connection.execute("""SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
          JOIN tenants t ON t.id=m.tenant_id WHERE m.tenant_id=%s AND m.user_id=%s
          AND m.status='ACTIVE' AND m.role='OWNER' AND u.status='ACTIVE' AND t.status='ACTIVE'""", (tenant, user)).fetchone()
        if row is None:
            _fail("access_denied", "An active OWNER account is required for MCP authorization.", 403)

    def _grant(self, connection: Any, grant_id: UUID, client_id: str, resource: str) -> Any:
        self.canonical_resource(resource)
        row = connection.execute("""SELECT g.id,g.tenant_id,g.user_id,g.mcp_client_id,g.oauth_client_id,
          g.resource,g.scopes,g.expires_at,g.revoked_at FROM oauth_grants g
          JOIN mcp_clients mc ON mc.id=g.mcp_client_id AND mc.tenant_id=g.tenant_id
          WHERE g.id=%s AND g.oauth_client_id=%s AND g.resource=%s AND g.issuer=%s
            AND mc.status='ACTIVE' AND mc.owner_user_id=g.user_id FOR UPDATE OF g,mc""",
          (grant_id, client_id, self.resource, self.issuer)).fetchone()
        if row is None or row[8] is not None or row[7] <= _now():
            _fail("invalid_grant", "This authorization is invalid, expired or revoked.")
        _scopes(None, tuple(row[6]))
        self._owner(connection, row[1], row[2])
        return row

    def _locate(self, hashed: str, kind: str) -> Any:
        with self.pool.public_transaction() as connection:
            return connection.execute("SELECT tenant_id,user_id,grant_id FROM gatescope_security.locate_oauth_credential(%s,%s)", (hashed, kind)).fetchone()

    def _issue(self, connection: Any, grant: Any, scopes: tuple[str, ...]) -> dict[str, Any]:
        scopes = _scopes(" ".join(scopes), tuple(grant[6]))
        counts = connection.execute("""SELECT count(*) FILTER(WHERE mt.created_at>now()-interval '1 minute'),count(*)
          FROM oauth_access_tokens a JOIN mcp_tokens mt ON mt.id=a.token_id
          WHERE a.grant_id=%s AND mt.tenant_id=%s AND mt.client_id=%s
            AND mt.created_at>now()-interval '1 day'""", (grant[0], grant[1], grant[3])).fetchone()
        if counts[0] >= 10 or counts[1] >= 256:
            _fail("temporarily_unavailable", "Too many token requests. Try again later.", 429)
        access = f"gsoat_{secrets.token_urlsafe(36)}"
        refresh = f"gsort_{secrets.token_urlsafe(36)}"
        token_id = uuid4()
        now = _now()
        expires = min(now + self.ACCESS_TTL, grant[7])
        connection.execute("""INSERT INTO mcp_tokens
          (id,tenant_id,client_id,created_by,label,token_hash,token_hint,scopes,expires_at,created_at)
          VALUES (%s,%s,%s,%s,'ChatGPT OAuth',%s,%s,%s,%s,%s)""",
          (token_id, grant[1], grant[3], grant[2], _hash(access), f"{access[:10]}...{access[-4:]}", list(scopes), expires, now))
        connection.execute("INSERT INTO oauth_access_tokens (token_id,tenant_id,grant_id) VALUES (%s,%s,%s)", (token_id, grant[1], grant[0]))
        connection.execute("SELECT gatescope_security.register_mcp_token_locator(%s,%s)", (_hash(access), token_id))
        result = {"access_token": access, "token_type": "Bearer", "expires_in": max(1, int((expires-now).total_seconds())),
                  "scope": " ".join(scopes)}
        client = self._client(connection, grant[4])
        if "refresh_token" in client[5]:
            connection.execute("""INSERT INTO oauth_refresh_tokens (token_hash,tenant_id,grant_id,scopes,expires_at)
              VALUES (%s,%s,%s,%s,%s)""", (_hash(refresh), grant[1], grant[0], list(scopes), grant[7]))
            result["refresh_token"] = refresh
        return result

    @staticmethod
    def _revoke_grant(connection: Any, grant: Any) -> None:
        connection.execute("UPDATE oauth_grants SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s", (grant[0],))
        connection.execute("UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at,now()) WHERE client_id=%s", (grant[3],))
        connection.execute("UPDATE mcp_clients SET status='REVOKED',etag=etag+1,updated_at=now() WHERE id=%s AND status='ACTIVE'", (grant[3],))
        connection.execute("SELECT gatescope_security.remove_mcp_client_locators(%s)", (grant[3],))

    def _redirect(self, pending: Any, **parameters: str) -> str:
        return f"{pending[1]}?{urlencode({**parameters, 'state': pending[2], 'iss': self.issuer})}"
