from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gatescope.adapters.gate_credentials import GateProbeResult
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_exchange_accounts import ExchangeAccountConflict, PostgresExchangeAccountRepository
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from gatescope.adapters.postgres_strategy_certifications import (
    PostgresStrategyCertificationRepository,
)
from gatescope.adapters.postgres_testnet_execution import (
    PostgresTestnetExecutionRepository,
    TestnetDrillConflict as DrillConflict,
)
from gatescope.application.credential_validation import CredentialValidationMonitor
from gatescope.credential_crypto import CredentialMaterial
from gatescope.domain.paper_trading import PaperSide, SignalTerms
from gatescope.quant import LiveEligibilityArtifact, LiveEligibilityGateEvaluator, LiveEligibilityPolicy
from tests.integration.test_exchange_credential_admission import _crypto_pair
from tests.integration.test_paper_trade_ledger import _god, _insert_bar
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, USER_A, seed_two_tenants

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


class ReadyProbe:
    def probe(self, environment: str, material: CredentialMaterial) -> GateProbeResult:
        assert environment == "TESTNET"
        return GateProbeResult(
            {
                "schema_version": 1,
                "account_read": True,
                "futures_usdt_read": True,
                "testnet": True,
                "ip_whitelist_configured": True,
                "trade_permission": "UNVERIFIED",
                "withdrawal_permission": "UNVERIFIED",
                "trading_ready": False,
            },
            ("API_PERMISSION_SCOPE_UNVERIFIED",),
        )


def test_frozen_paper_terms_are_atomically_admitted_to_one_testnet_drill(
    migrated_database: str,
) -> None:
    seed_two_tenants(migrated_database)
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=121)
    with psycopg.connect(migrated_database) as connection:
        for index in range(120):
            _insert_bar(connection, base + timedelta(minutes=index))
    decision_at = base + timedelta(minutes=120, seconds=5)
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        paper = PostgresPaperRepository(pool)
        paper_account = paper.create_account(
            TENANT_A, USER_A, name="TESTNET source", base_currency="USDT",
            initial_equity=Decimal("10000"),
        )
        signal = paper.record_god_signal(
            TENANT_A,
            USER_A,
            instrument_id=INSTRUMENT,
            timeframe="1m",
            strategy_version_ref="testnet-admission-v1",
            god_decision=_god(decision_at),
            terms=SignalTerms(
                PaperSide.LONG, Decimal("100"), Decimal("95"), Decimal("110"), 10,
            ),
            indicator_snapshot={"rsi": {"value": "55", "version": "1.0.0"}},
        )
        decision = paper.create_execution_decision(
            TENANT_A,
            USER_A,
            signal.id,
            paper_account_id=paper_account.id,
            client_request_id="testnet-admission-decision-0001",
            now=decision_at,
        )
        proposal = paper.create_proposal(
            TENANT_A,
            USER_A,
            execution_decision_id=decision.id,
            client_request_id="testnet-admission-proposal-0001",
            now=decision_at,
        )
        approved = paper.approve_proposal(
            TENANT_A,
            USER_A,
            proposal.id,
            expected_version=proposal.version,
            idempotency_key="testnet-admission-approval-0001",
            now=decision_at + timedelta(seconds=1),
        )
        assert approved.trade is not None

        encryptor, decryptor = _crypto_pair()
        accounts = PostgresExchangeAccountRepository(pool)
        account, _ = accounts.create_account(
            TENANT_A,
            USER_A,
            alias="Gate isolated testnet",
            material=CredentialMaterial("integration-key-123456", "integration-secret-123456"),
            encryptor=encryptor,
            no_withdrawal_attested=True,
        )
        worker_id = "testnet-admission-validator"
        CredentialValidationMonitor(
            accounts,
            decryptor,
            ReadyProbe(),  # type: ignore[arg-type]
            worker_id=worker_id,
            worker_subject_id=uuid5(NAMESPACE_URL, worker_id),
        ).run_once()
        validated = accounts.get_account(TENANT_A, USER_A, account.id)
        armed = accounts.arm_testnet_execution(
            TENANT_A,
            USER_A,
            account.id,
            expected_etag=validated.etag,
            ip_whitelist_attested=True,
            no_withdrawal_attested=True,
            exclusive_account_attested=True,
        )
        assert armed.status == "TESTNET_READY" and armed.trading_ready

        executions = PostgresTestnetExecutionRepository(pool)
        with pytest.raises(DrillConflict, match="LIVE_ELIGIBLE"):
            executions.create_drill(
                TENANT_A,
                USER_A,
                paper_public_trade_id=approved.trade.public_trade_id,
                exchange_account_id=account.id,
                idempotency_key="testnet-uncertified-drill-0001",
            )
        assert accounts.get_account(TENANT_A, USER_A, account.id).status == "TESTNET_READY"
        artifact = LiveEligibilityArtifact(
            strategy_version_ref="testnet-admission-v1",
            source_artifact_digest="b" * 64,
            computed_at=decision_at,
            independent_blocks=500,
            required_power_blocks=400,
            holm_adjusted_p_value=0.02,
            policy_value_lcb=0.001,
            deflated_sharpe_probability=0.97,
            pbo_probability=0.08,
            base_cost_oos_ev=0.003,
            double_cost_oos_ev=0.001,
            event_ev_robust_lcb=0.0005,
            maximum_regime_profit_contribution=0.55,
            minimum_allowed_regime_blocks=130,
            leave_one_regime_out_ev=(0.001, 0.0005),
            trial_registry_complete=True,
            nested_walk_forward_complete=True,
            sealed_holdout_consumed_once=True,
            calibration_passed=True,
            maximum_drawdown_passed=True,
            data_quality_passed=True,
            out_of_distribution_passed=True,
            cost_model_passed=True,
            # These observations would fail every withdrawn 0026 floor. The
            # independent evidence certificate and account safety still apply.
            oos_sharpe_ratio=-.5, oos_max_drawdown=.30, oos_win_rate=.40,
            oos_t_statistic=-1., oos_daily_observations=20, oos_trade_count=5,
        )
        certification = LiveEligibilityGateEvaluator().evaluate(
            artifact, LiveEligibilityPolicy("integration-cert-v1"), decided_at=decision_at,
        )
        assert certification.decision == "CERTIFY" and len(certification.gate_report) == 19
        PostgresStrategyCertificationRepository(pool).record_decision(
            TENANT_A,
            USER_A,
            approved_by=USER_A,
            decision=certification,
            validity=timedelta(days=30),
        )
        certifications = executions.list_certifications(TENANT_A, USER_A)
        assert certifications[0].evidence_tier == "LIVE_ELIGIBLE"
        drill = executions.create_drill(
            TENANT_A,
            USER_A,
            paper_public_trade_id=approved.trade.public_trade_id,
            exchange_account_id=account.id,
            idempotency_key="testnet-admission-drill-0001",
        )
        assert drill.state == "QUEUED"
        assert drill.contract == "BTC_USDT"
        assert drill.exact_terms == dict(approved.trade.exact_terms)
        assert accounts.get_account(TENANT_A, USER_A, account.id).status == "TESTNET_BUSY"
        assert [item["event_type"] for item in executions.timeline(
            TENANT_A, USER_A, drill.public_drill_id,
        )] == ["DRILL_QUEUED"]
        # Neither queued nor recovery exposure may lose the credentials used by
        # its monitor, even when the account is frozen rather than TESTNET_BUSY.
        for state in ("QUEUED", "RECONCILIATION_PENDING"):
            if state == "RECONCILIATION_PENDING":
                with psycopg.connect(migrated_database) as connection:
                    connection.execute("""UPDATE testnet_execution_drills SET
                        state='RECONCILIATION_PENDING',frozen=true,next_reconcile_at=now(),
                        completed_at=now(),started_at=now(),attempt_count=1
                        WHERE id=%s""", (drill.id,))
                    connection.execute("""UPDATE exchange_accounts SET
                        status='EXECUTION_FROZEN',trading_ready=false,etag=etag+1,
                        execution_freeze_code='TEST_RECOVERY'
                        WHERE id=%s""", (account.id,))
            etag = accounts.get_account(TENANT_A, USER_A, account.id).etag
            with pytest.raises(ExchangeAccountConflict, match="未完成"):
                accounts.request_validation(TENANT_A, USER_A, account.id, expected_etag=etag)
            with pytest.raises(ExchangeAccountConflict, match="未完成"):
                accounts.disable_account(TENANT_A, USER_A, account.id, expected_etag=etag)
            with pytest.raises(ExchangeAccountConflict, match="未完成"):
                accounts.rotate_credential(TENANT_A, USER_A, account.id, expected_etag=etag,
                    material=CredentialMaterial("rotation-test-key", "rotation-test-secret"),
                    encryptor=encryptor, no_withdrawal_attested=True)
            assert accounts.get_account(TENANT_A, USER_A, account.id).etag == etag
    finally:
        pool.close()


@pytest.mark.parametrize("tamper", ["missing_result", "unknown_gate", "missing_value", "duplicate_gate", "missing_gate", "unrecorded_failure"])
def test_strategy_role_cannot_forge_a_passing_certification_report(
    migrated_database: str, tamper: str,
) -> None:
    seed_two_tenants(migrated_database)
    decided_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    artifact = LiveEligibilityArtifact(
        strategy_version_ref="forgery-test-v1",
        source_artifact_digest="d" * 64,
        computed_at=decided_at - timedelta(minutes=1),
        independent_blocks=500,
        required_power_blocks=400,
        holm_adjusted_p_value=0.02,
        policy_value_lcb=0.001,
        deflated_sharpe_probability=0.97,
        pbo_probability=0.08,
        base_cost_oos_ev=0.003,
        double_cost_oos_ev=0.001,
        event_ev_robust_lcb=0.0005,
        maximum_regime_profit_contribution=0.55,
        minimum_allowed_regime_blocks=130,
        leave_one_regime_out_ev=(0.001, 0.0005),
        trial_registry_complete=True,
        nested_walk_forward_complete=True,
        sealed_holdout_consumed_once=True,
        calibration_passed=True,
        maximum_drawdown_passed=True,
        data_quality_passed=True,
        out_of_distribution_passed=True,
        cost_model_passed=True,
        oos_sharpe_ratio=1.8, oos_max_drawdown=.10, oos_win_rate=.60,
        oos_t_statistic=2.5, oos_daily_observations=365, oos_trade_count=100,
    )
    decision = LiveEligibilityGateEvaluator().evaluate(
        artifact, LiveEligibilityPolicy("forgery-policy-v1"), decided_at=decided_at,
    )
    malformed_report = [dict(item) for item in decision.gate_report]
    if tamper == "missing_result":
        malformed_report[0].pop("result")
    elif tamper == "unknown_gate":
        malformed_report[0]["gate_code"] = "UNRECOGNIZED"
    elif tamper == "missing_value":
        malformed_report[0].pop("value")
    elif tamper == "duplicate_gate":
        malformed_report[0]["gate_code"] = malformed_report[1]["gate_code"]
    elif tamper == "missing_gate":
        malformed_report.pop()
    else:
        malformed_report[0]["result"] = "FAIL"
    with psycopg.connect(migrated_database) as connection:
        connection.execute("SET ROLE gatescope_strategy")
        connection.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="gate report is invalid|decision does not match|certification_gate_count"):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
                connection.execute(
                    """INSERT INTO strategy_execution_certifications
                         (id,tenant_id,strategy_version_ref,environment,decision,evidence_tier,
                          certification_policy_version,source_artifact_digest,
                          artifact_computed_at,gate_report_json,reason_codes_json,
                          assumptions_json,certification_digest,approved_by,approved_at,
                          expires_at,created_at)
                       VALUES ('50000000-0000-0000-0000-000000000002',%s,%s,'TESTNET',
                         'CERTIFY','LIVE_ELIGIBLE',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                    (
                        TENANT_A,
                        decision.strategy_version_ref,
                        decision.policy_version,
                        decision.source_artifact_digest,
                        decision.artifact_computed_at,
                        Jsonb(malformed_report),
                        Jsonb([]),
                        Jsonb(list(decision.assumptions)),
                        decision.certification_digest,
                        USER_A,
                        decision.decided_at,
                        decision.decided_at + timedelta(days=30),
                    ),
                )


def test_api_role_cannot_forge_noninitial_testnet_event(migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as connection:
        connection.execute("SET ROLE gatescope_api")
        connection.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
                connection.execute(
                    """INSERT INTO testnet_execution_events
                         (id,tenant_id,drill_id,sequence,event_type,payload_json,event_digest,
                          occurred_at,recorded_at)
                       VALUES ('40000000-0000-0000-0000-000000000001',%s,
                         '40000000-0000-0000-0000-000000000002',2,'POSITION_PROTECTED','{}',
                         %s,now(),now())""",
                    (TENANT_A, "f" * 64),
                )
