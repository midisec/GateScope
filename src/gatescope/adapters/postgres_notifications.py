"""Telegram connection and notification channel persistence boundaries."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.auth import token_hash


class NotificationNotFound(LookupError):
    pass


class NotificationConflict(RuntimeError):
    pass


class NotificationPreconditionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramLink:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class TelegramConnection:
    id: UUID
    telegram_user_id: int
    telegram_chat_id: int
    telegram_username: str | None
    status: str
    etag: int
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationChannel:
    id: UUID
    alias: str
    status: str
    connection_id: UUID
    etag: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TelegramStartResult:
    accepted: bool
    duplicate: bool
    connection_id: UUID | None = None


@dataclass(frozen=True)
class TelegramPrincipal:
    tenant_id: UUID
    user_id: UUID
    role: str


@dataclass(frozen=True)
class Operation:
    id: UUID
    operation_type: str
    resource_type: str | None
    resource_id: UUID | None
    state: str
    progress: int
    result_link: str | None
    safe_error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationRoute:
    id: UUID
    channel_id: UUID
    name: str
    status: str
    etag: int
    spec: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationEvent:
    id: UUID
    event_type: str
    channel_alias: str
    status: str
    telegram_message_id: int | None
    safe_error_code: str | None
    created_at: datetime
    updated_at: datetime


class PostgresNotificationRepository:
    LINK_TTL = timedelta(minutes=5)

    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def create_link_token(self, tenant_id: UUID, user_id: UUID, session_id: UUID) -> TelegramLink:
        now = datetime.now(timezone.utc)
        token = secrets.token_urlsafe(32)
        expires_at = now + self.LINK_TTL
        digest = token_hash(token)
        with self.pool.transaction(tenant_id, user_id) as connection:
            connection.execute(
                """UPDATE telegram_link_tokens SET consumed_at=%s
                   WHERE user_id=%s AND consumed_at IS NULL""",
                (now, user_id),
            )
            connection.execute(
                """INSERT INTO telegram_link_tokens
                     (id, tenant_id, user_id, session_id, token_hash, expires_at,
                      attempt_count, max_attempts, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 0, 5, %s)""",
                (uuid4(), tenant_id, user_id, session_id, digest, expires_at, now),
            )
            connection.execute(
                "SELECT gatescope_security.register_telegram_link_locator(%s, %s, %s)",
                (digest, tenant_id, expires_at),
            )
        return TelegramLink(token=token, expires_at=expires_at)

    def accept_private_start(
        self,
        token: str,
        *,
        update_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_username: str | None,
        expected_tenant_id: UUID | None = None,
    ) -> TelegramStartResult:
        digest = token_hash(_valid_token(token))
        with self.pool.public_transaction() as connection:
            locator = connection.execute(
                "SELECT gatescope_security.resolve_telegram_link_tenant(%s)", (digest,),
            ).fetchone()
        if locator is None or locator[0] is None:
            return TelegramStartResult(accepted=False, duplicate=False)
        tenant_id = locator[0]
        if expected_tenant_id is not None and tenant_id != expected_tenant_id:
            return TelegramStartResult(accepted=False, duplicate=False)
        now = datetime.now(timezone.utc)
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "chat_id": telegram_chat_id,
                    "update_id": update_id,
                    "user_id": telegram_user_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.pool.transaction(tenant_id, tenant_id) as connection:
            inbox = connection.execute(
                """INSERT INTO inbox_events
                     (id, tenant_id, source, external_id, payload_hash, received_at, created_at)
                   VALUES (%s, %s, 'TELEGRAM_LINK', %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, source, external_id) DO NOTHING RETURNING id""",
                (uuid4(), tenant_id, str(update_id), payload_hash, now, now),
            ).fetchone()
            if inbox is None:
                return TelegramStartResult(accepted=True, duplicate=True)
            row = connection.execute(
                """SELECT t.id, t.user_id, t.session_id, t.expires_at,
                          t.attempt_count, t.max_attempts, t.consumed_at
                   FROM telegram_link_tokens t
                   JOIN sessions s ON s.id=t.session_id AND s.user_id=t.user_id
                   WHERE t.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>%s
                   FOR UPDATE OF t""",
                (digest, now),
            ).fetchone()
            if row is None:
                return TelegramStartResult(accepted=False, duplicate=False)
            valid = row[6] is None and row[3] > now and int(row[4]) < int(row[5])
            if not valid:
                connection.execute(
                    """UPDATE telegram_link_tokens
                       SET attempt_count=LEAST(attempt_count+1, max_attempts) WHERE id=%s""",
                    (row[0],),
                )
                return TelegramStartResult(accepted=False, duplicate=False)
            connection.execute(
                "UPDATE telegram_link_tokens SET consumed_at=%s, attempt_count=attempt_count+1 WHERE id=%s",
                (now, row[0]),
            )
            connection.execute(
                "SELECT gatescope_security.remove_telegram_link_locator(%s)", (digest,),
            )
            connection_row = connection.execute(
                """INSERT INTO telegram_connections
                     (id, tenant_id, user_id, telegram_user_id, telegram_chat_id,
                      telegram_username, status, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'PENDING_CONFIRMATION', %s, %s)
                   ON CONFLICT (tenant_id, user_id, telegram_chat_id) DO UPDATE
                     SET telegram_user_id=EXCLUDED.telegram_user_id,
                         telegram_username=EXCLUDED.telegram_username,
                         updated_at=EXCLUDED.updated_at
                   RETURNING id""",
                (
                    uuid4(), tenant_id, row[1], telegram_user_id, telegram_chat_id,
                    _telegram_username(telegram_username), now, now,
                ),
            ).fetchone()
            connection.execute("UPDATE inbox_events SET processed_at=%s WHERE id=%s", (now, inbox[0]))
            return TelegramStartResult(
                accepted=True,
                duplicate=False,
                connection_id=connection_row[0] if connection_row else None,
            )

    def resolve_active_telegram_principal(
        self, telegram_user_id: int, telegram_chat_id: int,
    ) -> TelegramPrincipal | None:
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT tenant_id,subject_user_id,role
                   FROM gatescope_security.resolve_active_telegram_principal(%s,%s)""",
                (telegram_user_id, telegram_chat_id),
            ).fetchone()
        return TelegramPrincipal(row[0], row[1], str(row[2])) if row is not None else None

    def accept_command_update(
        self,
        principal: TelegramPrincipal,
        *,
        update_id: int,
        command_text: str,
    ) -> bool:
        payload_hash = hashlib.sha256(command_text.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self.pool.transaction(principal.tenant_id, principal.user_id) as connection:
            row = connection.execute(
                """INSERT INTO inbox_events
                     (id,tenant_id,source,external_id,payload_hash,received_at,created_at,processed_at)
                   VALUES (%s,%s,'TELEGRAM_COMMAND',%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id,source,external_id) DO NOTHING RETURNING id""",
                (
                    uuid4(), principal.tenant_id, str(update_id), payload_hash,
                    now, now, now,
                ),
            ).fetchone()
        return row is not None

    def list_connections(self, tenant_id: UUID, user_id: UUID) -> list[TelegramConnection]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            rows = connection.execute(
                """SELECT id, telegram_user_id, telegram_chat_id, telegram_username,
                          status, etag, verified_at, created_at, updated_at
                   FROM telegram_connections
                   WHERE user_id=%s AND status<>'REVOKED' ORDER BY created_at DESC, id DESC""",
                (user_id,),
            ).fetchall()
        return [self._connection(row) for row in rows]

    def confirm_connection(
        self,
        tenant_id: UUID,
        user_id: UUID,
        connection_id: UUID,
    ) -> TelegramConnection:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """UPDATE telegram_connections
                   SET status='ACTIVE', verified_at=now(), etag=etag+1, updated_at=now()
                   WHERE id=%s AND user_id=%s AND status='PENDING_CONFIRMATION'
                   RETURNING id, telegram_user_id, telegram_chat_id, telegram_username,
                             status, etag, verified_at, created_at, updated_at""",
                (connection_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("pending Telegram connection not found")
        return self._connection(row)

    def list_channels(self, tenant_id: UUID, user_id: UUID) -> list[NotificationChannel]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            rows = connection.execute(
                """SELECT id, alias, status, connection_id, etag, created_at, updated_at
                   FROM notification_channels WHERE owner_user_id=%s AND status<>'REVOKED'
                   ORDER BY created_at DESC, id DESC""",
                (user_id,),
            ).fetchall()
        return [self._channel(row) for row in rows]

    def get_channel(self, tenant_id: UUID, user_id: UUID, channel_id: UUID) -> NotificationChannel:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """SELECT id, alias, status, connection_id, etag, created_at, updated_at
                   FROM notification_channels
                   WHERE id=%s AND owner_user_id=%s AND status<>'REVOKED'""",
                (channel_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("notification channel not found")
        return self._channel(row)

    def create_channel(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        connection_id: UUID,
        alias: str,
    ) -> NotificationChannel:
        channel_id = uuid4()
        try:
            with self.pool.transaction(tenant_id, user_id) as connection:
                linked = connection.execute(
                    """SELECT id FROM telegram_connections
                       WHERE id=%s AND user_id=%s AND status='ACTIVE'""",
                    (connection_id, user_id),
                ).fetchone()
                if linked is None:
                    raise NotificationConflict("Telegram connection is not active")
                row = connection.execute(
                    """INSERT INTO notification_channels
                         (id, tenant_id, owner_user_id, channel_type, alias, status,
                          connection_id, created_at, updated_at)
                       VALUES (%s, %s, %s, 'TELEGRAM', %s, 'ACTIVE', %s, now(), now())
                       RETURNING id, alias, status, connection_id, etag, created_at, updated_at""",
                    (channel_id, tenant_id, user_id, _alias(alias), connection_id),
                ).fetchone()
        except UniqueViolation as exc:
            raise NotificationConflict("notification channel alias already exists") from exc
        return self._channel(row)

    def revoke_channel(self, tenant_id: UUID, user_id: UUID, channel_id: UUID) -> None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """UPDATE notification_channels
                   SET status='REVOKED', etag=etag+1, updated_at=now()
                   WHERE id=%s AND owner_user_id=%s AND status<>'REVOKED' RETURNING id""",
                (channel_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("notification channel not found")

    def list_routes(self, tenant_id: UUID, user_id: UUID) -> list[NotificationRoute]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            rows = connection.execute(
                self._route_select()
                + " WHERE nr.owner_user_id=%s AND nr.status<>'DELETED' ORDER BY nr.updated_at DESC, nr.id DESC",
                (user_id,),
            ).fetchall()
        return [self._route(row) for row in rows]

    def get_route(self, tenant_id: UUID, user_id: UUID, route_id: UUID) -> NotificationRoute:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                self._route_select()
                + " WHERE nr.id=%s AND nr.owner_user_id=%s AND nr.status<>'DELETED'",
                (route_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("notification route not found")
        return self._route(row)

    def create_route(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        name: str,
        spec: Mapping[str, Any],
    ) -> NotificationRoute:
        route_id = uuid4()
        event_types, filters, quiet_hours, rate_policy = _route_parts(spec)
        try:
            with self.pool.transaction(tenant_id, user_id) as connection:
                self._require_channel(connection, user_id, UUID(str(spec["channel_id"])))
                connection.execute(
                    """INSERT INTO notification_routes
                         (id, tenant_id, owner_user_id, channel_id, name, event_types,
                          filters_json, quiet_hours_json, rate_policy_json, status,
                          created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())""",
                    (
                        route_id, tenant_id, user_id, spec["channel_id"], _alias(name),
                        event_types, Jsonb(filters), Jsonb(quiet_hours), Jsonb(rate_policy),
                        "ACTIVE" if bool(spec["enabled"]) else "PAUSED",
                    ),
                )
        except UniqueViolation as exc:
            raise NotificationConflict("active notification route name already exists") from exc
        return self.get_route(tenant_id, user_id, route_id)

    def update_route(
        self,
        tenant_id: UUID,
        user_id: UUID,
        route_id: UUID,
        *,
        name: str,
        spec: Mapping[str, Any],
        expected_etag: int,
    ) -> NotificationRoute:
        event_types, filters, quiet_hours, rate_policy = _route_parts(spec)
        try:
            with self.pool.transaction(tenant_id, user_id) as connection:
                self._require_channel(connection, user_id, UUID(str(spec["channel_id"])))
                row = connection.execute(
                    """UPDATE notification_routes
                       SET channel_id=%s, name=%s, event_types=%s, filters_json=%s,
                           quiet_hours_json=%s, rate_policy_json=%s,
                           status=%s, etag=etag+1, updated_at=now()
                       WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' AND etag=%s
                       RETURNING id""",
                    (
                        spec["channel_id"], _alias(name), event_types, Jsonb(filters),
                        Jsonb(quiet_hours), Jsonb(rate_policy),
                        "ACTIVE" if bool(spec["enabled"]) else "PAUSED",
                        route_id, user_id, expected_etag,
                    ),
                ).fetchone()
                if row is None:
                    found = connection.execute(
                        """SELECT etag FROM notification_routes
                           WHERE id=%s AND owner_user_id=%s AND status<>'DELETED'""",
                        (route_id, user_id),
                    ).fetchone()
                    if found is None:
                        raise NotificationNotFound("notification route not found")
                    raise NotificationPreconditionFailed("notification route etag changed")
        except UniqueViolation as exc:
            raise NotificationConflict("active notification route name already exists") from exc
        return self.get_route(tenant_id, user_id, route_id)

    def delete_route(self, tenant_id: UUID, user_id: UUID, route_id: UUID) -> None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """UPDATE notification_routes
                   SET status='DELETED', etag=etag+1, updated_at=now()
                   WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' RETURNING id""",
                (route_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("notification route not found")

    def enqueue_test(self, tenant_id: UUID, user_id: UUID, channel_id: UUID) -> UUID:
        event_id = uuid4()
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, user_id) as connection:
            channel = connection.execute(
                """SELECT id FROM notification_channels
                   WHERE id=%s AND owner_user_id=%s AND status='ACTIVE'""",
                (channel_id, user_id),
            ).fetchone()
            if channel is None:
                raise NotificationNotFound("active notification channel not found")
            connection.execute(
                """INSERT INTO operations_current
                     (id, tenant_id, actor_user_id, operation_type, resource_type,
                      resource_id, state, progress, version, expires_at, created_at, updated_at)
                   VALUES (%s, %s, %s, 'NOTIFICATION_TEST', 'NOTIFICATION_CHANNEL',
                           %s, 'QUEUED', 0, 1, %s, %s, %s)""",
                (event_id, tenant_id, user_id, channel_id, now + timedelta(days=1), now, now),
            )
            connection.execute(
                """INSERT INTO outbox_events
                     (event_id, tenant_id, aggregate_type, aggregate_id, event_type,
                      schema_version, payload_json, occurred_at, created_at, next_attempt_at)
                   VALUES (%s, %s, 'NOTIFICATION_CHANNEL', %s, 'notification.test.requested.v1',
                           1, %s, %s, %s, %s)""",
                (
                    event_id, tenant_id, channel_id,
                    Jsonb({"channel_id": str(channel_id), "requested_by": str(user_id)}),
                    now, now, now,
                ),
            )
        return event_id

    def get_operation(self, tenant_id: UUID, user_id: UUID, operation_id: UUID) -> Operation:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """SELECT id, operation_type, resource_type, resource_id, state, progress,
                          result_link, safe_error_code, version, created_at, updated_at
                   FROM operations_current
                   WHERE id=%s AND actor_user_id=%s AND (expires_at IS NULL OR expires_at>now())""",
                (operation_id, user_id),
            ).fetchone()
        if row is None:
            raise NotificationNotFound("operation not found")
        return Operation(
            id=row[0], operation_type=str(row[1]),
            resource_type=str(row[2]) if row[2] else None, resource_id=row[3],
            state=str(row[4]), progress=int(row[5]),
            result_link=str(row[6]) if row[6] else None,
            safe_error_code=str(row[7]) if row[7] else None, version=int(row[8]),
            created_at=row[9], updated_at=row[10],
        )

    def list_notification_events(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
    ) -> list[NotificationEvent]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            rows = connection.execute(
                """SELECT nd.id,oe.event_type,nc.alias,nd.status,nd.telegram_message_id,
                          nd.safe_error_code,nd.created_at,nd.updated_at
                   FROM notification_deliveries nd
                   JOIN notification_channels nc ON nc.id=nd.channel_id
                   JOIN outbox_events oe ON oe.event_id=nd.event_id
                   WHERE nc.owner_user_id=%s AND (%s='' OR nd.status=%s)
                   ORDER BY nd.created_at DESC,nd.id DESC LIMIT %s OFFSET %s""",
                (user_id, status, status, max(1, min(limit, 201)), max(0, offset)),
            ).fetchall()
        return [
            NotificationEvent(
                id=UUID(str(row[0])),
                event_type=str(row[1]),
                channel_alias=str(row[2]),
                status=str(row[3]),
                telegram_message_id=int(row[4]) if row[4] is not None else None,
                safe_error_code=str(row[5]) if row[5] else None,
                created_at=row[6],
                updated_at=row[7],
            )
            for row in rows
        ]

    def notification_counts(self, tenant_id: UUID, user_id: UUID, status: str = "") -> tuple[int, int]:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute("""SELECT count(*) FILTER (WHERE %s='' OR nd.status=%s),
                count(*) FILTER (WHERE nd.status IN ('PENDING','RETRY','FAILED','DEAD_LETTER'))
                FROM notification_deliveries nd JOIN notification_channels nc ON nc.id=nd.channel_id
                WHERE nc.owner_user_id=%s""", (status, status, user_id)).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _require_channel(connection: Any, user_id: UUID, channel_id: UUID) -> None:
        row = connection.execute(
            """SELECT id FROM notification_channels
               WHERE id=%s AND owner_user_id=%s AND status='ACTIVE'""",
            (channel_id, user_id),
        ).fetchone()
        if row is None:
            raise NotificationConflict("notification channel is unavailable")

    @staticmethod
    def _route_select() -> str:
        return """SELECT nr.id, nr.channel_id, nr.name, nr.status, nr.etag,
                         nr.event_types, nr.filters_json, nr.quiet_hours_json,
                         nr.rate_policy_json, nr.created_at, nr.updated_at
                  FROM notification_routes nr"""

    @staticmethod
    def _route(row: Any) -> NotificationRoute:
        filters = dict(row[6])
        quiet_hours = dict(row[7])
        rate_policy = dict(row[8])
        return NotificationRoute(
            id=row[0], channel_id=row[1], name=str(row[2]), status=str(row[3]),
            etag=int(row[4]),
            spec={
                "schema_version": 1,
                "channel_id": str(row[1]),
                "event_types": list(row[5]),
                **filters,
                **quiet_hours,
                **rate_policy,
                "enabled": str(row[3]) == "ACTIVE",
            },
            created_at=row[9], updated_at=row[10],
        )

    @staticmethod
    def _connection(row: Any) -> TelegramConnection:
        return TelegramConnection(
            id=row[0], telegram_user_id=int(row[1]), telegram_chat_id=int(row[2]),
            telegram_username=str(row[3]) if row[3] else None, status=str(row[4]),
            etag=int(row[5]), verified_at=row[6], created_at=row[7], updated_at=row[8],
        )

    @staticmethod
    def _channel(row: Any) -> NotificationChannel:
        return NotificationChannel(
            id=row[0], alias=str(row[1]), status=str(row[2]), connection_id=row[3],
            etag=int(row[4]), created_at=row[5], updated_at=row[6],
        )


def _valid_token(token: str) -> str:
    if not 40 <= len(token) <= 64 or any(character not in _TOKEN_CHARACTERS for character in token):
        raise ValueError("invalid Telegram link token")
    return token


def _alias(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 80:
        raise ValueError("channel alias must be between 1 and 80 characters")
    return cleaned


def _telegram_username(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lstrip("@")
    return cleaned[:64] or None


_TOKEN_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _route_parts(spec: Mapping[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, Any], dict[str, Any]]:
    event_types = [str(value) for value in spec["event_types"]]
    filters = {
        key: spec[key]
        for key in (
            "strategy_ids", "exchange_account_ids", "symbols", "environments", "minimum_severity",
        )
    }
    quiet_hours = {
        key: spec[key]
        for key in ("quiet_hours", "digest_mode", "bypass_quiet_rules")
    }
    rate_policy = {
        key: spec[key]
        for key in ("max_per_minute", "dedupe_seconds")
    }
    return event_types, filters, quiet_hours, rate_policy
