from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from gatescope.domain.paper_matching import (
    BookLevel, ContractSpec, OrderBook, exit_trigger, match_order, validate_price, validate_protection,
)

NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)


def spec(**kwargs):
    return replace(ContractSpec("SOL_USDT", D("0.1"), D("0.01"), D("0.1"), D("100000"), D("0.00075")), **kwargs)


def book(**kwargs):
    return replace(OrderBook("SOL_USDT", (BookLevel(D("99.99"), D("100")), BookLevel(D("99.90"), D("200"))),
                             (BookLevel(D("100.01"), D("100")), BookLevel(D("100.10"), D("200"))),
                             NOW, NOW, updated_at=NOW, sequence_id="123"), quote_id="", **kwargs)


def match(**kwargs):
    args = dict(side="BUY", contracts=15, order_type="MARKET", time_in_force="IOC", now=NOW)
    args.update(kwargs)
    return match_order(spec(), book(), **args)


def test_buy_sweeps_asks_ten_percent_and_computes_exact_fees():
    result = match()
    assert result.status == "FILLED"
    assert [(fill.price, fill.contracts) for fill in result.fills] == [(D("100.01"), 10), (D("100.10"), 5)]
    assert result.notional == D("150.060")
    assert result.fee == D("0.1125450")
    assert result.vwap == D("100.04")
    assert result.remaining_contracts == 0
    assert result.to_dict()["execution_mode"] == "PAPER"
    assert result.to_dict()["participation_rate"] == "0.10"


def test_sell_sweeps_bids_not_last_or_mark_price():
    result = match(side="SELL")
    assert [(fill.price, fill.contracts) for fill in result.fills] == [(D("99.99"), 10), (D("99.90"), 5)]
    assert result.notional == D("149.940")
    assert result.vwap == D("99.96")


def test_market_ioc_slippage_cap_cancels_remainder():
    result = match(max_slippage_bps=D("1"))
    assert result.status == "PARTIALLY_FILLED"
    assert result.filled_contracts == 10
    assert result.remaining_contracts == 5
    assert result.reason == "SLIPPAGE_CAP"
    assert result.remainder_cancelled and not result.resting


@pytest.mark.parametrize("side,limit", [("BUY", "99.99"), ("SELL", "100.01")])
def test_limit_gtc_inside_spread_does_not_invent_maker_fill(side, limit):
    result = match(side=side, order_type="LIMIT", time_in_force="GTC", limit_price=D(limit))
    assert result.status == "OPEN"
    assert result.fills == ()
    assert result.resting and not result.remainder_cancelled
    assert result.vwap is None and result.fee == 0


@pytest.mark.parametrize("time_in_force,filled,status,resting", [
    ("GTC", 10, "PARTIALLY_FILLED", True), ("IOC", 10, "PARTIALLY_FILLED", False), ("FOK", 0, "CANCELLED", False),
])
def test_limit_tif_partial_and_atomic_fok(time_in_force, filled, status, resting):
    result = match(order_type="LIMIT", time_in_force=time_in_force, limit_price=D("100.01"))
    assert result.status == status and result.filled_contracts == filled and result.resting == resting
    if time_in_force == "FOK":
        assert result.notional == 0 and result.fee == 0 and result.fills == ()


def test_limit_fok_full_depth_succeeds():
    assert match(order_type="LIMIT", time_in_force="FOK", limit_price=D("100.10")).status == "FILLED"


def test_shared_quote_liquidity_is_subtracted_after_participation():
    consumed = {D("100.01"): 10, D("100.10"): 18}
    result = match(consumed=consumed)
    assert result.filled_contracts == 2
    assert result.fills[0].price == D("100.10")
    assert consumed == {D("100.01"): 10, D("100.10"): 18}


def test_subinteger_usable_depth_is_not_rounded_up():
    depth = book(asks=(BookLevel(D("100.01"), D("9.99")),))
    result = match_order(spec(), depth, side="BUY", contracts=1, order_type="MARKET", time_in_force="IOC", now=NOW)
    assert result.filled_contracts == 0 and result.status == "CANCELLED"


@pytest.mark.parametrize("contracts", [True, False, 0, -1, D("1"), "1", 1.0, 100001])
def test_integer_contract_input_and_exchange_limits(contracts):
    with pytest.raises(ValueError, match="contracts"):
        match(contracts=contracts)


def test_separate_market_size_limit_and_decimal_exchange_minimum():
    with pytest.raises(ValueError, match="market-order"):
        match_order(spec(market_max_contracts=D("10")), book(), side="BUY", contracts=15,
                    order_type="MARKET", time_in_force="IOC", now=NOW)
    assert match(contracts=1).filled_contracts == 1


def test_existing_accepted_order_remainder_does_not_reapply_new_order_minimum():
    contract = spec(min_contracts=D("10"))
    args = dict(side="BUY", contracts=5, order_type="LIMIT", time_in_force="GTC", limit_price=D("100.10"), now=NOW)
    with pytest.raises(ValueError, match="contracts"):
        match_order(contract, book(), **args)
    result = match_order(contract, book(), remaining_order=True, **args)
    assert result.filled_contracts == 5


@pytest.mark.parametrize("kwargs", [{"remaining_order": 1}, {"remaining_order": True, "contracts": 100001},
                                     {"remaining_order": True, "contracts": 0}])
def test_existing_remainder_flag_does_not_bypass_integer_or_maximum_rules(kwargs):
    with pytest.raises(ValueError):
        match(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"order_type": "MARKET", "time_in_force": "GTC"},
    {"order_type": "MARKET", "time_in_force": "FOK"},
    {"order_type": "LIMIT"}, {"limit_price": D("100")},
    {"order_type": "LIMIT", "limit_price": D("100.001")},
    {"max_slippage_bps": D("NaN")}, {"max_slippage_bps": D("1001")},
    {"participation_rate": D("0")}, {"participation_rate": D("1.1")},
    {"side": "LONG"}, {"consumed": {D("100"): -1}}, {"consumed": {"100": 1}},
])
def test_invalid_order_values_fail_closed(kwargs):
    with pytest.raises(ValueError):
        match(**kwargs)


@pytest.mark.parametrize("age", [5.001, -2.001])
def test_old_or_future_clock_not_executable(age):
    with pytest.raises(ValueError, match="stale or future"):
        match(now=NOW + timedelta(seconds=age))


def test_both_local_and_server_clocks_are_checked():
    stale = book(source_at=NOW - timedelta(seconds=6), updated_at=NOW - timedelta(seconds=6))
    with pytest.raises(ValueError, match="stale"):
        stale.assert_fresh(NOW)


@pytest.mark.parametrize("kwargs", [
    {"bids": ()},
    {"bids": (BookLevel(D("100.01"), D("1")),)},
    {"asks": (BookLevel(D("100.1"), D("1")), BookLevel(D("100.01"), D("1")))},
    {"asks": (BookLevel(D("100.01"), D("1")), BookLevel(D("100.01"), D("1")))},
    {"source_at": NOW.replace(tzinfo=None)}, {"updated_at": NOW + timedelta(seconds=1)},
    {"sequence_id": "secret/not/sequence"},
])
def test_invalid_depth_rejected(kwargs):
    with pytest.raises(ValueError):
        book(**kwargs)


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("0"), D("-1"), 1.01, True])
def test_invalid_exact_depth_values_rejected(value):
    with pytest.raises(ValueError):
        BookLevel(value, D("10"))


def test_quote_hash_is_stable_when_same_book_is_observed_again():
    original = book()
    second = replace(original, quote_id="", captured_at=NOW + timedelta(seconds=1), source_at=NOW + timedelta(seconds=1))
    assert original.quote_id == second.quote_id
    assert replace(original, quote_id="", sequence_id="124").quote_id != original.quote_id
    with pytest.raises(ValueError, match="quote_id"):
        replace(original, quote_id="a" * 64)


def test_quote_id_does_not_mint_depth_when_decimal_string_format_changes():
    original = book()
    reformatted = replace(original, quote_id="", asks=(BookLevel(D("100.010"), D("100.00")),
                                                       BookLevel(D("100.100"), D("200.0"))))
    assert original.quote_id == reformatted.quote_id


def test_precision_is_not_silently_rounded_to_exchange_tick():
    with pytest.raises(ValueError, match="aligned"):
        validate_price(D("0.000001001"), D("0.00000001"))
    assert validate_price(D("999999999999999.00000001"), D("0.00000001")) == D("999999999999999.00000001")


@pytest.mark.parametrize("side,stop,target", [("LONG", "99", "101"), ("SHORT", "101", "99")])
def test_protection_sides(side, stop, target):
    validate_protection(side, D("100"), D(stop), D(target), D("0.01"))
    with pytest.raises(ValueError):
        validate_protection(side, D("100"), D(target), D(stop), D("0.01"))


@pytest.mark.parametrize("side,stop,target,expected", [
    ("LONG", "100", "110", "STOP_LOSS"), ("SHORT", "100", "90", "STOP_LOSS"),
    ("LONG", "90", "99", "TAKE_PROFIT"), ("SHORT", "110", "101", "TAKE_PROFIT"),
    ("LONG", "90", "110", None), ("SHORT", "110", "90", None),
])
def test_exit_uses_actual_bid_ask(side, stop, target, expected):
    assert exit_trigger(side, book(), stop_loss=D(stop), take_profit=D(target), now=NOW) == expected


def test_stop_gap_fill_does_not_invent_execution_at_stop_price():
    assert exit_trigger("LONG", book(), stop_loss=D("100"), take_profit=D("110"), now=NOW) == "STOP_LOSS"
    fill = match(side="SELL", contracts=1)
    assert fill.vwap == D("99.99") < D("100")


def test_latched_stop_survives_rebound_and_partial_exit():
    assert exit_trigger("LONG", book(), stop_loss=D("90"), take_profit=D("110"),
                        latched_reason="STOP_LOSS", now=NOW) == "STOP_LOSS"
    with pytest.raises(ValueError):
        exit_trigger("LONG", book(), stop_loss=None, take_profit=None, latched_reason="UNKNOWN", now=NOW)


def test_book_and_contract_identity_cannot_be_mixed():
    with pytest.raises(ValueError, match="identities"):
        match_order(spec(symbol="BTC_USDT"), book(), side="BUY", contracts=1,
                    order_type="MARKET", time_in_force="IOC", now=NOW)
