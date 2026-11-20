"""Encrypted tenant Telegram Bot configuration and webhook lookup."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from gatescope.adapters.postgres import PostgresPool
from gatescope.credential_crypto import (
    CredentialEnvelope,
    CredentialMaterial,
    RsaOaepCredentialDecryptor,
    RsaOaepCredentialEncryptor,
    credential_fingerprint,
)


class TelegramConfigNotFound(LookupError):
    pass


@dataclass(frozen=True)
class TelegramBotConfig:
    id: UUID
    bot_username: str
    token_fingerprint: str
    allowed_user_ids: tuple[int, ...]
    status: str
    last_safe_error_code: str | None
    secret_version: int
    validated_at: datetime | None
    webhook_configured_at: datetime | None
    etag: int
    created_at: datetime
    updated_at: datetime

    @property
    def configured(self) -> bool:
        return self.status == "ACTIVE"


@dataclass(frozen=True)
class TelegramWebhookConfig:
    tenant_id: UUID
    allowed_user_ids: frozenset[int]
    webhook_secret_hash: str

    def accepts_secret(self, supplied: str) -> bool:
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        return hmac.compare_digest(self.webhook_secret_hash, digest)


class PostgresTelegramConfigRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def get(self, tenant_id: UUID, user_id: UUID) -> TelegramBotConfig | None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(self._select() + " WHERE tenant_id=%s", (tenant_id,)).fetchone()
        return self._config(row) if row is not None else None

    def save(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        bot_username: str,
        bot_token: str,
        allowed_user_ids: tuple[int, ...],
        webhook_id: str,
        webhook_secret: str,
        encryptor: RsaOaepCredentialEncryptor,
        status: str,
        last_safe_error_code: str | None,
    ) -> TelegramBotConfig:
        _validate(bot_username, bot_token, allowed_user_ids, webhook_id, webhook_secret, status)
        with self.pool.transaction(tenant_id, user_id) as connection:
            existing = connection.execute(
                "SELECT id,secret_version FROM telegram_bot_configs WHERE tenant_id=%s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            config_id = existing[0] if existing is not None else uuid4()
            secret_version = int(existing[1]) + 1 if existing is not None else 1
            context = _context(tenant_id, config_id, secret_version)
            envelope = encryptor.protect(
                CredentialMaterial(api_key=bot_token, api_secret=webhook_secret),
                context=context.encode("utf-8"),
            )
            values: tuple[Any, ...] = (
                bot_username,
                credential_fingerprint("TELEGRAM", bot_token),
                list(allowed_user_ids),
                webhook_id,
                hashlib.sha256(webhook_secret.encode("utf-8")).hexdigest(),
                envelope.ciphertext,
                envelope.nonce,
                envelope.wrapped_data_key,
                context,
                envelope.key_version,
                secret_version,
                status,
                last_safe_error_code,
                user_id,
            )
            if existing is None:
                connection.execute(
                    """INSERT INTO telegram_bot_configs
                         (id,tenant_id,bot_username,token_fingerprint,allowed_user_ids,
                          webhook_id,webhook_secret_hash,secret_ciphertext,secret_nonce,
                          wrapped_data_key,encryption_context,key_version,secret_version,status,
                          last_safe_error_code,created_by,updated_by,validated_at,
                          webhook_configured_at,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               now(),CASE WHEN %s='ACTIVE' THEN now() END,now(),now())""",
                    (config_id, tenant_id, *values, user_id, status),
                )
            else:
                connection.execute(
                    """UPDATE telegram_bot_configs
                          SET bot_username=%s,token_fingerprint=%s,allowed_user_ids=%s,
                              webhook_id=%s,webhook_secret_hash=%s,secret_ciphertext=%s,
                              secret_nonce=%s,wrapped_data_key=%s,encryption_context=%s,
                              key_version=%s,secret_version=%s,status=%s,last_safe_error_code=%s,
                              updated_by=%s,validated_at=now(),
                              webhook_configured_at=CASE WHEN %s='ACTIVE' THEN now() ELSE NULL END,
                              disabled_at=NULL,etag=etag+1,updated_at=now()
                        WHERE id=%s""",
                    (*values, status, config_id),
                )
            connection.execute(
                "SELECT gatescope_security.replace_telegram_bot_config_locator(%s,%s)",
                (hashlib.sha256(webhook_id.encode("ascii")).hexdigest(), tenant_id),
            )
            row = connection.execute(self._select() + " WHERE id=%s", (config_id,)).fetchone()
        if row is None:
            raise RuntimeError("Telegram configuration disappeared after save")
        return self._config(row)

    def disable(self, tenant_id: UUID, user_id: UUID) -> None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            changed = connection.execute(
                """UPDATE telegram_bot_configs SET status='DISABLED',disabled_at=now(),
                          updated_by=%s,etag=etag+1,updated_at=now()
                    WHERE tenant_id=%s AND status<>'DISABLED' RETURNING id""",
                (user_id, tenant_id),
            ).fetchone()
            if changed is None:
                raise TelegramConfigNotFound("active Telegram configuration not found")
            connection.execute(
                "SELECT gatescope_security.remove_telegram_bot_config_locator(%s)", (tenant_id,),
            )

    def resolve_webhook(self, opaque_webhook_id: str) -> TelegramWebhookConfig | None:
        digest = hashlib.sha256(opaque_webhook_id.encode("utf-8")).hexdigest()
        with self.pool.public_transaction() as connection:
            locator = connection.execute(
                "SELECT gatescope_security.resolve_telegram_bot_config_tenant(%s)", (digest,),
            ).fetchone()
        if locator is None or locator[0] is None:
            return None
        tenant_id = UUID(str(locator[0]))
        with self.pool.transaction(tenant_id, tenant_id) as connection:
            row = connection.execute(
                """SELECT allowed_user_ids,webhook_secret_hash FROM telegram_bot_configs
                   WHERE tenant_id=%s AND status='ACTIVE' AND webhook_id=%s""",
                (tenant_id, opaque_webhook_id),
            ).fetchone()
        if row is None:
            return None
        return TelegramWebhookConfig(
            tenant_id=tenant_id,
            allowed_user_ids=frozenset(int(value) for value in row[0]),
            webhook_secret_hash=str(row[1]),
        )

    def delivery_token(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        decryptor: RsaOaepCredentialDecryptor,
    ) -> str:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            row = connection.execute(
                """SELECT id,secret_ciphertext,secret_nonce,wrapped_data_key,
                          encryption_context,key_version,secret_version
                   FROM telegram_bot_configs WHERE tenant_id=%s AND status='ACTIVE'""",
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise TelegramConfigNotFound("active Telegram configuration not found")
        expected_context = _context(tenant_id, UUID(str(row[0])), int(row[6]))
        if not hmac.compare_digest(str(row[4]), expected_context):
            raise ValueError("Telegram secret context mismatch")
        envelope = CredentialEnvelope(bytes(row[1]), bytes(row[2]), bytes(row[3]), str(row[5]))
        return decryptor.reveal(envelope, context=expected_context.encode("utf-8")).api_key

    @staticmethod
    def _select() -> str:
        return """SELECT id,bot_username,token_fingerprint,allowed_user_ids,status,
                         last_safe_error_code,secret_version,validated_at,
                         webhook_configured_at,etag,created_at,updated_at
                  FROM telegram_bot_configs"""

    @staticmethod
    def _config(row: Any) -> TelegramBotConfig:
        return TelegramBotConfig(
            id=UUID(str(row[0])),
            bot_username=str(row[1]),
            token_fingerprint=str(row[2]),
            allowed_user_ids=tuple(int(value) for value in row[3]),
            status=str(row[4]),
            last_safe_error_code=str(row[5]) if row[5] else None,
            secret_version=int(row[6]),
            validated_at=row[7],
            webhook_configured_at=row[8],
            etag=int(row[9]),
            created_at=row[10],
            updated_at=row[11],
        )


def _context(tenant_id: UUID, config_id: UUID, secret_version: int) -> str:
    return f"gatescope:v1:tenant:{tenant_id}:telegram-config:{config_id}:secret:{secret_version}"


def _validate(
    username: str,
    token: str,
    allowed_ids: tuple[int, ...],
    webhook_id: str,
    webhook_secret: str,
    status: str,
) -> None:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", username) is None:
        raise ValueError("Telegram Bot username is invalid")
    if re.fullmatch(r"[0-9]{6,20}:[A-Za-z0-9_-]{30,128}", token) is None:
        raise ValueError("Telegram Bot token is invalid")
    if len(allowed_ids) > 100 or len(set(allowed_ids)) != len(allowed_ids):
        raise ValueError("Telegram allowlist must contain 0-100 unique IDs")
    if any(value <= 0 or value >= 2**63 for value in allowed_ids):
        raise ValueError("Telegram allowlist contains an invalid ID")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", webhook_id) is None:
        raise ValueError("Telegram webhook ID is invalid")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", webhook_secret) is None:
        raise ValueError("Telegram webhook secret is invalid")
    if status not in {"ACTIVE", "NEEDS_HTTPS", "VALIDATION_FAILED"}:
        raise ValueError("Telegram configuration status is invalid")
