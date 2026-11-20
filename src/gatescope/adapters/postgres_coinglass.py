"""Tenant-scoped encrypted Coinglass configuration and test-job persistence."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation

from gatescope.adapters.coinglass import COINGLASS_ORIGIN, SAFE_API_KEY
from gatescope.adapters.postgres import PostgresPool
from gatescope.credential_crypto import (
    CredentialEnvelope,
    CredentialMaterial,
    RsaOaepCredentialDecryptor,
    RsaOaepCredentialEncryptor,
    credential_fingerprint,
)


class CoinglassConfigNotFound(LookupError):
    pass


class CoinglassConfigDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class CoinglassConfig:
    id: UUID
    origin: str
    key_fingerprint: str
    secret_version: int
    status: str
    last_safe_error_code: str | None
    capability: dict[str, Any]
    last_tested_at: datetime | None
    etag: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CoinglassTestJob:
    id: UUID
    config_id: UUID
    config_secret_version: int
    test_symbol: str
    status: str
    result: dict[str, Any]
    safe_error_code: str | None
    retry_after_seconds: int | None
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class ClaimedCoinglassTest:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    config_id: UUID
    config_secret_version: int
    test_symbol: str
    envelope: CredentialEnvelope
    encryption_context: str


class PostgresCoinglassRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def get(self, tenant_id: UUID, user_id: UUID) -> CoinglassConfig | None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                self._config_select() + " WHERE tenant_id=%s",
                (tenant_id,),
            ).fetchone()
        return self._config(row) if row is not None else None

    def save_and_enqueue(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        api_key: str,
        test_symbol: str,
        encryptor: RsaOaepCredentialEncryptor,
    ) -> tuple[CoinglassConfig, CoinglassTestJob]:
        symbol = _valid_symbol(test_symbol)
        if SAFE_API_KEY.fullmatch(api_key) is None:
            raise ValueError("Coinglass API credential has an invalid format")
        now_job_id = uuid4()
        with self.pool.transaction(tenant_id, user_id) as connection:
            existing = connection.execute(
                "SELECT id,secret_version FROM coinglass_configs WHERE tenant_id=%s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            config_id = UUID(str(existing[0])) if existing is not None else uuid4()
            secret_version = int(existing[1]) + 1 if existing is not None else 1
            context = _context(tenant_id, config_id, secret_version)
            envelope = encryptor.protect(
                CredentialMaterial(api_key=api_key, api_secret=""),
                context=context.encode("utf-8"),
            )
            values: tuple[Any, ...] = (
                COINGLASS_ORIGIN,
                credential_fingerprint("COINGLASS", api_key),
                envelope.ciphertext,
                envelope.nonce,
                envelope.wrapped_data_key,
                context,
                envelope.key_version,
                secret_version,
                user_id,
            )
            if existing is None:
                connection.execute(
                    """INSERT INTO coinglass_configs
                         (id,tenant_id,origin,key_fingerprint,secret_ciphertext,secret_nonce,
                          wrapped_data_key,encryption_context,key_version,secret_version,status,
                          last_safe_error_code,capability_json,last_tested_at,created_by,updated_by,
                          disabled_at,etag,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING_TEST',NULL,'{}'::jsonb,
                               NULL,%s,%s,NULL,1,now(),now())""",
                    (config_id, tenant_id, *values, user_id),
                )
            else:
                connection.execute(
                    """UPDATE coinglass_configs
                          SET origin=%s,key_fingerprint=%s,secret_ciphertext=%s,secret_nonce=%s,
                              wrapped_data_key=%s,encryption_context=%s,key_version=%s,
                              secret_version=%s,status='PENDING_TEST',last_safe_error_code=NULL,
                              capability_json='{}'::jsonb,last_tested_at=NULL,updated_by=%s,
                              disabled_at=NULL,etag=etag+1,updated_at=now()
                        WHERE id=%s""",
                    (*values, config_id),
                )
            connection.execute(
                """INSERT INTO coinglass_test_jobs
                     (id,tenant_id,config_id,config_secret_version,requested_by,test_symbol,status,
                      attempt_count,result_json,created_at,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',0,'{}'::jsonb,now(),now())""",
                (now_job_id, tenant_id, config_id, secret_version, user_id, symbol),
            )
            config_row = connection.execute(
                self._config_select() + " WHERE id=%s",
                (config_id,),
            ).fetchone()
            job_row = connection.execute(
                self._job_select() + " WHERE id=%s",
                (now_job_id,),
            ).fetchone()
        if config_row is None or job_row is None:
            raise RuntimeError("Coinglass configuration disappeared after save")
        return self._config(config_row), self._job(job_row)

    def enqueue_test(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        test_symbol: str,
    ) -> CoinglassTestJob:
        symbol = _valid_symbol(test_symbol)
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                """SELECT id,secret_version,status FROM coinglass_configs
                    WHERE tenant_id=%s FOR UPDATE""",
                (tenant_id,),
            ).fetchone()
            if row is None:
                raise CoinglassConfigNotFound("Coinglass configuration not found")
            if str(row[2]) == "DISABLED":
                raise CoinglassConfigDisabled("Coinglass configuration is disabled")
            config_id, secret_version = UUID(str(row[0])), int(row[1])
            active = connection.execute(
                self._job_select() + " WHERE tenant_id=%s AND config_id=%s AND config_secret_version=%s"
                " AND status IN ('QUEUED','RUNNING') ORDER BY created_at DESC LIMIT 1",
                (tenant_id, config_id, secret_version),
            ).fetchone()
            if active is not None:
                return self._job(active)
            job_id = uuid4()
            try:
                connection.execute(
                    """INSERT INTO coinglass_test_jobs
                         (id,tenant_id,config_id,config_secret_version,requested_by,test_symbol,status,
                          attempt_count,result_json,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,'QUEUED',0,'{}'::jsonb,now(),now())""",
                    (job_id, tenant_id, config_id, secret_version, user_id, symbol),
                )
            except UniqueViolation:
                # A concurrent click won the partial unique index. The outer
                # transaction will be aborted by psycopg, so surface a conflict.
                raise RuntimeError("Coinglass test was concurrently queued") from None
            created = connection.execute(
                self._job_select() + " WHERE id=%s",
                (job_id,),
            ).fetchone()
        if created is None:
            raise RuntimeError("Coinglass test job disappeared after enqueue")
        return self._job(created)

    def get_test(self, tenant_id: UUID, user_id: UUID, job_id: UUID) -> CoinglassTestJob:
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                self._job_select() + " WHERE tenant_id=%s AND id=%s",
                (tenant_id, job_id),
            ).fetchone()
        if row is None:
            raise CoinglassConfigNotFound("Coinglass test job not found")
        return self._job(row)

    def latest_test(self, tenant_id: UUID, user_id: UUID) -> CoinglassTestJob | None:
        """Only return the current credential version's sanitized diagnostic record."""
        with self.pool.transaction(tenant_id, user_id) as connection:
            row = connection.execute(
                self._job_select()
                + """ WHERE tenant_id=%s
                    AND (config_id,config_secret_version) IN
                        (SELECT id,secret_version FROM coinglass_configs WHERE tenant_id=%s)
                    ORDER BY created_at DESC,id DESC LIMIT 1""",
                (tenant_id, tenant_id),
            ).fetchone()
        return self._job(row) if row else None

    def disable(self, tenant_id: UUID, user_id: UUID) -> None:
        with self.pool.transaction(tenant_id, user_id) as connection:
            changed = connection.execute(
                """UPDATE coinglass_configs
                      SET status='DISABLED',disabled_at=now(),updated_by=%s,
                          etag=etag+1,updated_at=now()
                    WHERE tenant_id=%s AND status<>'DISABLED' RETURNING id""",
                (user_id, tenant_id),
            ).fetchone()
        if changed is None:
            raise CoinglassConfigNotFound("active Coinglass configuration not found")

    def claim_test(
        self,
        worker_id: str,
        decryptor: RsaOaepCredentialDecryptor,
    ) -> tuple[ClaimedCoinglassTest, str] | None:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", worker_id) is None:
            raise ValueError("Coinglass worker ID is invalid")
        with self.pool.public_transaction() as connection:
            claimed = connection.execute(
                "SELECT * FROM gatescope_security.claim_coinglass_test_job(%s)",
                (worker_id,),
            ).fetchone()
        if claimed is None:
            return None
        job_id, tenant_id = UUID(str(claimed[0])), UUID(str(claimed[1]))
        with self.pool.transaction(tenant_id, tenant_id) as connection:
            row = connection.execute(
                """SELECT job.id,job.config_id,job.config_secret_version,job.test_symbol,
                          config.secret_ciphertext,config.secret_nonce,config.wrapped_data_key,
                          config.encryption_context,config.key_version,config.secret_version,
                          config.status,config.created_by
                     FROM coinglass_test_jobs job
                     JOIN coinglass_configs config ON config.id=job.config_id
                    WHERE job.id=%s AND job.status='RUNNING'""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise CoinglassConfigNotFound("claimed Coinglass test disappeared")
        expected_version = int(row[2])
        if str(row[10]) == "DISABLED" or int(row[9]) != expected_version:
            self.complete_test(
                tenant_id,
                job_id,
                expected_version=expected_version,
                success=False,
                result={},
                safe_error_code="COINGLASS_CONFIG_SUPERSEDED",
            )
            return None
        context = str(row[7])
        expected_context = _context(tenant_id, UUID(str(row[1])), expected_version)
        if not hmac.compare_digest(context, expected_context):
            raise ValueError("Coinglass secret context mismatch")
        envelope = CredentialEnvelope(bytes(row[4]), bytes(row[5]), bytes(row[6]), str(row[8]))
        material = decryptor.reveal(envelope, context=context.encode("utf-8"))
        if SAFE_API_KEY.fullmatch(material.api_key) is None:
            raise ValueError("Coinglass decrypted credential is invalid")
        return (
            ClaimedCoinglassTest(
                id=job_id,
                tenant_id=tenant_id,
                owner_user_id=UUID(str(row[11])),
                config_id=UUID(str(row[1])),
                config_secret_version=expected_version,
                test_symbol=str(row[3]),
                envelope=envelope,
                encryption_context=context,
            ),
            material.api_key,
        )

    def complete_test(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        expected_version: int,
        success: bool,
        result: dict[str, Any],
        safe_error_code: str | None,
        retry_after_seconds: int | None = None,
    ) -> None:
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 128 * 1024:
            raise ValueError("Coinglass safe result exceeds storage limit")
        status = "SUCCEEDED" if success else "FAILED"
        transient = not success and _transient_failure(safe_error_code, result)
        with self.pool.transaction(tenant_id, tenant_id) as connection:
            row = connection.execute(
                """UPDATE coinglass_test_jobs
                      SET status=%s,result_json=%s::jsonb,safe_error_code=%s,
                          retry_after_seconds=%s,completed_at=now(),updated_at=now()
                    WHERE id=%s AND tenant_id=%s AND status='RUNNING'
                    RETURNING config_id""",
                (status, encoded, safe_error_code, retry_after_seconds, job_id, tenant_id),
            ).fetchone()
            if row is None:
                return
            capability = result.get("checks", {}).get("account", {}).get("metadata", {})
            if not isinstance(capability, dict):
                capability = {}
            connection.execute(
                """UPDATE coinglass_configs
                      SET status=CASE WHEN %s THEN status ELSE %s END,last_safe_error_code=%s,
                          capability_json=CASE WHEN %s THEN capability_json ELSE %s::jsonb END,
                          last_tested_at=now(),etag=etag+1,updated_at=now()
                    WHERE id=%s AND tenant_id=%s AND secret_version=%s AND status<>'DISABLED'""",
                (
                    transient,
                    "ACTIVE" if success else "VALIDATION_FAILED",
                    safe_error_code,
                    transient,
                    json.dumps(capability, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                    row[0],
                    tenant_id,
                    expected_version,
                ),
            )

    @staticmethod
    def _config_select() -> str:
        return """SELECT id,origin,key_fingerprint,secret_version,status,last_safe_error_code,
                         capability_json,last_tested_at,etag,created_at,updated_at
                    FROM coinglass_configs"""

    @staticmethod
    def _job_select() -> str:
        return """SELECT id,config_id,config_secret_version,test_symbol,status,result_json,
                         safe_error_code,retry_after_seconds,attempt_count,created_at,started_at,
                         completed_at,updated_at
                    FROM coinglass_test_jobs"""

    @staticmethod
    def _config(row: Any) -> CoinglassConfig:
        return CoinglassConfig(
            id=UUID(str(row[0])),
            origin=str(row[1]),
            key_fingerprint=str(row[2]),
            secret_version=int(row[3]),
            status=str(row[4]),
            last_safe_error_code=str(row[5]) if row[5] else None,
            capability=dict(row[6]) if isinstance(row[6], dict) else {},
            last_tested_at=row[7],
            etag=int(row[8]),
            created_at=row[9],
            updated_at=row[10],
        )

    @staticmethod
    def _job(row: Any) -> CoinglassTestJob:
        return CoinglassTestJob(
            id=UUID(str(row[0])),
            config_id=UUID(str(row[1])),
            config_secret_version=int(row[2]),
            test_symbol=str(row[3]),
            status=str(row[4]),
            result=dict(row[5]) if isinstance(row[5], dict) else {},
            safe_error_code=str(row[6]) if row[6] else None,
            retry_after_seconds=int(row[7]) if row[7] is not None else None,
            attempt_count=int(row[8]),
            created_at=row[9],
            started_at=row[10],
            completed_at=row[11],
            updated_at=row[12],
        )


def _context(tenant_id: UUID, config_id: UUID, secret_version: int) -> str:
    return f"gatescope:v1:tenant:{tenant_id}:coinglass-config:{config_id}:secret:{secret_version}"


def _transient_failure(code: str | None, result: dict[str, Any]) -> bool:
    """Network/quota failures do not revoke already validated credentials.

    Rotation resets status and capability before testing, so preserving a new
    PENDING_TEST is fail-closed. Explicit HTTP auth errors always invalidate,
    including when the adapter classifies them as provider unavailability.
    """
    if code not in {"PROVIDER_RATE_LIMITED", "PROVIDER_UNAVAILABLE"}:
        return False
    checks = result.get("checks", {})
    if not isinstance(checks, dict):
        return False
    for check in checks.values():
        if not isinstance(check, dict):
            return False
        status = check.get("http_status")
        if status is not None and status != 200 and status not in {408, 425, 429}:
            if not isinstance(status, int) or isinstance(status, bool) or not 500 <= status <= 599:
                return False
        error = check.get("safe_error_code")
        if error and error not in {
            "PROVIDER_RATE_LIMITED",
            "PROVIDER_UNAVAILABLE",
            "DEFERRED_AFTER_PROVIDER_ERROR",
        }:
            return False
    return True


def _valid_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if re.fullmatch(r"[A-Z0-9]{1,20}", normalized) is None:
        raise ValueError("Coinglass test symbol is invalid")
    return normalized
