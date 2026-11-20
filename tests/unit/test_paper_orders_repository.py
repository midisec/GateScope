from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_paper import PaperPreconditionFailed
from gatescope.adapters.postgres_paper_orders import (
    PostgresPaperOrderRepository, _decimal, _integer, _json, _now, normalized_request, request_hash,
)


def opening(**overrides):
    request = {"account_id": "10000000-0000-0000-0000-000000000001", "instrument_id": "20000000-0000-0000-0000-000000000002",
               "symbol": "BTC_USDT", "side": "LONG", "contracts": 3, "stop_price": "95", "take_profit_price": "120",
               "idempotency_key": "test-request-1"}
    request.update(overrides)
    return request


@pytest.mark.parametrize("field,value", [("contracts", True), ("contracts", 1.5), ("contracts", 0), ("contracts", -2), ("contracts", "1"),
    ("contracts", 10**12+1), ("side", "BUY"), ("side", ""), ("timeframe", "2m"), ("order_type", "STOP"),
    ("time_in_force", "FOK"), ("time_in_force", "GTC"), ("stop_price", None), ("stop_price", "NaN"),
    ("take_profit_price", "Infinity"), ("stop_price", "0"), ("max_slippage_bps", 101), ("max_slippage_bps", -1),
    ("ttl_seconds", 86401), ("ttl_seconds", 0), ("source", "EXCHANGE"), ("idempotency_key", "short"),
    ("idempotency_key", "unsafe\nkey"), ("limit_price", "100"), ("environment", "LIVE")])
def test_invalid_order_parameters_fail_before_database(field, value):
    with pytest.raises((ValueError, KeyError)):
        normalized_request(opening(**{field: value}))


def test_normalization_is_full_effective_payload_and_defaults_are_stable():
    result = normalized_request(opening())
    assert result["time_in_force"] == "IOC" and result["effect"] == "OPEN"
    assert request_hash(result) == request_hash(normalized_request(opening(time_in_force="IOC", max_slippage_bps=50, timeframe="15m", source="MCP")))
    for field, value in (("contracts", 4), ("stop_price", "94"), ("source", "WEB"), ("ttl_seconds", 60)):
        assert request_hash(normalized_request(opening(**{field: value}))) != request_hash(result)
    assert "environment" not in result


@pytest.mark.parametrize("tif", ["IOC", "GTC", "FOK"])
def test_limit_order_normalization(tif):
    result = normalized_request(opening(order_type="LIMIT", time_in_force=tif, limit_price="99.5"))
    assert result["limit_price"] == "99.5" and result["time_in_force"] == tif


def test_close_names_exact_lot_and_rejects_protection_changes():
    position = uuid4()
    request = {"contracts": 1, "idempotency_key": "close-request"}
    result = normalized_request(request, effect="CLOSE", position_id=position)
    assert result["position_id"] == str(position)
    assert result["effect"] == "CLOSE"
    assert request_hash(result) != request_hash(normalized_request(request, effect="CLOSE", position_id=uuid4()))
    with pytest.raises(ValueError):
        normalized_request({**request, "side": "LONG"}, effect="CLOSE", position_id=position)
    all_available = normalized_request({"idempotency_key": "full-close-request"}, effect="CLOSE", position_id=position)
    assert all_available["contracts"] is None


def test_quote_freshness_and_dimension_failure_are_not_fake_fills():
    at = datetime.now(timezone.utc)
    quote = SimpleNamespace(spec=SimpleNamespace(symbol="BTC_USDT"), book=SimpleNamespace(symbol="BTC_USDT", source_at=at), metadata_as_of=at)
    PostgresPaperOrderRepository._quote(quote, "BTC_USDT", at)
    for candidate, symbol, now in ((None, "BTC_USDT", at), (quote, "SOL_USDT", at), (quote, "BTC_USDT", at+timedelta(seconds=6)), (quote, "BTC_USDT", at-timedelta(seconds=3))):
        with pytest.raises(PaperPreconditionFailed):
            PostgresPaperOrderRepository._quote(candidate, symbol, now)
    quote.metadata_as_of = at-timedelta(seconds=61)
    with pytest.raises(PaperPreconditionFailed, match="METADATA_STALE"):
        PostgresPaperOrderRepository._quote(quote, "BTC_USDT", at)


def test_budget_preserves_legacy_positions_fees_and_unfilled_reservations():
    class Connection:
        rows = iter(((100, 3, 1), (200, 4), (300, 5, 2)))

        def execute(self, query, args):
            assert "tenant_id=%s" in query and "owner_user_id=%s" in query
            return SimpleNamespace(fetchone=lambda: next(self.rows))

    account = {"id": uuid4(), "initial_equity": Decimal(10000), "realized_pnl": Decimal(-10),
               "risk_fraction": Decimal(".0025"), "max_notional_fraction": Decimal(".1")}
    balance = PostgresPaperOrderRepository.budgets(Connection(), uuid4(), uuid4(), account)
    assert balance["cash_balance"] == 9990
    assert balance["position_margin"] == 300
    assert balance["order_reserved_margin"] == 300
    assert balance["available_balance"] == 9387
    assert balance["risk_used"] == 12
    assert balance["notional_used"] == 600
    assert balance["legacy_open_fees"] == 1


def test_json_never_rounds_decimals_and_naive_clock_rejected():
    at = datetime.now(timezone.utc)
    assert _json({"amount": Decimal("0.123456789012345678"), "at": at}) == {"amount": "0.123456789012345678", "at": at.isoformat()}
    assert _decimal("0.001", "price") == Decimal(".001")
    assert _integer(1, "quantity") == 1
    with pytest.raises(ValueError):
        _now(datetime(2026, 9, 14))


def test_detail_queries_bound_fills_and_events_by_the_same_snapshot(monkeypatch):
    at = datetime(2026, 9, 14, tzinfo=timezone.utc)
    queries = []
    pool = SimpleNamespace(read_transaction=lambda *args: nullcontext(object()))
    monkeypatch.setattr("gatescope.adapters.postgres_paper_orders._one", lambda *args: {"id": uuid4(), "decision_snapshot_id": None})
    def many(connection, query, params):
        queries.append((query, params))
        return []
    monkeypatch.setattr("gatescope.adapters.postgres_paper_orders._many", many)
    PostgresPaperOrderRepository(pool).get_order(uuid4(), uuid4(), uuid4(), now=at)
    assert len(queries) == 2 and all(params[-1] == at for _, params in queries)
    assert "filled_at<=%s" in queries[0][0] and "created_at<=%s" in queries[1][0]


def test_public_projection_does_not_return_internal_owner_or_hash():
    assert PostgresPaperOrderRepository._public({"id": 1, "owner_user_id": "secret", "tenant_id": "private", "request_hash": "digest", "request_json": {"private": True}}) == {"id": 1}
