from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gatescope.adapters.gate_paper_market import MarketSnapshot
from gatescope.adapters.postgres_paper import PaperNotFound
from gatescope.application.paper_execution import PaperExecutionError, PaperExecutionService, PaperOrderRequest, execution_failure
from tests.unit.test_paper_matching import book, spec


def setup():
    account, instrument = uuid4(), uuid4()
    calls = []
    def quote(symbol):
        calls.append(symbol)
        now = datetime.now(timezone.utc)
        return MarketSnapshot(spec(), book(source_at=now, captured_at=now, updated_at=now), None, None, now)
    market = SimpleNamespace(get_instrument=lambda value: {"id": str(instrument), "symbol": "SOL_USDT",
                                                          "market": "futures_usdt", "status": "ACTIVE"})
    repo = SimpleNamespace(replay_request=lambda *a, **kw: None,
        submit=lambda *a, **kw: {"accepted": True}, account_snapshot=lambda *a: {"balance": {
            "available_balance": "10000", "notional_used": "0", "notional_limit": "1000", "risk_used": "0", "risk_limit": "100"}},
        get_position=lambda *a: {"symbol": "SOL_USDT"}, close=lambda *a, **kw: {"closed": True})
    accounts = SimpleNamespace(get_account=lambda *a: SimpleNamespace(fee_bps_per_side=Decimal("5"), status="ACTIVE", base_currency="USDT"))
    service = PaperExecutionService(repo, accounts, market, SimpleNamespace(get=quote))
    request = {"account_id": str(account), "instrument_id": str(instrument), "side": "LONG", "contracts": 1,
               "stop_price": "99", "take_profit_price": "110", "idempotency_key": "test-paper-execution-001"}
    return service, request, calls


def test_preview_is_real_model_without_creating_orders():
    service, request, calls = setup()
    service.repository.submit = lambda *a: pytest.fail("preview must not submit")
    result = service.preview(uuid4(), uuid4(), request)
    assert result["preview_only"] and not result["order_created"] and calls == ["SOL_USDT"]
    assert result["estimate"]["filled_contracts"] == 1
    assert result["market"]["spec"]["multiplier"] == "0.1"
    assert result["checks"]["budget_available"]
    assert result["checks"]["balance_reserved"] is False
    assert result["sizing"]["requested_base_quantity"] == "0.1"
    assert result["sizing"]["stop_loss_is_guaranteed"] is False


def test_preview_includes_higher_account_fees_and_budget_rejection_without_order():
    service, request, _ = setup()
    service.accounts.get_account = lambda *a: SimpleNamespace(fee_bps_per_side=Decimal("50"), status="ACTIVE", base_currency="USDT")
    service.repository.account_snapshot = lambda *a: {"balance": {"available_balance": "1", "notional_used": "0", "notional_limit": "1", "risk_used": "0", "risk_limit": ".001"}}
    service.repository.submit = lambda *a: pytest.fail("preview must not submit")
    result = service.preview(uuid4(), uuid4(), request)
    assert result["checks"]["reasons"] == ["INSUFFICIENT_PAPER_BALANCE", "ACCOUNT_NOTIONAL_LIMIT", "ACCOUNT_RISK_LIMIT"]
    assert result["sizing"]["effective_fee_rate"] == "0.005"
    assert Decimal(result["estimate"]["fee"]) == Decimal(result["estimate"]["notional"]) * Decimal(".005")
    assert sum(Decimal(fill["fee"]) for fill in result["estimate"]["fills"]) == Decimal(result["estimate"]["fee"])
    assert result["sizing"]["max_contracts_by_budget"] == 0


def test_limit_preview_reserves_full_request_even_when_nothing_can_fill():
    service, request, _ = setup()
    result = service.preview(uuid4(), uuid4(), {**request, "order_type": "LIMIT", "time_in_force": "GTC", "limit_price": "99.5"})
    assert result["estimate"]["filled_contracts"] == 0
    assert Decimal(result["sizing"]["required_margin"]) == Decimal("9.95")
    assert result["sizing"]["estimated_stop_loss"] is None


def test_replay_works_even_when_market_unavailable_and_never_fetches_again():
    service, request, calls = setup()
    service.repository.replay_request = lambda *a, **kw: {"replayed": True}
    assert service.submit(uuid4(), uuid4(), request) == {"replayed": True}
    assert service.close(uuid4(), uuid4(), uuid4(), {"idempotency_key": "paper-close-request-001"}) == {"replayed": True}
    assert not calls


def test_previous_order_can_be_queried_by_same_request_after_instrument_disabled():
    service, request, calls = setup()
    service.market.get_instrument = lambda _: {"market": "futures_usdt", "symbol": "SOL_USDT", "status": "INACTIVE"}
    service.repository.replay_request = lambda *a, **kw: {"replayed": True}
    assert service.submit(uuid4(), uuid4(), request)["replayed"]
    service.repository.replay_request = lambda *a, **kw: None
    with pytest.raises(PaperExecutionError):
        service.submit(uuid4(), uuid4(), request)
    assert not calls


def test_ownership_is_checked_before_public_provider_call():
    service, request, calls = setup()
    def denied(*args):
        raise PaperNotFound("other account")
    service.accounts.get_account = denied
    with pytest.raises(PaperNotFound):
        service.submit(uuid4(), uuid4(), request)
    assert not calls


@pytest.mark.parametrize("change", [{"contracts": True}, {"contracts": 1.2}, {"contracts": 0},
    {"stop_price": "NaN"}, {"max_slippage_bps": "101"}, {"order_type": "MARKET", "time_in_force": "GTC"},
    {"order_type": "LIMIT"}, {"ttl_seconds": 86401}, {"environment": "LIVE"}, {"owner_user_id": str(uuid4())}])
def test_invalid_requests_cannot_reach_execution(change):
    _, request, _ = setup()
    with pytest.raises(ValueError):
        PaperOrderRequest.model_validate({**request, **change})


def test_safe_errors_are_specific_and_private_detail_never_escapes():
    assert execution_failure(PermissionError("private")).status == 403
    assert execution_failure(ValueError("private")).status == 422
    service, request, _ = setup()
    def unavailable(*a):
        raise RuntimeError("SECRET private provider exception")
    service.quotes.get = unavailable
    with pytest.raises(PaperExecutionError) as caught:
        service.submit(uuid4(), uuid4(), request)
    assert caught.value.code == "PAPER_MARKET_UNAVAILABLE" and "SECRET" not in str(caught.value)
