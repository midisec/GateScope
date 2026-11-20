from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_exchange_accounts import PostgresExchangeAccountRepository
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from gatescope.adapters.postgres_strategy_certifications import PostgresStrategyCertificationRepository
from gatescope.adapters.postgres_testnet_execution import (
    PostgresTestnetExecutionRepository,
    TestnetDrillConflict as DrillConflict,
    TestnetOutcomeRecord as Outcome,
)
from gatescope.application.credential_validation import CredentialValidationMonitor
from gatescope.credential_crypto import CredentialMaterial
from gatescope.domain.paper_trading import PaperSide, SignalTerms
from gatescope.quant import LiveEligibilityGateEvaluator, LiveEligibilityPolicy
from tests.integration.test_exchange_credential_admission import _crypto_pair
from tests.integration.test_paper_trade_ledger import _god, _insert_bar
from tests.integration.test_postgres_rls import INSTRUMENT, TENANT_A, USER_A, seed_two_tenants
from tests.integration.test_testnet_execution_admission import ReadyProbe
from tests.unit.test_live_eligibility import _artifact


WORKER = "testnet-recovery-worker"
SUBJECT = uuid5(NAMESPACE_URL, WORKER)


@pytest.fixture
def recovery_drill(migrated_database: str):
    seed_two_tenants(migrated_database)
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=121)
    with psycopg.connect(migrated_database) as connection:
        for index in range(120):
            _insert_bar(connection, base + timedelta(minutes=index))
    now = base + timedelta(minutes=120, seconds=5)
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        paper = PostgresPaperRepository(pool)
        paper_account = paper.create_account(
            TENANT_A, USER_A, name="Recovery source", base_currency="USDT", initial_equity=Decimal("10000"),
        )
        signal = paper.record_god_signal(
            TENANT_A, USER_A, instrument_id=INSTRUMENT, timeframe="1m", strategy_version_ref="recovery-v1",
            god_decision=_god(now), terms=SignalTerms(PaperSide.LONG, Decimal("100"), Decimal("95"), Decimal("110"), 10),
            indicator_snapshot={"rsi": {"value": "55", "version": "1.0.0"}},
        )
        decision = paper.create_execution_decision(
            TENANT_A, USER_A, signal.id, paper_account_id=paper_account.id,
            client_request_id="recovery-decision-0001", now=now,
        )
        proposal = paper.create_proposal(
            TENANT_A, USER_A, execution_decision_id=decision.id,
            client_request_id="recovery-proposal-0001", now=now,
        )
        approved = paper.approve_proposal(
            TENANT_A, USER_A, proposal.id, expected_version=proposal.version,
            idempotency_key="recovery-approval-0001", now=now + timedelta(seconds=1),
        )
        assert approved.trade is not None
        encryptor, decryptor = _crypto_pair()
        accounts = PostgresExchangeAccountRepository(pool)
        account, _ = accounts.create_account(
            TENANT_A, USER_A, alias="Recovery TESTNET",
            material=CredentialMaterial("integration-key-123456", "integration-secret-123456"),
            encryptor=encryptor, no_withdrawal_attested=True,
        )
        CredentialValidationMonitor(
            accounts, decryptor, ReadyProbe(), worker_id=WORKER, worker_subject_id=SUBJECT,
        ).run_once()
        validated = accounts.get_account(TENANT_A, USER_A, account.id)
        accounts.arm_testnet_execution(
            TENANT_A, USER_A, account.id, expected_etag=validated.etag,
            ip_whitelist_attested=True, no_withdrawal_attested=True, exclusive_account_attested=True,
        )
        certification = LiveEligibilityGateEvaluator().evaluate(
            _artifact(strategy_version_ref="recovery-v1", computed_at=now),
            LiveEligibilityPolicy("recovery-cert-v1"), decided_at=now,
        )
        PostgresStrategyCertificationRepository(pool).record_decision(
            TENANT_A, USER_A, approved_by=USER_A, decision=certification, validity=timedelta(days=30),
        )
        repository = PostgresTestnetExecutionRepository(pool)
        drill = repository.create_drill(
            TENANT_A, USER_A, paper_public_trade_id=approved.trade.public_trade_id,
            exchange_account_id=account.id, idempotency_key="recovery-drill-0001",
        )
        yield repository, accounts, drill
    finally:
        pool.close()


def test_pending_exposure_remains_claimable_and_frozen_until_reverified(recovery_drill, migrated_database: str) -> None:
    repository, accounts, drill = recovery_drill
    execution = repository.claim_next(TENANT_A, SUBJECT, worker_id=WORKER)
    assert execution is not None
    journal = repository.journal(execution, SUBJECT, worker_id=WORKER)
    journal.append("STOP_LOSS_PREPARED", {"request": {"trigger_price": "95"}})
    journal.append("TAKE_PROFIT_PREPARED", {"request": {"trigger_price": "110"}})
    repository.complete(
        execution, SUBJECT, worker_id=WORKER,
        outcome=Outcome("PROTECTED", "PROTECTION_VERIFIED", "101", 10, "200", "201", None, False),
    )
    monitored = repository.claim_protected(TENANT_A, SUBJECT, worker_id=WORKER)
    assert monitored is not None
    repository.complete_protection_check(
        monitored, SUBJECT, worker_id=WORKER,
        outcome=Outcome("RECONCILIATION_PENDING", "PROTECTION_QUERY_RETRY_PENDING", None, 10, "200", "201", None, True),
    )
    current = repository.get_drill(TENANT_A, USER_A, drill.public_drill_id)
    assert current.state == "RECONCILIATION_PENDING" and current.next_reconcile_at is not None and current.frozen
    account = accounts.get_account(TENANT_A, USER_A, drill.exchange_account_id)
    assert account.status == "EXECUTION_FROZEN" and not account.trading_ready
    with pytest.raises(DrillConflict):
        repository.create_drill(
            TENANT_A, USER_A, paper_public_trade_id=drill.paper_public_trade_id,
            exchange_account_id=drill.exchange_account_id, idempotency_key="recovery-second-drill-0002",
        )
    with psycopg.connect(migrated_database) as connection:
        connection.execute("UPDATE testnet_execution_drills SET next_reconcile_at=now()-interval '1 second' WHERE id=%s", (drill.id,))
    assert TENANT_A in repository.due_tenants()
    retry = repository.claim_protected(TENANT_A, SUBJECT, worker_id=WORKER)
    assert retry is not None and retry.stop_order_id == "200" and retry.filled_contracts == 10
    assert repository.claim_protected(TENANT_A, SUBJECT, worker_id="other-worker") is None
    repository.protection_journal(retry, SUBJECT, worker_id=WORKER).append("RECOVERY_FACT_OBSERVED", {"safe": True})
    repository.complete_protection_check(
        retry, SUBJECT, worker_id=WORKER,
        outcome=Outcome("PROTECTED", "PROTECTION_REVERIFIED", None, 10, "200", "201", None, False),
    )
    assert repository.get_drill(TENANT_A, USER_A, drill.public_drill_id).state == "PROTECTED"
    account = accounts.get_account(TENANT_A, USER_A, drill.exchange_account_id)
    assert account.status == "TESTNET_BUSY" and not account.trading_ready


def test_expired_worker_lease_recovers_prepared_identities_without_reclaiming_entry(recovery_drill, migrated_database: str) -> None:
    repository, accounts, drill = recovery_drill
    execution = repository.claim_next(TENANT_A, SUBJECT, worker_id=WORKER)
    assert execution is not None
    journal = repository.journal(execution, SUBJECT, worker_id=WORKER)
    journal.append("ENTRY_RECONCILED", {"exchange_order_id": "101", "filled_contracts": 10})
    journal.append("STOP_LOSS_PREPARED", {"request": {"trigger_price": "95"}})
    journal.append("STOP_LOSS_ACKNOWLEDGED", {"exchange_order_id": "200"})
    journal.append("FAIL_SAFE_EXIT_PREPARED", {"request": {"client_text": "t-gs-prepared-exit", "contracts": 10}})
    with psycopg.connect(migrated_database) as connection:
        connection.execute("UPDATE testnet_execution_drills SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (drill.id,))
    assert repository.claim_next(TENANT_A, SUBJECT, worker_id="restarted") is None
    current = repository.get_drill(TENANT_A, USER_A, drill.public_drill_id)
    assert current.state == "RECONCILIATION_PENDING" and current.next_reconcile_at is not None
    assert accounts.get_account(TENANT_A, USER_A, drill.exchange_account_id).status == "EXECUTION_FROZEN"
    recovery = repository.claim_protected(TENANT_A, SUBJECT, worker_id="restarted")
    assert recovery is not None and recovery.stop_order_id == "200" and recovery.filled_contracts == 10
    assert any(item["payload"].get("request", {}).get("client_text") == "t-gs-prepared-exit" for item in recovery.execution_events)
    with pytest.raises(DrillConflict, match="lease was lost"):
        journal.append("ENTRY_PREPARED", {"request": {}})
