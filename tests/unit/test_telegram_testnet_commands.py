from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from apps.api.formal_app import _telegram_command_response
from gatescope.adapters.postgres_testnet_execution import TestnetDrill as DrillRecord


NOW = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
DRILL_ID = "testnet_00000000000000000000000000"


class FakeTestnetRepository:
    def __init__(self) -> None:
        self.drill = DrillRecord(
            id=UUID("10000000-0000-0000-0000-000000000001"),
            public_drill_id=DRILL_ID,
            owner_user_id=UUID("10000000-0000-0000-0000-000000000002"),
            paper_trade_id=UUID("10000000-0000-0000-0000-000000000003"),
            paper_public_trade_id="paper_00000000000000000000000000",
            exchange_account_id=UUID("10000000-0000-0000-0000-000000000004"),
            exchange_alias="isolated",
            instrument_id=UUID("10000000-0000-0000-0000-000000000005"),
            contract="BTC_USDT",
            side="LONG",
            exact_terms={
                "entry_reference": "60000",
                "quantity": "0.001",
                "stop_price": "59000",
                "take_profit_price": "62000",
            },
            exact_terms_digest="a" * 64,
            state="PROTECTED",
            safe_code="STOP_AND_TAKE_PROFIT_VERIFIED",
            entry_order_id="101",
            filled_contracts=1,
            stop_order_id="102",
            take_profit_order_id="103",
            fail_safe_order_id=None,
            frozen=False,
            version=2,
            created_at=NOW,
            started_at=NOW,
            completed_at=None,
            next_reconcile_at=NOW,
            updated_at=NOW,
        )

    def list_drills(self, tenant_id: UUID, subject_id: UUID, *, limit: int):
        assert limit == 5
        return [self.drill]

    def get_drill(self, tenant_id: UUID, subject_id: UUID, public_drill_id: str):
        assert public_drill_id == DRILL_ID
        return self.drill

    def timeline(self, tenant_id: UUID, subject_id: UUID, public_drill_id: str):
        return [{"sequence": 4, "event_type": "PROTECTION_VERIFIED", "occurred_at": NOW}]


def _response(command: str) -> str:
    identity = UUID("10000000-0000-0000-0000-000000000002")
    return _telegram_command_response(
        object(),  # type: ignore[arg-type]
        FakeTestnetRepository(),  # type: ignore[arg-type]
        identity,
        identity,
        command,
        "https://gatescope.example",
    )


def test_testnet_list_command_is_compact_and_explicit_about_environment() -> None:
    response = _response("/testnets")
    assert DRILL_ID in response
    assert "Gate TESTNET" in response
    assert "PROTECTED" in response


def test_testnet_detail_command_returns_protection_ledger_without_mutation_controls() -> None:
    response = _response(f"/testnet {DRILL_ID}")
    assert "STOP_AND_TAKE_PROFIT_VERIFIED" in response
    assert "#4 PROTECTION_VERIFIED" in response
    assert "止盈：62000" in response
    assert "此查询只读" in response


def test_testnet_detail_rejects_unbounded_identifier() -> None:
    assert "格式不正确" in _response("/testnet live_123")
