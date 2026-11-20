from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import psycopg
import pytest

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PostgresPaperRepository
from gatescope.domain.paper_trading import PaperSide, SignalTerms
from gatescope.quant.evidence import GodDecision
from tests.integration.test_postgres_rls import (
    INSTRUMENT,
    TENANT_A,
    USER_A,
    seed_two_tenants,
)


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _insert_bar(
    connection: psycopg.Connection[tuple[object, ...]],
    opened_at: datetime,
    *,
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> None:
    connection.execute(
        """INSERT INTO bars
             (venue,market,instrument_id,timeframe,open_time,close_time,open,high,low,close,
              volume,is_final,source_sequence,revision,available_at,payload_hash,quality_flags,created_at)
           VALUES ('GATE','FUTURES_USDT',%s,'1m',%s,%s,100,%s,%s,%s,10,true,%s,1,%s,%s,'{}',now())""",
        (
            INSTRUMENT,
            opened_at,
            opened_at + timedelta(minutes=1),
            Decimal(high),
            Decimal(low),
            Decimal(close),
            opened_at.isoformat(),
            opened_at + timedelta(minutes=1),
            f"hash-{opened_at.isoformat()}",
        ),
    )


def _god(decision_at: datetime) -> GodDecision:
    return GodDecision(
        decision="PAPER_ELIGIBLE",
        evidence_tier="PAPER",
        reason_codes=(),
        independent_blocks=250,
        win_probability_estimate=0.7,
        win_probability_lcb=0.61,
        mean_net_return=0.01,
        expected_net_return_lcb=0.002,
        alpha_per_hypothesis=0.01,
        decision_at=decision_at,
        policy_version="paper-evidence-v1",
        assumptions=("sealed fixture",),
        evidence_digest="a" * 64,
    )


def test_frozen_signal_proposal_approval_and_paper_exit(
    migrated_database: str,
) -> None:
    seed_two_tenants(migrated_database)
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=120)
    with psycopg.connect(migrated_database) as connection:
        for index in range(120):
            _insert_bar(connection, base + timedelta(minutes=index))
    decision_at = base + timedelta(minutes=120, seconds=5)
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresPaperRepository(pool)
        account = repository.create_account(
            TENANT_A,
            USER_A,
            name="Integration PAPER",
            base_currency="USDT",
            initial_equity=Decimal("10000"),
        )
        signal = repository.record_god_signal(
            TENANT_A,
            USER_A,
            instrument_id=INSTRUMENT,
            timeframe="1m",
            strategy_version_ref="fixture-strategy-v1",
            god_decision=_god(decision_at),
            terms=SignalTerms(
                PaperSide.LONG,
                Decimal("100"),
                Decimal("95"),
                Decimal("110"),
                10,
            ),
            indicator_snapshot={"rsi": {"value": "55", "version": "1.0.0"}},
        )
        original = repository.signal_chart(TENANT_A, USER_A, signal.id)
        assert len(original["bars"]) == 120
        execution = repository.create_execution_decision(
            TENANT_A,
            USER_A,
            signal.id,
            paper_account_id=account.id,
            client_request_id="integration-decision-0001",
            now=decision_at,
        )
        assert execution.decision == "ELIGIBLE"
        assert execution.environment == "PAPER"
        proposal = repository.create_proposal(
            TENANT_A,
            USER_A,
            execution_decision_id=execution.id,
            client_request_id="integration-proposal-0001",
            now=decision_at,
        )
        approved = repository.approve_proposal(
            TENANT_A,
            USER_A,
            proposal.id,
            expected_version=proposal.version,
            idempotency_key="integration-approval-0001",
            now=decision_at + timedelta(seconds=1),
        )
        assert approved.outcome == "APPROVE"
        assert approved.trade is not None
        assert approved.trade.state == "OPEN"

        exit_open = base + timedelta(minutes=120)
        with psycopg.connect(migrated_database) as connection:
            _insert_bar(connection, exit_open, high="111", low="99", close="110")
        closed = repository.reconcile_trade(
            TENANT_A,
            USER_A,
            approved.trade.public_trade_id,
            now=exit_open + timedelta(minutes=1, seconds=1),
        )
        assert closed.state == "CLOSED"
        assert closed.close_reason == "TP_HIT"
        assert closed.net_pnl is not None and closed.net_pnl > 0
        assert len(repository.signal_chart(TENANT_A, USER_A, signal.id)["bars"]) == 120
        timeline = repository.trade_timeline(
            TENANT_A, USER_A, approved.trade.public_trade_id,
        )
        assert [event["sequence"] for event in timeline] == [1, 2, 3, 4]
        assert all(
            timeline[index]["previous_digest"] == timeline[index - 1]["event_digest"]
            for index in range(1, len(timeline))
        )
    finally:
        pool.close()


def test_paper_worker_role_is_rls_bound_and_cannot_read_credentials(
    migrated_database: str,
) -> None:
    seed_two_tenants(migrated_database)
    with psycopg.connect(migrated_database) as connection:
        connection.execute("SET ROLE gatescope_paper")
        connection.commit()
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
            connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
            assert connection.execute("SELECT count(*) FROM paper_accounts").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT count(*) FROM mcp_tokens").fetchone()
