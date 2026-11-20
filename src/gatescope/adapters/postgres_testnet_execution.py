"""Durable PostgreSQL control plane for explicit Gate TESTNET drills."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.credential_crypto import CredentialEnvelope


class TestnetDrillNotFound(LookupError):
    pass


class TestnetDrillConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class TestnetDrill:
    id: UUID
    public_drill_id: str
    owner_user_id: UUID
    paper_trade_id: UUID
    paper_public_trade_id: str
    exchange_account_id: UUID
    exchange_alias: str
    instrument_id: UUID
    contract: str
    side: str
    exact_terms: dict[str, Any]
    exact_terms_digest: str
    state: str
    safe_code: str | None
    entry_order_id: str | None
    filled_contracts: int
    stop_order_id: str | None
    take_profit_order_id: str | None
    fail_safe_order_id: str | None
    frozen: bool
    version: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    next_reconcile_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class StrategyExecutionCertification:
    id: UUID
    strategy_version_ref: str
    environment: str
    decision: str
    evidence_tier: str
    certification_policy_version: str
    source_artifact_digest: str
    certification_digest: str
    approved_at: datetime
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class TestnetExecutionCommand:
    id: UUID
    public_drill_id: str
    tenant_id: UUID
    exchange_account_id: UUID
    credential_id: UUID
    envelope: CredentialEnvelope
    encryption_context: str
    contract: str
    side: str
    exact_terms: dict[str, Any]


@dataclass(frozen=True)
class TestnetOutcomeRecord:
    state: str
    safe_code: str
    entry_order_id: str | None
    filled_contracts: int
    stop_order_id: str | None
    take_profit_order_id: str | None
    fail_safe_order_id: str | None
    frozen: bool


@dataclass(frozen=True)
class TestnetProtectionCommand:
    id: UUID
    public_drill_id: str
    tenant_id: UUID
    exchange_account_id: UUID
    envelope: CredentialEnvelope
    encryption_context: str
    contract: str
    side: str
    filled_contracts: int
    stop_order_id: str | None
    take_profit_order_id: str | None
    execution_events: tuple[dict[str, Any], ...] = ()


class ExecutionJournal(Protocol):
    def append(self, event_type: str, payload: Mapping[str, Any]) -> None: ...


class PostgresTestnetExecutionRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def create_drill(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        paper_public_trade_id: str,
        exchange_account_id: UUID,
        idempotency_key: str,
    ) -> TestnetDrill:
        if re.fullmatch(r"paper_[0-9A-HJKMNP-TV-Z]{26}", paper_public_trade_id) is None:
            raise ValueError("paper trade ID is invalid")
        if re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", idempotency_key) is None:
            raise ValueError("idempotency key is invalid")
        request_hash = hashlib.sha256(
            f"{owner_user_id}\0{idempotency_key}".encode("ascii"),
        ).hexdigest()
        now = datetime.now(timezone.utc)
        drill_id = uuid4()
        public_id = _public_drill_id()
        try:
            with self.pool.transaction(tenant_id, owner_user_id) as connection:
                existing = connection.execute(
                    """SELECT public_drill_id,paper_trade_id,exchange_account_id
                       FROM testnet_execution_drills
                       WHERE owner_user_id=%s AND idempotency_hash=%s""",
                    (owner_user_id, request_hash),
                ).fetchone()
                if existing is not None:
                    requested_trade = connection.execute(
                        "SELECT id FROM trades WHERE public_trade_id=%s",
                        (paper_public_trade_id,),
                    ).fetchone()
                    if (
                        requested_trade is None
                        or UUID(str(existing[1])) != UUID(str(requested_trade[0]))
                        or UUID(str(existing[2])) != exchange_account_id
                    ):
                        raise TestnetDrillConflict("idempotency key was used for different inputs")
                    return self.get_drill(tenant_id, owner_user_id, str(existing[0]))
                source = connection.execute(
                    """SELECT t.id,t.instrument_id,t.side,t.exact_terms_json,t.exact_terms_digest,
                              i.venue,i.market,i.symbol,state.state,evidence.strategy_version_ref,
                              EXISTS (
                                SELECT 1 FROM strategy_execution_certifications certification
                                WHERE certification.tenant_id=t.tenant_id
                                  AND certification.strategy_version_ref=evidence.strategy_version_ref
                                  AND certification.environment='TESTNET'
                                  AND certification.decision='CERTIFY'
                                  AND certification.evidence_tier='LIVE_ELIGIBLE'
                                  AND jsonb_array_length(certification.gate_report_json) IN (19,23)
                                  AND certification.approved_at <= now()
                                  AND certification.expires_at > now()
                              ) AS execution_certified
                       FROM trades t
                       JOIN trade_state_current state ON state.trade_id=t.id
                       JOIN instruments i ON i.id=t.instrument_id
                       JOIN signal_evidence_snapshots evidence
                         ON evidence.id=t.signal_evidence_snapshot_id
                       WHERE t.public_trade_id=%s AND t.owner_user_id=%s FOR UPDATE OF t,state""",
                    (paper_public_trade_id, owner_user_id),
                ).fetchone()
                if source is None:
                    raise TestnetDrillNotFound("paper trade not found")
                if str(source[8]) != "OPEN":
                    raise TestnetDrillConflict("only an open frozen PAPER trade can seed a drill")
                if not bool(source[10]):
                    raise TestnetDrillConflict(
                        "strategy version lacks an active LIVE_ELIGIBLE TESTNET certification"
                    )
                if str(source[5]) != "GATE" or str(source[6]) != "FUTURES_USDT":
                    raise TestnetDrillConflict("paper trade instrument is not Gate USDT futures")
                contract = str(source[7]).upper()
                if re.fullmatch(r"[A-Z0-9]{2,20}_USDT", contract) is None:
                    raise TestnetDrillConflict("instrument symbol is not a Gate contract")
                account = connection.execute(
                    """SELECT status,trading_ready,owner_user_id FROM exchange_accounts
                       WHERE id=%s FOR UPDATE""",
                    (exchange_account_id,),
                ).fetchone()
                if (
                    account is None
                    or UUID(str(account[2])) != owner_user_id
                    or str(account[0]) != "TESTNET_READY"
                    or not bool(account[1])
                ):
                    raise TestnetDrillConflict("exchange account is not TESTNET ready")
                exact_terms = _object(source[3])
                connection.execute(
                    """INSERT INTO testnet_execution_drills
                         (id,public_drill_id,tenant_id,owner_user_id,paper_trade_id,
                          exchange_account_id,instrument_id,contract,side,environment,
                          exact_terms_json,exact_terms_digest,idempotency_hash,state,frozen,
                          attempt_count,version,created_at,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'TESTNET',%s,%s,%s,'QUEUED',
                               false,0,1,%s,%s)""",
                    (
                        drill_id,
                        public_id,
                        tenant_id,
                        owner_user_id,
                        source[0],
                        exchange_account_id,
                        source[1],
                        contract,
                        source[2],
                        Jsonb(exact_terms),
                        source[4],
                        request_hash,
                        now,
                        now,
                    ),
                )
                self._append_event(
                    connection,
                    tenant_id,
                    drill_id,
                    "DRILL_QUEUED",
                    {
                        "paper_public_trade_id": paper_public_trade_id,
                        "exchange_account_id": str(exchange_account_id),
                        "exact_terms_digest": str(source[4]),
                        "environment": "TESTNET",
                    },
                    now,
                )
                reserved = connection.execute(
                    """UPDATE exchange_accounts
                       SET status='TESTNET_BUSY',trading_ready=false,etag=etag+1,updated_at=%s
                       WHERE id=%s AND status='TESTNET_READY' AND trading_ready""",
                    (now, exchange_account_id),
                )
                if reserved.rowcount != 1:
                    raise TestnetDrillConflict("exchange account reservation was lost")
                self._outbox(
                    connection,
                    tenant_id,
                    drill_id,
                    "testnet_execution.queued.v1",
                    {"public_drill_id": public_id, "environment": "TESTNET"},
                    now,
                )
        except (CheckViolation, UniqueViolation) as exc:
            raise TestnetDrillConflict("testnet drill already exists or failed admission") from exc
        return self.get_drill(tenant_id, owner_user_id, public_id)

    def list_certifications(
        self,
        tenant_id: UUID,
        subject_id: UUID,
    ) -> list[StrategyExecutionCertification]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    """SELECT id,strategy_version_ref,environment,decision,evidence_tier,
                              certification_policy_version,source_artifact_digest,
                              certification_digest,approved_at,expires_at,created_at
                       FROM strategy_execution_certifications
                       WHERE decision='CERTIFY' AND approved_at <= now() AND expires_at > now()
                       ORDER BY approved_at DESC,id DESC LIMIT 500""",
                ).fetchall()
        return [
            StrategyExecutionCertification(
                id=UUID(str(row["id"])),
                strategy_version_ref=str(row["strategy_version_ref"]),
                environment=str(row["environment"]),
                decision=str(row["decision"]),
                evidence_tier=str(row["evidence_tier"]),
                certification_policy_version=str(row["certification_policy_version"]),
                source_artifact_digest=str(row["source_artifact_digest"]),
                certification_digest=str(row["certification_digest"]),
                approved_at=row["approved_at"],
                expires_at=row["expires_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_drills(self, tenant_id: UUID, subject_id: UUID, *, limit: int = 100) -> list[TestnetDrill]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    self._select() + " ORDER BY drill.created_at DESC,drill.id DESC LIMIT %s",
                    (max(1, min(limit, 500)),),
                ).fetchall()
        return [_drill(row) for row in rows]

    def get_drill(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        public_drill_id: str,
    ) -> TestnetDrill:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    self._select() + " WHERE drill.public_drill_id=%s",
                    (public_drill_id,),
                ).fetchone()
        if row is None:
            raise TestnetDrillNotFound("testnet drill not found")
        return _drill(row)

    def timeline(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        public_drill_id: str,
    ) -> list[dict[str, Any]]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            drill = connection.execute(
                "SELECT id FROM testnet_execution_drills WHERE public_drill_id=%s",
                (public_drill_id,),
            ).fetchone()
            if drill is None:
                raise TestnetDrillNotFound("testnet drill not found")
            with connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    """SELECT sequence,event_type,payload_json,event_digest,occurred_at,recorded_at
                       FROM testnet_execution_events WHERE drill_id=%s ORDER BY sequence""",
                    (drill[0],),
                ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "payload": _object(row["payload_json"]),
                "event_digest": str(row["event_digest"]),
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]

    def due_tenants(self, *, limit: int = 20) -> list[UUID]:
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM gatescope_security.testnet_execution_due_tenants(%s)",
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
    ) -> TestnetExecutionCommand | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(10, min(lease_seconds, 120)))
        with self.pool.transaction(tenant_id, worker_subject_id) as connection:
            row = connection.execute(
                """SELECT drill.id,drill.public_drill_id,drill.tenant_id,
                          drill.exchange_account_id,account.active_credential_id,
                          credential.ciphertext,credential.nonce,credential.wrapped_data_key,
                          credential.encryption_context,credential.key_version,drill.contract,
                          drill.side,drill.exact_terms_json,drill.attempt_count,drill.state
                   FROM testnet_execution_drills drill
                   JOIN exchange_accounts account ON account.id=drill.exchange_account_id
                   JOIN secret_credentials credential ON credential.id=account.active_credential_id
                   WHERE (drill.state='QUEUED'
                          OR (drill.state='PROCESSING' AND drill.lease_expires_at <= %s))
                     AND account.status='TESTNET_BUSY'
                     AND account.environment='TESTNET' AND credential.status='READ_VALIDATED'
                   ORDER BY drill.created_at,drill.id
                   FOR UPDATE OF drill SKIP LOCKED LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            if str(row[14]) == "PROCESSING":
                connection.execute(
                    """UPDATE testnet_execution_drills
                       SET state='RECONCILIATION_PENDING',
                           safe_code='EXECUTOR_LEASE_EXPIRED',frozen=true,lease_owner=NULL,
                           lease_expires_at=NULL,completed_at=%s,next_reconcile_at=%s,
                           version=version+1,updated_at=%s
                       WHERE id=%s""",
                    (now, now, now, row[0]),
                )
                connection.execute(
                    """UPDATE exchange_accounts
                       SET status='EXECUTION_FROZEN',trading_ready=false,
                           execution_freeze_code='EXECUTOR_LEASE_EXPIRED',etag=etag+1,updated_at=%s
                       WHERE id=%s""",
                    (now, row[3]),
                )
                self._append_event(
                    connection,
                    tenant_id,
                    UUID(str(row[0])),
                    "EXECUTOR_LEASE_EXPIRED",
                    {"action": "FROZEN_WITH_CONTINUING_RECONCILIATION_NO_MUTATION_REPLAY"},
                    now,
                )
                self._outbox(
                    connection,
                    tenant_id,
                    UUID(str(row[0])),
                    "testnet_execution.manual_intervention.v1",
                    {
                        "public_drill_id": str(row[1]),
                        "safe_code": "EXECUTOR_LEASE_EXPIRED",
                        "risk_critical": True,
                    },
                    now,
                )
                return None
            attempt = int(row[13]) + 1
            if attempt != 1:
                raise TestnetDrillConflict("a TESTNET mutation workflow cannot be replayed")
            connection.execute(
                """UPDATE testnet_execution_drills
                   SET state='PROCESSING',attempt_count=%s,lease_owner=%s,lease_expires_at=%s,
                       started_at=%s,version=version+1,updated_at=%s WHERE id=%s""",
                (attempt, worker_id, lease_until, now, now, row[0]),
            )
            self._append_event(
                connection,
                tenant_id,
                UUID(str(row[0])),
                "DRILL_CLAIMED",
                {"worker_id_hash": hashlib.sha256(worker_id.encode()).hexdigest()},
                now,
            )
            exact = _object(row[12])
            return TestnetExecutionCommand(
                id=UUID(str(row[0])),
                public_drill_id=str(row[1]),
                tenant_id=UUID(str(row[2])),
                exchange_account_id=UUID(str(row[3])),
                credential_id=UUID(str(row[4])),
                envelope=CredentialEnvelope(bytes(row[5]), bytes(row[6]), bytes(row[7]), str(row[9])),
                encryption_context=str(row[8]),
                contract=str(row[10]),
                side=str(row[11]),
                exact_terms=exact,
            )

    def journal(
        self,
        command: TestnetExecutionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
    ) -> ExecutionJournal:
        return _PostgresExecutionJournal(self, command, worker_subject_id, worker_id)

    def protection_journal(
        self,
        command: TestnetProtectionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
    ) -> ExecutionJournal:
        return _PostgresProtectionJournal(self, command, worker_subject_id, worker_id)

    def complete(
        self,
        command: TestnetExecutionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        outcome: TestnetOutcomeRecord,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            locked = connection.execute(
                """SELECT state,lease_owner,exchange_account_id FROM testnet_execution_drills
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if locked is None or str(locked[0]) != "PROCESSING" or str(locked[1]) != worker_id:
                raise TestnetDrillConflict("TESTNET execution lease was lost")
            self._append_event(
                connection,
                command.tenant_id,
                command.id,
                "DRILL_OUTCOME_RECORDED",
                {
                    "state": outcome.state,
                    "safe_code": outcome.safe_code,
                    "frozen": outcome.frozen,
                },
                now,
            )
            connection.execute(
                """UPDATE testnet_execution_drills
                   SET state=%s,safe_code=%s,entry_order_id=%s,filled_contracts=%s,
                       stop_order_id=%s,take_profit_order_id=%s,fail_safe_order_id=%s,
                       frozen=%s,lease_owner=NULL,lease_expires_at=NULL,version=version+1,
                       completed_at=%s,next_reconcile_at=%s,updated_at=%s WHERE id=%s""",
                (
                    outcome.state,
                    outcome.safe_code,
                    outcome.entry_order_id,
                    outcome.filled_contracts,
                    outcome.stop_order_id,
                    outcome.take_profit_order_id,
                    outcome.fail_safe_order_id,
                    outcome.frozen,
                    now,
                    now if outcome.state in {"PROTECTED", "RECONCILIATION_PENDING"} else None,
                    now,
                    command.id,
                ),
            )
            if outcome.state == "ENTRY_UNFILLED" and not outcome.frozen:
                connection.execute(
                    """UPDATE exchange_accounts SET status='TESTNET_READY',trading_ready=true,
                         execution_freeze_code=NULL,etag=etag+1,updated_at=%s WHERE id=%s""",
                    (now, command.exchange_account_id),
                )
            elif outcome.state != "PROTECTED":
                connection.execute(
                    """UPDATE exchange_accounts SET status='EXECUTION_FROZEN',trading_ready=false,
                         execution_freeze_code=%s,etag=etag+1,updated_at=%s WHERE id=%s""",
                    (outcome.safe_code, now, command.exchange_account_id),
                )
            self._outbox(
                connection,
                command.tenant_id,
                command.id,
                "testnet_execution.outcome.v1",
                {
                    "public_drill_id": command.public_drill_id,
                    "state": outcome.state,
                    "safe_code": outcome.safe_code,
                    "risk_critical": outcome.frozen,
                },
                now,
            )

    def claim_protected(
        self,
        tenant_id: UUID,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> TestnetProtectionCommand | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=max(10, min(lease_seconds, 120)))
        with self.pool.transaction(tenant_id, worker_subject_id) as connection:
            row = connection.execute(
                """SELECT drill.id,drill.public_drill_id,drill.tenant_id,
                          drill.exchange_account_id,credential.ciphertext,credential.nonce,
                          credential.wrapped_data_key,credential.encryption_context,
                          credential.key_version,drill.contract,drill.side,
                          drill.filled_contracts,drill.stop_order_id,drill.take_profit_order_id
                   FROM testnet_execution_drills drill
                   JOIN exchange_accounts account ON account.id=drill.exchange_account_id
                   JOIN secret_credentials credential ON credential.id=account.active_credential_id
                   WHERE drill.state IN ('PROTECTED','RECONCILIATION_PENDING')
                     AND drill.next_reconcile_at <= %s
                     AND (drill.monitor_lease_expires_at IS NULL
                          OR drill.monitor_lease_expires_at <= %s)
                     AND account.status IN ('TESTNET_BUSY','EXECUTION_FROZEN')
                     AND account.environment='TESTNET' AND credential.status='READ_VALIDATED'
                   ORDER BY drill.next_reconcile_at,drill.id
                   FOR UPDATE OF drill SKIP LOCKED LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                """SELECT event_type,payload_json FROM testnet_execution_events
                   WHERE drill_id=%s AND event_type IN (
                     'ENTRY_RECONCILED','STOP_ACTIVE','TAKE_PROFIT_ACTIVE',
                     'STOP_LOSS_ACKNOWLEDGED','TAKE_PROFIT_ACKNOWLEDGED',
                     'STOP_LOSS_PREPARED','TAKE_PROFIT_PREPARED',
                     'FAIL_SAFE_EXIT_PREPARED','MONITOR_EXIT_PREPARED')
                   ORDER BY sequence""",
                (row[0],),
            ).fetchall()
            execution_events: tuple[dict[str, Any], ...] = tuple(
                {"event_type": str(item[0]), "payload": _object(item[1])} for item in events
            )
            stop_order_id = str(row[12]) if row[12] is not None else None
            take_profit_order_id = str(row[13]) if row[13] is not None else None
            filled_contracts = int(row[11])
            for item in execution_events:
                payload = item["payload"]
                if item["event_type"] in {"STOP_ACTIVE", "STOP_LOSS_ACKNOWLEDGED"}:
                    stop_order_id = payload.get("exchange_order_id") or stop_order_id
                elif item["event_type"] in {"TAKE_PROFIT_ACTIVE", "TAKE_PROFIT_ACKNOWLEDGED"}:
                    take_profit_order_id = payload.get("exchange_order_id") or take_profit_order_id
                elif item["event_type"] == "ENTRY_RECONCILED":
                    filled_contracts = max(filled_contracts, int(payload.get("filled_contracts", 0)))
            connection.execute(
                """UPDATE testnet_execution_drills
                   SET monitor_lease_owner=%s,monitor_lease_expires_at=%s,
                       reconcile_count=reconcile_count+1,version=version+1,updated_at=%s
                   WHERE id=%s""",
                (worker_id, lease_until, now, row[0]),
            )
            return TestnetProtectionCommand(
                id=UUID(str(row[0])),
                public_drill_id=str(row[1]),
                tenant_id=UUID(str(row[2])),
                exchange_account_id=UUID(str(row[3])),
                envelope=CredentialEnvelope(bytes(row[4]), bytes(row[5]), bytes(row[6]), str(row[8])),
                encryption_context=str(row[7]),
                contract=str(row[9]),
                side=str(row[10]),
                filled_contracts=filled_contracts,
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
                execution_events=execution_events,
            )

    def complete_protection_check(
        self,
        command: TestnetProtectionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        outcome: TestnetOutcomeRecord,
        next_check_seconds: int = 5,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            locked = connection.execute(
                """SELECT state,monitor_lease_owner,safe_code FROM testnet_execution_drills
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if locked is None or str(locked[0]) not in {"PROTECTED", "RECONCILIATION_PENDING"} or str(locked[1]) != worker_id:
                raise TestnetDrillConflict("TESTNET protection-monitor lease was lost")
            if outcome.state in {"PROTECTED", "RECONCILIATION_PENDING"}:
                connection.execute(
                    """UPDATE testnet_execution_drills
                       SET state=%s,safe_code=%s,frozen=%s,
                           monitor_lease_owner=NULL,monitor_lease_expires_at=NULL,
                           filled_contracts=greatest(filled_contracts,%s),
                           stop_order_id=coalesce(%s,stop_order_id),
                           take_profit_order_id=coalesce(%s,take_profit_order_id),
                           fail_safe_order_id=coalesce(%s,fail_safe_order_id),
                           next_reconcile_at=%s,version=version+1,updated_at=%s WHERE id=%s""",
                    (outcome.state, outcome.safe_code, outcome.frozen,
                     max(command.filled_contracts, outcome.filled_contracts),
                     outcome.stop_order_id or command.stop_order_id,
                     outcome.take_profit_order_id or command.take_profit_order_id,
                     outcome.fail_safe_order_id,
                     now + timedelta(seconds=max(2, min(next_check_seconds, 60))), now, command.id),
                )
                account_status = "EXECUTION_FROZEN" if outcome.frozen else "TESTNET_BUSY"
                connection.execute(
                    """UPDATE exchange_accounts SET status=%s,trading_ready=false,
                         execution_freeze_code=%s,etag=etag+1,updated_at=%s WHERE id=%s""",
                    (account_status, outcome.safe_code if outcome.frozen else None,
                     now, command.exchange_account_id),
                )
                if str(locked[0]) != outcome.state or str(locked[2]) != outcome.safe_code:
                    self._append_event(
                        connection, command.tenant_id, command.id, "PROTECTION_MONITOR_OUTCOME",
                        {"state": outcome.state, "safe_code": outcome.safe_code, "frozen": outcome.frozen}, now,
                    )
                    self._outbox(
                        connection, command.tenant_id, command.id, "testnet_execution.protection_outcome.v1",
                        {"public_drill_id": command.public_drill_id, "state": outcome.state,
                         "safe_code": outcome.safe_code, "risk_critical": outcome.frozen}, now,
                    )
                return
            self._append_event(
                connection,
                command.tenant_id,
                command.id,
                "PROTECTION_MONITOR_OUTCOME",
                {"state": outcome.state, "safe_code": outcome.safe_code, "frozen": outcome.frozen},
                now,
            )
            connection.execute(
                """UPDATE testnet_execution_drills
                   SET state=%s,safe_code=%s,fail_safe_order_id=coalesce(%s,fail_safe_order_id),
                       frozen=%s,monitor_lease_owner=NULL,monitor_lease_expires_at=NULL,
                       next_reconcile_at=NULL,version=version+1,completed_at=%s,updated_at=%s
                   WHERE id=%s""",
                (
                    outcome.state,
                    outcome.safe_code,
                    outcome.fail_safe_order_id,
                    outcome.frozen,
                    now,
                    now,
                    command.id,
                ),
            )
            if outcome.state == "CLOSED_PROTECTED" and not outcome.frozen:
                connection.execute(
                    """UPDATE exchange_accounts SET status='TESTNET_READY',trading_ready=true,
                         execution_freeze_code=NULL,etag=etag+1,updated_at=%s WHERE id=%s""",
                    (now, command.exchange_account_id),
                )
            else:
                connection.execute(
                    """UPDATE exchange_accounts SET status='EXECUTION_FROZEN',trading_ready=false,
                         execution_freeze_code=%s,etag=etag+1,updated_at=%s WHERE id=%s""",
                    (outcome.safe_code, now, command.exchange_account_id),
                )
            self._outbox(
                connection,
                command.tenant_id,
                command.id,
                "testnet_execution.protection_outcome.v1",
                {
                    "public_drill_id": command.public_drill_id,
                    "state": outcome.state,
                    "safe_code": outcome.safe_code,
                    "risk_critical": outcome.frozen,
                },
                now,
            )

    def append_worker_event(
        self,
        command: TestnetExecutionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            locked = connection.execute(
                """SELECT state,lease_owner FROM testnet_execution_drills
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if (
                locked is None
                or str(locked[0]) != "PROCESSING"
                or str(locked[1]) != worker_id
            ):
                raise TestnetDrillConflict("TESTNET execution lease was lost")
            self._append_event(
                connection,
                command.tenant_id,
                command.id,
                event_type,
                dict(payload),
                datetime.now(timezone.utc),
            )

    def append_protection_event(
        self,
        command: TestnetProtectionCommand,
        worker_subject_id: UUID,
        *,
        worker_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self.pool.transaction(command.tenant_id, worker_subject_id) as connection:
            locked = connection.execute(
                """SELECT state,monitor_lease_owner FROM testnet_execution_drills
                   WHERE id=%s FOR UPDATE""",
                (command.id,),
            ).fetchone()
            if locked is None or str(locked[0]) not in {"PROTECTED", "RECONCILIATION_PENDING"} or str(locked[1]) != worker_id:
                raise TestnetDrillConflict("TESTNET protection-monitor lease was lost")
            self._append_event(
                connection,
                command.tenant_id,
                command.id,
                event_type,
                dict(payload),
                datetime.now(timezone.utc),
            )

    @staticmethod
    def _append_event(
        connection: Any,
        tenant_id: UUID,
        drill_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> None:
        previous = connection.execute(
            """SELECT sequence,event_digest FROM testnet_execution_events
               WHERE drill_id=%s ORDER BY sequence DESC LIMIT 1""",
            (drill_id,),
        ).fetchone()
        sequence = int(previous[0]) + 1 if previous else 1
        previous_digest = str(previous[1]) if previous else None
        canonical = {
            "drill_id": str(drill_id),
            "event_type": event_type,
            "occurred_at": occurred_at.isoformat(),
            "payload": dict(payload),
            "previous_digest": previous_digest,
            "sequence": sequence,
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        connection.execute(
            """INSERT INTO testnet_execution_events
                 (id,tenant_id,drill_id,sequence,event_type,payload_json,previous_digest,
                  event_digest,occurred_at,recorded_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(), tenant_id, drill_id, sequence, event_type, Jsonb(dict(payload)),
                previous_digest, digest, occurred_at, datetime.now(timezone.utc),
            ),
        )

    @staticmethod
    def _outbox(
        connection: Any,
        tenant_id: UUID,
        aggregate_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO outbox_events
                 (event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,
                  payload_json,occurred_at,created_at,next_attempt_at)
               VALUES (%s,%s,'TESTNET_EXECUTION',%s,%s,1,%s,%s,%s,%s)""",
            (uuid4(), tenant_id, aggregate_id, event_type, Jsonb(dict(payload)), now, now, now),
        )

    @staticmethod
    def _select() -> str:
        return """SELECT drill.id,drill.public_drill_id,drill.owner_user_id,
                         drill.paper_trade_id,trade.public_trade_id AS paper_public_trade_id,
                         drill.exchange_account_id,account.alias AS exchange_alias,
                         drill.instrument_id,drill.contract,drill.side,drill.exact_terms_json,
                         drill.exact_terms_digest,drill.state,drill.safe_code,
                         drill.entry_order_id,drill.filled_contracts,drill.stop_order_id,
                         drill.take_profit_order_id,drill.fail_safe_order_id,drill.frozen,
                         drill.version,drill.created_at,drill.started_at,drill.completed_at,
                         drill.next_reconcile_at,drill.updated_at
                  FROM testnet_execution_drills drill
                  JOIN trades trade ON trade.id=drill.paper_trade_id
                  JOIN exchange_accounts account ON account.id=drill.exchange_account_id"""


class _PostgresExecutionJournal:
    def __init__(
        self,
        repository: PostgresTestnetExecutionRepository,
        command: TestnetExecutionCommand,
        worker_subject_id: UUID,
        worker_id: str,
    ) -> None:
        self.repository = repository
        self.command = command
        self.worker_subject_id = worker_subject_id
        self.worker_id = worker_id

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.repository.append_worker_event(
            self.command,
            self.worker_subject_id,
            worker_id=self.worker_id,
            event_type=event_type,
            payload=payload,
        )


class _PostgresProtectionJournal:
    def __init__(
        self,
        repository: PostgresTestnetExecutionRepository,
        command: TestnetProtectionCommand,
        worker_subject_id: UUID,
        worker_id: str,
    ) -> None:
        self.repository = repository
        self.command = command
        self.worker_subject_id = worker_subject_id
        self.worker_id = worker_id

    def append(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.repository.append_protection_event(
            self.command,
            self.worker_subject_id,
            worker_id=self.worker_id,
            event_type=event_type,
            payload=payload,
        )


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise TestnetDrillConflict("stored JSON object is invalid")
    return dict(value)


def _drill(row: Mapping[str, Any]) -> TestnetDrill:
    return TestnetDrill(
        id=UUID(str(row["id"])),
        public_drill_id=str(row["public_drill_id"]),
        owner_user_id=UUID(str(row["owner_user_id"])),
        paper_trade_id=UUID(str(row["paper_trade_id"])),
        paper_public_trade_id=str(row["paper_public_trade_id"]),
        exchange_account_id=UUID(str(row["exchange_account_id"])),
        exchange_alias=str(row["exchange_alias"]),
        instrument_id=UUID(str(row["instrument_id"])),
        contract=str(row["contract"]),
        side=str(row["side"]),
        exact_terms=_object(row["exact_terms_json"]),
        exact_terms_digest=str(row["exact_terms_digest"]),
        state=str(row["state"]),
        safe_code=str(row["safe_code"]) if row["safe_code"] else None,
        entry_order_id=str(row["entry_order_id"]) if row["entry_order_id"] else None,
        filled_contracts=int(row["filled_contracts"]),
        stop_order_id=str(row["stop_order_id"]) if row["stop_order_id"] else None,
        take_profit_order_id=(
            str(row["take_profit_order_id"]) if row["take_profit_order_id"] else None
        ),
        fail_safe_order_id=(
            str(row["fail_safe_order_id"]) if row["fail_safe_order_id"] else None
        ),
        frozen=bool(row["frozen"]),
        version=int(row["version"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        next_reconcile_at=row["next_reconcile_at"],
        updated_at=row["updated_at"],
    )


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _public_drill_id() -> str:
    number = uuid4().int
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[number & 31])
        number >>= 5
    return "testnet_" + "".join(reversed(chars))
