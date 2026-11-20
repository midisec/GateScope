"""RLS-bound exchange account metadata and encrypt-only credential admission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from gatescope.adapters.postgres import PostgresPool
from gatescope.credential_crypto import (
    CredentialEnvelope,
    CredentialMaterial,
    RsaOaepCredentialEncryptor,
    credential_fingerprint,
)


class ExchangeAccountNotFound(LookupError):
    pass


class ExchangeAccountConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ExchangeAccount:
    id: UUID
    owner_user_id: UUID
    alias: str
    venue: str
    environment: str
    market_type: str
    settle_asset: str
    status: str
    trading_ready: bool
    credential_version: int
    credential_fingerprint: str | None
    capabilities: dict[str, Any]
    warning_codes: tuple[str, ...]
    last_safe_error_code: str | None
    testnet_armed_at: datetime | None
    testnet_armed_by: UUID | None
    execution_freeze_code: str | None
    validation_requested_at: datetime
    validated_at: datetime | None
    etag: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CredentialValidationCommand:
    id: UUID
    tenant_id: UUID
    exchange_account_id: UUID
    credential_id: UUID
    environment: str
    envelope: CredentialEnvelope
    encryption_context: str
    attempt_count: int


class PostgresExchangeAccountRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def list_accounts(self, tenant_id: UUID, subject_id: UUID) -> list[ExchangeAccount]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    """SELECT id,owner_user_id,alias,venue,environment,market_type,settle_asset,
                              status,trading_ready,credential_version,credential_fingerprint,
                              capability_json,warning_codes,last_safe_error_code,testnet_armed_at,
                              testnet_armed_by,execution_freeze_code,
                              validation_requested_at,validated_at,etag,created_at,updated_at
                       FROM exchange_accounts
                       ORDER BY created_at,id""",
                ).fetchall()
        return [_account(row) for row in rows]

    def get_account(self, tenant_id: UUID, subject_id: UUID, account_id: UUID) -> ExchangeAccount:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    """SELECT id,owner_user_id,alias,venue,environment,market_type,settle_asset,
                              status,trading_ready,credential_version,credential_fingerprint,
                              capability_json,warning_codes,last_safe_error_code,testnet_armed_at,
                              testnet_armed_by,execution_freeze_code,
                              validation_requested_at,validated_at,etag,created_at,updated_at
                       FROM exchange_accounts WHERE id=%s""",
                    (account_id,),
                ).fetchone()
        if row is None:
            raise ExchangeAccountNotFound("exchange account not found")
        return _account(row)

    def create_account(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        alias: str,
        material: CredentialMaterial,
        encryptor: RsaOaepCredentialEncryptor,
        no_withdrawal_attested: bool,
    ) -> tuple[ExchangeAccount, UUID]:
        cleaned_alias = alias.strip()
        if not cleaned_alias or not no_withdrawal_attested:
            raise ValueError("exchange account admission policy was not accepted")
        account_id = uuid4()
        credential_id = uuid4()
        command_id = uuid4()
        now = datetime.now(timezone.utc)
        context = _encryption_context(tenant_id, account_id, credential_id, 1)
        envelope = encryptor.protect(material, context=context.encode("ascii"))
        fingerprint = credential_fingerprint("GATE", material.api_key)
        try:
            with self.pool.transaction(tenant_id, owner_user_id) as connection:
                connection.execute(
                    """INSERT INTO exchange_accounts
                         (id,tenant_id,owner_user_id,venue,alias,environment,market_type,settle_asset,
                          status,trading_ready,credential_version,pending_credential_id,
                          validation_requested_at,etag,created_at,updated_at)
                       VALUES (%s,%s,%s,'GATE',%s,'TESTNET','FUTURES_USDT','USDT',
                               'PENDING_VALIDATION',false,1,NULL,%s,1,%s,%s)""",
                    (account_id, tenant_id, owner_user_id, cleaned_alias, now, now, now),
                )
                connection.execute(
                    """INSERT INTO secret_credentials
                         (id,tenant_id,exchange_account_id,version,ciphertext,nonce,wrapped_data_key,
                          encryption_context,key_version,fingerprint,no_withdrawal_attested,status,
                          created_by,created_at)
                       VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,true,'PENDING_VALIDATION',%s,%s)""",
                    (
                        credential_id, tenant_id, account_id, envelope.ciphertext, envelope.nonce,
                        envelope.wrapped_data_key, context, envelope.key_version, fingerprint,
                        owner_user_id, now,
                    ),
                )
                connection.execute(
                    "UPDATE exchange_accounts SET pending_credential_id=%s WHERE id=%s",
                    (credential_id, account_id),
                )
                self._insert_command(
                    connection, command_id, tenant_id, account_id, credential_id, owner_user_id, now,
                )
                self._outbox(
                    connection, tenant_id, account_id, "exchange_account.validation_requested.v1",
                    {"account_id": str(account_id), "venue": "GATE", "environment": "TESTNET", "credential_version": 1},
                    now,
                )
        except UniqueViolation as exc:
            raise ExchangeAccountConflict("exchange account alias or credential already exists") from exc
        return self.get_account(tenant_id, owner_user_id, account_id), command_id

    def rotate_credential(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        account_id: UUID,
        *,
        expected_etag: int,
        material: CredentialMaterial,
        encryptor: RsaOaepCredentialEncryptor,
        no_withdrawal_attested: bool,
    ) -> tuple[ExchangeAccount, UUID]:
        if not no_withdrawal_attested:
            raise ValueError("exchange account admission policy was not accepted")
        credential_id = uuid4()
        command_id = uuid4()
        now = datetime.now(timezone.utc)
        try:
            with self.pool.transaction(tenant_id, owner_user_id) as connection:
                row = connection.execute(
                    """SELECT credential_version,status,etag,pending_credential_id
                       FROM exchange_accounts WHERE id=%s FOR UPDATE""",
                    (account_id,),
                ).fetchone()
                if row is None:
                    raise ExchangeAccountNotFound("exchange account not found")
                if int(row[2]) != expected_etag:
                    raise ExchangeAccountConflict("exchange account version changed")
                self._require_no_unresolved_execution(connection, account_id)
                if str(row[1]) in {"DISABLED", "TESTNET_BUSY"}:
                    raise ExchangeAccountConflict("exchange account is disabled")
                if row[3] is not None:
                    raise ExchangeAccountConflict("credential validation is already pending")
                version = int(row[0]) + 1
                context = _encryption_context(tenant_id, account_id, credential_id, version)
                envelope = encryptor.protect(material, context=context.encode("ascii"))
                fingerprint = credential_fingerprint("GATE", material.api_key)
                connection.execute(
                    """INSERT INTO secret_credentials
                         (id,tenant_id,exchange_account_id,version,ciphertext,nonce,wrapped_data_key,
                          encryption_context,key_version,fingerprint,no_withdrawal_attested,status,
                          created_by,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,'PENDING_VALIDATION',%s,%s)""",
                    (
                        credential_id, tenant_id, account_id, version, envelope.ciphertext,
                        envelope.nonce, envelope.wrapped_data_key, context, envelope.key_version,
                        fingerprint, owner_user_id, now,
                    ),
                )
                updated = connection.execute(
                    """UPDATE exchange_accounts
                       SET credential_version=%s,pending_credential_id=%s,status='PENDING_VALIDATION',
                           trading_ready=false,validation_requested_at=%s,last_safe_error_code=NULL,
                           warning_codes='{}',testnet_armed_at=NULL,testnet_armed_by=NULL,
                           execution_freeze_code=NULL,etag=etag+1,updated_at=%s
                       WHERE id=%s AND etag=%s""",
                    (version, credential_id, now, now, account_id, expected_etag),
                )
                if updated.rowcount != 1:
                    raise ExchangeAccountConflict("exchange account version changed")
                self._insert_command(
                    connection, command_id, tenant_id, account_id, credential_id, owner_user_id, now,
                )
                self._outbox(
                    connection, tenant_id, account_id, "exchange_account.credential_rotation_requested.v1",
                    {"account_id": str(account_id), "venue": "GATE", "environment": "TESTNET", "credential_version": version},
                    now,
                )
        except UniqueViolation as exc:
            raise ExchangeAccountConflict("credential is already admitted or validation is pending") from exc
        return self.get_account(tenant_id, owner_user_id, account_id), command_id

    def request_validation(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        account_id: UUID,
        *,
        expected_etag: int,
    ) -> tuple[ExchangeAccount, UUID]:
        command_id = uuid4()
        now = datetime.now(timezone.utc)
        try:
            with self.pool.transaction(tenant_id, owner_user_id) as connection:
                row = connection.execute(
                    """SELECT active_credential_id,pending_credential_id,status,etag
                       FROM exchange_accounts WHERE id=%s FOR UPDATE""",
                    (account_id,),
                ).fetchone()
                if row is None:
                    raise ExchangeAccountNotFound("exchange account not found")
                if int(row[3]) != expected_etag:
                    raise ExchangeAccountConflict("exchange account version changed")
                self._require_no_unresolved_execution(connection, account_id)
                if str(row[2]) in {"DISABLED", "TESTNET_BUSY"}:
                    raise ExchangeAccountConflict("exchange account is disabled")
                credential_id = row[1] or row[0]
                if credential_id is None:
                    raise ExchangeAccountConflict("exchange account has no credential")
                self._insert_command(
                    connection, command_id, tenant_id, account_id, UUID(str(credential_id)), owner_user_id, now,
                )
                connection.execute(
                    """UPDATE exchange_accounts SET status='PENDING_VALIDATION',trading_ready=false,
                              testnet_armed_at=NULL,testnet_armed_by=NULL,execution_freeze_code=NULL,
                              validation_requested_at=%s,last_safe_error_code=NULL,etag=etag+1,updated_at=%s
                       WHERE id=%s""",
                    (now, now, account_id),
                )
                self._outbox(
                    connection, tenant_id, account_id, "exchange_account.validation_requested.v1",
                    {"account_id": str(account_id), "venue": "GATE", "environment": "TESTNET"}, now,
                )
        except UniqueViolation as exc:
            raise ExchangeAccountConflict("credential validation is already pending") from exc
        return self.get_account(tenant_id, owner_user_id, account_id), command_id

    def disable_account(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        account_id: UUID,
        *,
        expected_etag: int,
    ) -> ExchangeAccount:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, owner_user_id) as connection:
            connection.execute("SELECT id FROM exchange_accounts WHERE id=%s FOR UPDATE", (account_id,)).fetchone()
            self._require_no_unresolved_execution(connection, account_id)
            updated = connection.execute(
                """UPDATE exchange_accounts
                   SET status='DISABLED',trading_ready=false,pending_credential_id=NULL,
                       testnet_armed_at=NULL,testnet_armed_by=NULL,execution_freeze_code=NULL,
                       etag=etag+1,updated_at=%s
                   WHERE id=%s AND etag=%s AND status NOT IN ('DISABLED','TESTNET_BUSY')""",
                (now, account_id, expected_etag),
            )
            if updated.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM exchange_accounts WHERE id=%s", (account_id,),
                ).fetchone()
                if exists is None:
                    raise ExchangeAccountNotFound("exchange account not found")
                raise ExchangeAccountConflict("exchange account version changed or is disabled")
            connection.execute(
                """UPDATE credential_validation_commands
                   SET state='FAILED',safe_result_code='ACCOUNT_DISABLED',completed_at=%s,updated_at=%s
                   WHERE exchange_account_id=%s AND state IN ('PENDING','PROCESSING')""",
                (now, now, account_id),
            )
            self._outbox(
                connection, tenant_id, account_id, "exchange_account.disabled.v1",
                {"account_id": str(account_id), "remote_key_revoked": False}, now,
            )
        return self.get_account(tenant_id, owner_user_id, account_id)

    def arm_testnet_execution(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        account_id: UUID,
        *,
        expected_etag: int,
        ip_whitelist_attested: bool,
        no_withdrawal_attested: bool,
        exclusive_account_attested: bool,
    ) -> ExchangeAccount:
        """Explicitly arm a read-validated TESTNET account; never permits LIVE."""

        if not all((ip_whitelist_attested, no_withdrawal_attested, exclusive_account_attested)):
            raise ValueError("all TESTNET execution safety attestations are required")
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, owner_user_id) as connection:
            connection.execute("SELECT id FROM exchange_accounts WHERE id=%s FOR UPDATE", (account_id,)).fetchone()
            self._require_no_unresolved_execution(connection, account_id)
            updated = connection.execute(
                """UPDATE exchange_accounts
                   SET status='TESTNET_READY',trading_ready=true,testnet_armed_at=%s,
                       testnet_armed_by=%s,execution_freeze_code=NULL,etag=etag+1,updated_at=%s
                   WHERE id=%s AND owner_user_id=%s AND etag=%s AND status='READ_VALIDATED'
                     AND active_credential_id IS NOT NULL
                     AND coalesce((capability_json->>'account_read')::boolean,false)
                     AND coalesce((capability_json->>'futures_usdt_read')::boolean,false)
                     AND coalesce((capability_json->>'ip_whitelist_configured')::boolean,false)""",
                (now, owner_user_id, now, account_id, owner_user_id, expected_etag),
            )
            if updated.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM exchange_accounts WHERE id=%s", (account_id,),
                ).fetchone()
                if exists is None:
                    raise ExchangeAccountNotFound("exchange account not found")
                raise ExchangeAccountConflict("exchange account is not eligible for TESTNET arming")
            self._outbox(
                connection,
                tenant_id,
                account_id,
                "exchange_account.testnet_armed.v1",
                {"account_id": str(account_id), "environment": "TESTNET", "live_eligible": False},
                now,
            )
        return self.get_account(tenant_id, owner_user_id, account_id)

    def disarm_testnet_execution(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        account_id: UUID,
        *,
        expected_etag: int,
    ) -> ExchangeAccount:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(tenant_id, owner_user_id) as connection:
            connection.execute("SELECT id FROM exchange_accounts WHERE id=%s FOR UPDATE", (account_id,)).fetchone()
            self._require_no_unresolved_execution(connection, account_id)
            updated = connection.execute(
                """UPDATE exchange_accounts
                   SET status='READ_VALIDATED',trading_ready=false,testnet_armed_at=NULL,
                       testnet_armed_by=NULL,execution_freeze_code=NULL,etag=etag+1,updated_at=%s
                   WHERE id=%s AND owner_user_id=%s AND etag=%s AND status='TESTNET_READY'""",
                (now, account_id, owner_user_id, expected_etag),
            )
            if updated.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM exchange_accounts WHERE id=%s", (account_id,),
                ).fetchone()
                if exists is None:
                    raise ExchangeAccountNotFound("exchange account not found")
                raise ExchangeAccountConflict("only an idle TESTNET account can be disarmed")
            self._outbox(
                connection,
                tenant_id,
                account_id,
                "exchange_account.testnet_disarmed.v1",
                {"account_id": str(account_id), "environment": "TESTNET"},
                now,
            )
        return self.get_account(tenant_id, owner_user_id, account_id)

    def due_tenants(self, *, limit: int = 20) -> list[UUID]:
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM gatescope_security.credential_validation_due_tenants(%s)",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [UUID(str(row[0])) for row in rows]

    def claim_next(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> CredentialValidationCommand | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(10, min(lease_seconds, 120)))
        with self.pool.transaction(tenant_id, worker_subject_id) as connection:
            row = connection.execute(
                """SELECT command.id,command.tenant_id,command.exchange_account_id,
                          command.credential_id,account.environment,credential.ciphertext,
                          credential.nonce,credential.wrapped_data_key,credential.encryption_context,
                          credential.key_version,command.attempt_count
                   FROM credential_validation_commands command
                   JOIN exchange_accounts account ON account.id=command.exchange_account_id
                   JOIN secret_credentials credential ON credential.id=command.credential_id
                   WHERE account.status NOT IN ('DISABLED','TESTNET_BUSY','EXECUTION_FROZEN') AND (
                       (command.state='PENDING' AND command.next_attempt_at <= %s)
                       OR (command.state='PROCESSING' AND command.lease_expires_at <= %s)
                   )
                   ORDER BY command.next_attempt_at,command.created_at
                   FOR UPDATE OF command SKIP LOCKED LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            attempt = int(row[10]) + 1
            connection.execute(
                """UPDATE credential_validation_commands
                   SET state='PROCESSING',attempt_count=%s,lease_owner=%s,lease_expires_at=%s,
                       started_at=coalesce(started_at,%s),updated_at=%s
                   WHERE id=%s""",
                (attempt, worker_id, lease_until, now, now, row[0]),
            )
            return CredentialValidationCommand(
                id=UUID(str(row[0])), tenant_id=UUID(str(row[1])),
                exchange_account_id=UUID(str(row[2])), credential_id=UUID(str(row[3])),
                environment=str(row[4]),
                envelope=CredentialEnvelope(bytes(row[5]), bytes(row[6]), bytes(row[7]), str(row[9])),
                encryption_context=str(row[8]), attempt_count=attempt,
            )

    def complete_success(
        self,
        command: CredentialValidationCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        capabilities: dict[str, Any],
        warning_codes: tuple[str, ...],
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            connection.execute("SELECT id FROM exchange_accounts WHERE id=%s FOR UPDATE",
                               (command.exchange_account_id,)).fetchone()
            locked = connection.execute(
                """SELECT state,lease_owner FROM credential_validation_commands
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if locked is None or str(locked[0]) != "PROCESSING" or str(locked[1]) != worker_id:
                raise ExchangeAccountConflict("credential validation lease was lost")
            account = connection.execute(
                "SELECT active_credential_id,status FROM exchange_accounts WHERE id=%s FOR UPDATE",
                (command.exchange_account_id,),
            ).fetchone()
            if account is None or str(account[1]) == "DISABLED":
                raise ExchangeAccountConflict("exchange account is unavailable")
            self._require_no_unresolved_execution(connection, command.exchange_account_id)
            if account[0] is not None and UUID(str(account[0])) != command.credential_id:
                connection.execute(
                    """UPDATE secret_credentials SET status='SUPERSEDED'
                       WHERE id=%s AND status='READ_VALIDATED'""",
                    (account[0],),
                )
            credential = connection.execute(
                """UPDATE secret_credentials
                   SET status='READ_VALIDATED',validated_at=%s,rejected_at=NULL
                   WHERE id=%s AND status IN ('PENDING_VALIDATION','READ_VALIDATED')""",
                (now, command.credential_id),
            )
            if credential.rowcount != 1:
                raise ExchangeAccountConflict("credential is no longer validatable")
            fingerprint_row = connection.execute(
                "SELECT fingerprint FROM secret_credentials WHERE id=%s", (command.credential_id,),
            ).fetchone()
            if fingerprint_row is None:
                raise ExchangeAccountConflict("credential disappeared during validation")
            connection.execute(
                """UPDATE exchange_accounts
                   SET active_credential_id=%s,pending_credential_id=NULL,credential_fingerprint=%s,
                       status='READ_VALIDATED',trading_ready=false,capability_json=%s::jsonb,
                       warning_codes=%s,last_safe_error_code=NULL,validated_at=%s,
                       testnet_armed_at=NULL,testnet_armed_by=NULL,execution_freeze_code=NULL,
                       etag=etag+1,updated_at=%s
                   WHERE id=%s""",
                (
                    command.credential_id, str(fingerprint_row[0]), json.dumps(capabilities),
                    list(warning_codes), now, now, command.exchange_account_id,
                ),
            )
            connection.execute(
                """UPDATE credential_validation_commands
                   SET state='SUCCEEDED',safe_result_code='READ_VALIDATED',completed_at=%s,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE id=%s""",
                (now, now, command.id),
            )
            connection.execute(
                """INSERT INTO credential_validation_results
                     (id,tenant_id,exchange_account_id,credential_id,command_id,success,
                      capability_json,warning_codes,safe_error_code,checked_at,created_at)
                   VALUES (%s,%s,%s,%s,%s,true,%s::jsonb,%s,NULL,%s,%s)""",
                (
                    uuid4(), command.tenant_id, command.exchange_account_id, command.credential_id,
                    command.id, json.dumps(capabilities), list(warning_codes), now, now,
                ),
            )
            self._outbox(
                connection, command.tenant_id, command.exchange_account_id,
                "exchange_account.read_validated.v1",
                {"account_id": str(command.exchange_account_id), "trading_ready": False, "warning_codes": list(warning_codes)},
                now,
            )

    def complete_failure(
        self,
        command: CredentialValidationCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        safe_error_code: str,
        retry_after_seconds: int | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            connection.execute("SELECT id FROM exchange_accounts WHERE id=%s FOR UPDATE",
                               (command.exchange_account_id,)).fetchone()
            locked = connection.execute(
                """SELECT state,lease_owner,attempt_count FROM credential_validation_commands
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if locked is None or str(locked[0]) != "PROCESSING" or str(locked[1]) != worker_id:
                raise ExchangeAccountConflict("credential validation lease was lost")
            self._require_no_unresolved_execution(connection, command.exchange_account_id)
            retryable = safe_error_code in {
                "GATE_RATE_LIMITED", "GATE_NETWORK_UNAVAILABLE", "GATE_SERVICE_UNAVAILABLE",
            }
            attempts = int(locked[2])
            if retryable and attempts < 5:
                delay = max(1, min(retry_after_seconds or 2**attempts, 300))
                connection.execute(
                    """UPDATE credential_validation_commands
                       SET state='PENDING',next_attempt_at=%s,safe_result_code=%s,
                           lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE id=%s""",
                    (now + timedelta(seconds=delay), safe_error_code, now, command.id),
                )
                return True
            connection.execute(
                """UPDATE credential_validation_commands
                   SET state='FAILED',safe_result_code=%s,completed_at=%s,
                       lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE id=%s""",
                (safe_error_code, now, now, command.id),
            )
            connection.execute(
                """UPDATE secret_credentials SET status='REJECTED',rejected_at=%s
                   WHERE id=%s AND status IN ('PENDING_VALIDATION','READ_VALIDATED')""",
                (now, command.credential_id),
            )
            connection.execute(
                """UPDATE exchange_accounts
                   SET status='VALIDATION_FAILED',trading_ready=false,active_credential_id=NULL,
                       pending_credential_id=NULL,capability_json='{}',warning_codes='{}',
                       testnet_armed_at=NULL,testnet_armed_by=NULL,execution_freeze_code=NULL,
                       last_safe_error_code=%s,etag=etag+1,updated_at=%s
                   WHERE id=%s AND status<>'DISABLED'""",
                (safe_error_code, now, command.exchange_account_id),
            )
            connection.execute(
                """INSERT INTO credential_validation_results
                     (id,tenant_id,exchange_account_id,credential_id,command_id,success,
                      capability_json,warning_codes,safe_error_code,checked_at,created_at)
                   VALUES (%s,%s,%s,%s,%s,false,'{}','{}',%s,%s,%s)""",
                (
                    uuid4(), command.tenant_id, command.exchange_account_id, command.credential_id,
                    command.id, safe_error_code, now, now,
                ),
            )
            self._outbox(
                connection, command.tenant_id, command.exchange_account_id,
                "exchange_account.validation_failed.v1",
                {"account_id": str(command.exchange_account_id), "safe_error_code": safe_error_code}, now,
            )
            return False

    @staticmethod
    def _require_no_unresolved_execution(connection: Any, account_id: UUID) -> None:
        # Caller holds the account row lock, shared with drill admission. A
        # separate SELECT after that lock prevents a stale-snapshot race from
        # rotating away credentials needed by an already admitted position.
        active = connection.execute("""SELECT 1 FROM testnet_execution_drills
            WHERE exchange_account_id=%s AND state NOT IN
              ('ENTRY_UNFILLED','CLOSED_PROTECTED','CLOSED_FAIL_SAFE','PREFLIGHT_REJECTED')
            LIMIT 1""", (account_id,)).fetchone()
        if active is not None:
            raise ExchangeAccountConflict("账户仍有未完成的执行或持仓对账，暂不能修改凭据、重新验证或停用")

    @staticmethod
    def _insert_command(
        connection: Any,
        command_id: UUID,
        tenant_id: UUID,
        account_id: UUID,
        credential_id: UUID,
        requested_by: UUID,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO credential_validation_commands
                 (id,tenant_id,exchange_account_id,credential_id,requested_by,state,attempt_count,
                  next_attempt_at,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,'PENDING',0,%s,%s,%s)""",
            (command_id, tenant_id, account_id, credential_id, requested_by, now, now, now),
        )

    @staticmethod
    def _outbox(
        connection: Any,
        tenant_id: UUID,
        account_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO outbox_events
                 (event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,
                  payload_json,occurred_at,created_at,next_attempt_at)
               VALUES (%s,%s,'EXCHANGE_ACCOUNT',%s,%s,1,%s::jsonb,%s,%s,%s)""",
            (uuid4(), tenant_id, account_id, event_type, json.dumps(payload), now, now, now),
        )


def _encryption_context(
    tenant_id: UUID,
    account_id: UUID,
    credential_id: UUID,
    version: int,
) -> str:
    return (
        f"gatescope:v1:tenant:{tenant_id}:account:{account_id}:credential:{credential_id}:"
        f"version:{version}:venue:GATE:environment:TESTNET"
    )


def _account(row: dict[str, Any]) -> ExchangeAccount:
    capabilities = row["capability_json"]
    if isinstance(capabilities, str):
        capabilities = json.loads(capabilities)
    return ExchangeAccount(
        id=UUID(str(row["id"])), owner_user_id=UUID(str(row["owner_user_id"])),
        alias=str(row["alias"]), venue=str(row["venue"]), environment=str(row["environment"]),
        market_type=str(row["market_type"]), settle_asset=str(row["settle_asset"]),
        status=str(row["status"]), trading_ready=bool(row["trading_ready"]),
        credential_version=int(row["credential_version"]),
        credential_fingerprint=str(row["credential_fingerprint"]) if row["credential_fingerprint"] else None,
        capabilities=dict(capabilities), warning_codes=tuple(row["warning_codes"] or ()),
        last_safe_error_code=str(row["last_safe_error_code"]) if row["last_safe_error_code"] else None,
        testnet_armed_at=row["testnet_armed_at"],
        testnet_armed_by=UUID(str(row["testnet_armed_by"])) if row["testnet_armed_by"] else None,
        execution_freeze_code=(
            str(row["execution_freeze_code"]) if row["execution_freeze_code"] else None
        ),
        validation_requested_at=row["validation_requested_at"], validated_at=row["validated_at"],
        etag=int(row["etag"]), created_at=row["created_at"], updated_at=row["updated_at"],
    )
