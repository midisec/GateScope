"""Append-only persistence for independently evaluated execution eligibility."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.quant import LiveEligibilityDecision


class StrategyCertificationConflict(RuntimeError):
    pass


class PostgresStrategyCertificationRepository:
    """Writer used by the strategy workload, never by the public API."""

    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def record_decision(
        self,
        tenant_id: UUID,
        strategy_subject_id: UUID,
        *,
        approved_by: UUID,
        decision: LiveEligibilityDecision,
        validity: timedelta = timedelta(days=30),
    ) -> UUID:
        if validity <= timedelta(0) or validity > timedelta(days=90):
            raise ValueError("certification validity must be in (0, 90 days]")
        certification_id = uuid4()
        expires_at = decision.decided_at + validity
        try:
            with self.pool.transaction(tenant_id, strategy_subject_id) as connection:
                inserted = connection.execute(
                    """INSERT INTO strategy_execution_certifications
                         (id,tenant_id,strategy_version_ref,environment,decision,evidence_tier,
                          certification_policy_version,source_artifact_digest,
                          artifact_computed_at,gate_report_json,reason_codes_json,
                          assumptions_json,certification_digest,approved_by,approved_at,
                          expires_at,created_at)
                       VALUES (%s,%s,%s,'TESTNET',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT (tenant_id,certification_digest) DO NOTHING
                       RETURNING id""",
                    (
                        certification_id,
                        tenant_id,
                        decision.strategy_version_ref,
                        decision.decision,
                        decision.evidence_tier,
                        decision.policy_version,
                        decision.source_artifact_digest,
                        decision.artifact_computed_at,
                        Jsonb(list(decision.gate_report)),
                        Jsonb(list(decision.reason_codes)),
                        Jsonb(list(decision.assumptions)),
                        decision.certification_digest,
                        approved_by,
                        decision.decided_at,
                        expires_at,
                    ),
                ).fetchone()
                if inserted is not None:
                    return UUID(str(inserted[0]))
                existing = connection.execute(
                    """SELECT id,strategy_version_ref,decision,evidence_tier,
                              certification_policy_version,source_artifact_digest,
                              artifact_computed_at,gate_report_json,reason_codes_json,
                              assumptions_json,approved_by,approved_at,expires_at
                       FROM strategy_execution_certifications
                       WHERE certification_digest=%s""",
                    (decision.certification_digest,),
                ).fetchone()
                expected = (
                    decision.strategy_version_ref,
                    decision.decision,
                    decision.evidence_tier,
                    decision.policy_version,
                    decision.source_artifact_digest,
                    decision.artifact_computed_at,
                    list(decision.gate_report),
                    list(decision.reason_codes),
                    list(decision.assumptions),
                    approved_by,
                    decision.decided_at,
                    expires_at,
                )
                if existing is None or tuple(existing[1:]) != expected:
                    raise StrategyCertificationConflict(
                        "certification digest is already bound to different immutable facts"
                    )
                return UUID(str(existing[0]))
        except (CheckViolation, ForeignKeyViolation, InsufficientPrivilege) as exc:
            raise StrategyCertificationConflict(
                "strategy certification failed database admission"
            ) from exc
