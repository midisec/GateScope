from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gatescope.domain.paper_trading import (
    PaperBar,
    PaperCloseReason,
    PaperRiskPolicy,
    PaperSide,
    SignalTerms,
    apply_entry_slippage,
    entry_drift_bps,
    evaluate_paper_exit,
    paper_pnl,
    size_paper_terms,
)


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def long_terms() -> tuple[SignalTerms, PaperRiskPolicy]:
    return (
        SignalTerms(PaperSide.LONG, Decimal("100"), Decimal("95"), Decimal("110"), 3),
        PaperRiskPolicy("paper-risk-v1", Decimal("10000")),
    )


def bar(
    offset: int,
    *,
    opened: str = "100",
    high: str = "104",
    low: str = "98",
    close: str = "102",
) -> PaperBar:
    opened_at = NOW + timedelta(minutes=offset)
    return PaperBar(
        open_time=opened_at,
        available_at=opened_at + timedelta(minutes=1),
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        payload_hash=str(offset),
    )


def test_paper_sizing_honors_risk_and_notional_caps_and_is_stable() -> None:
    signal, policy = long_terms()
    first = size_paper_terms(signal, policy)
    second = size_paper_terms(signal, policy)
    assert first.digest == second.digest
    assert first.worst_case_loss <= first.risk_budget == Decimal("25.0000")
    assert first.quantity * first.entry_reference <= first.max_notional == Decimal("1000.00")
    assert first.quantity.as_tuple().exponent >= -8
    assert first.same_bar_policy == "STOP_FIRST"


def test_new_tight_stop_terms_reserve_adverse_drift_and_slippage_below_shared_notional_cap():
    signal = SignalTerms(PaperSide.LONG, Decimal("100"), Decimal("99.99"), Decimal("110"), 3)
    _, policy = long_terms()
    terms = size_paper_terms(signal, policy)
    maximum_entry = terms.entry_reference * (1 + policy.max_entry_drift_bps / 10000) * (1 + policy.market_slippage_bps / 10000)
    assert terms.quantity * maximum_entry <= terms.max_notional


def test_signal_barriers_must_match_direction() -> None:
    with pytest.raises(ValueError, match="barriers"):
        SignalTerms(PaperSide.LONG, Decimal("100"), Decimal("105"), Decimal("110"), 3)
    with pytest.raises(ValueError, match="barriers"):
        SignalTerms(PaperSide.SHORT, Decimal("100"), Decimal("95"), Decimal("90"), 3)


def test_same_bar_ambiguity_is_conservatively_stop_first() -> None:
    signal, policy = long_terms()
    terms = size_paper_terms(signal, policy)
    result = evaluate_paper_exit(
        terms,
        [bar(1, high="112", low="94", close="105")],
        entered_after=NOW,
    )
    assert result is not None
    assert result.reason is PaperCloseReason.SL_HIT
    assert result.ambiguity_policy_applied is True
    assert result.fill_price < terms.stop_price


def test_adverse_gap_and_favorable_gap_have_conservative_fills() -> None:
    signal, policy = long_terms()
    terms = size_paper_terms(signal, policy)
    stopped = evaluate_paper_exit(
        terms,
        [bar(1, opened="90", high="96", low="89", close="93")],
        entered_after=NOW,
    )
    assert stopped is not None
    assert stopped.reason is PaperCloseReason.SL_HIT
    assert stopped.fill_price < Decimal("90")

    target = evaluate_paper_exit(
        terms,
        [bar(1, opened="120", high="121", low="111", close="118")],
        entered_after=NOW,
    )
    assert target is not None
    assert target.reason is PaperCloseReason.TP_HIT
    assert target.fill_price == Decimal("110")


def test_time_stop_uses_only_bars_available_after_entry() -> None:
    signal, policy = long_terms()
    terms = size_paper_terms(signal, policy)
    result = evaluate_paper_exit(
        terms,
        [bar(-1), bar(1), bar(2), bar(3)],
        entered_after=NOW,
    )
    assert result is not None
    assert result.reason is PaperCloseReason.TIME_STOP
    assert result.evaluated_bars == 3
    assert result.bar_open_time == NOW + timedelta(minutes=3)


def test_short_pnl_and_entry_drift_are_directionally_correct() -> None:
    signal = SignalTerms(PaperSide.SHORT, Decimal("100"), Decimal("105"), Decimal("90"), 5)
    terms = size_paper_terms(signal, PaperRiskPolicy("paper-risk-v1", Decimal("10000")))
    entry = apply_entry_slippage(PaperSide.SHORT, Decimal("100"), Decimal("3"))
    result = paper_pnl(terms, entry_fill_price=entry, exit_fill_price=Decimal("90"))
    assert result.gross_pnl > 0
    assert result.fees > 0
    assert result.net_pnl < result.gross_pnl
    assert entry_drift_bps(Decimal("100"), Decimal("100.5")) == Decimal("50.000")
