"""Deterministic PAPER-only sizing and triple-barrier simulation.

This module deliberately has no exchange or network port.  It turns frozen
signal terms and a frozen paper risk policy into exact terms, then evaluates
later final bars with a conservative, pre-registered ambiguity policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Iterable, Literal


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
BASE_QUANTUM = Decimal("0.00000001")


class PaperSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PaperCloseReason(str, Enum):
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    TIME_STOP = "TIME_STOP"


@dataclass(frozen=True)
class SignalTerms:
    side: PaperSide
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    time_stop_bars: int

    def __post_init__(self) -> None:
        prices = (self.entry_reference, self.stop_price, self.take_profit_price)
        if any(not value.is_finite() or value <= ZERO for value in prices):
            raise ValueError("signal prices must be finite and positive")
        if not 1 <= self.time_stop_bars <= 10_000:
            raise ValueError("time_stop_bars must be between 1 and 10,000")
        if self.side is PaperSide.LONG:
            valid = self.stop_price < self.entry_reference < self.take_profit_price
        else:
            valid = self.take_profit_price < self.entry_reference < self.stop_price
        if not valid:
            raise ValueError("signal barriers do not match the position side")


@dataclass(frozen=True)
class PaperRiskPolicy:
    policy_version: str
    equity: Decimal
    risk_fraction: Decimal = Decimal("0.0025")
    max_notional_fraction: Decimal = Decimal("0.10")
    fee_bps_per_side: Decimal = Decimal("5")
    market_slippage_bps: Decimal = Decimal("3")
    max_entry_drift_bps: Decimal = Decimal("50")
    quantity_quantum: Decimal = BASE_QUANTUM

    def __post_init__(self) -> None:
        if not self.policy_version or len(self.policy_version) > 80:
            raise ValueError("policy_version must contain 1-80 characters")
        decimals = (
            self.equity,
            self.risk_fraction,
            self.max_notional_fraction,
            self.fee_bps_per_side,
            self.market_slippage_bps,
            self.max_entry_drift_bps,
            self.quantity_quantum,
        )
        if any(not value.is_finite() for value in decimals):
            raise ValueError("paper risk policy values must be finite")
        if self.equity <= ZERO:
            raise ValueError("paper equity must be positive")
        if not ZERO < self.risk_fraction <= Decimal("0.005"):
            raise ValueError("risk_fraction must be in (0, 0.005]")
        if not ZERO < self.max_notional_fraction <= Decimal("0.10"):
            raise ValueError("max_notional_fraction must be in (0, 0.10]")
        if not ZERO <= self.fee_bps_per_side <= Decimal("100"):
            raise ValueError("fee_bps_per_side must be in [0, 100]")
        if not ZERO <= self.market_slippage_bps <= Decimal("100"):
            raise ValueError("market_slippage_bps must be in [0, 100]")
        if not ZERO < self.max_entry_drift_bps <= Decimal("100"):
            raise ValueError("max_entry_drift_bps must be in (0, 100]")
        if self.quantity_quantum <= ZERO:
            raise ValueError("quantity_quantum must be positive")


@dataclass(frozen=True)
class PaperExactTerms:
    side: PaperSide
    quantity: Decimal
    quantity_unit: Literal["BASE"]
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    time_stop_bars: int
    risk_budget: Decimal
    worst_case_loss: Decimal
    max_notional: Decimal
    fee_bps_per_side: Decimal
    market_slippage_bps: Decimal
    max_entry_drift_bps: Decimal
    same_bar_policy: Literal["STOP_FIRST"]
    sizing_policy_version: str
    digest: str

    def __post_init__(self) -> None:
        SignalTerms(
            self.side,
            self.entry_reference,
            self.stop_price,
            self.take_profit_price,
            self.time_stop_bars,
        )
        values = (
            self.quantity,
            self.risk_budget,
            self.worst_case_loss,
            self.max_notional,
            self.fee_bps_per_side,
            self.market_slippage_bps,
            self.max_entry_drift_bps,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("exact PAPER terms must be finite")
        if self.quantity <= ZERO or self.risk_budget <= ZERO or self.max_notional <= ZERO:
            raise ValueError("exact PAPER quantity and caps must be positive")
        if self.worst_case_loss < ZERO or self.worst_case_loss > self.risk_budget:
            raise ValueError("exact PAPER worst-case loss exceeds its risk budget")
        if self.quantity * self.entry_reference > self.max_notional:
            raise ValueError("exact PAPER notional exceeds its hard cap")
        if not ZERO <= self.fee_bps_per_side <= Decimal("100"):
            raise ValueError("exact PAPER fee is outside supported bounds")
        if not ZERO <= self.market_slippage_bps <= Decimal("100"):
            raise ValueError("exact PAPER slippage is outside supported bounds")
        if not ZERO < self.max_entry_drift_bps <= Decimal("100"):
            raise ValueError("exact PAPER entry drift is outside supported bounds")
        if self.quantity_unit != "BASE" or self.same_bar_policy != "STOP_FIRST":
            raise ValueError("unsupported exact PAPER execution convention")
        if not self.sizing_policy_version or len(self.sizing_policy_version) > 120:
            raise ValueError("invalid exact PAPER sizing policy version")
        if len(self.digest) != 64 or any(character not in "0123456789abcdef" for character in self.digest):
            raise ValueError("invalid exact PAPER digest")


@dataclass(frozen=True)
class PaperBar:
    open_time: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    revision: int = 1
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("paper bar timestamps must include a timezone")
        if self.available_at.astimezone(timezone.utc) < self.open_time.astimezone(timezone.utc):
            raise ValueError("bar cannot be available before it opens")
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= ZERO for value in prices):
            raise ValueError("bar prices must be finite and positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high violates OHLC ordering")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low violates OHLC ordering")
        if self.revision < 1:
            raise ValueError("bar revision must be positive")


@dataclass(frozen=True)
class PaperExit:
    reason: PaperCloseReason
    trigger_price: Decimal
    fill_price: Decimal
    bar_open_time: datetime
    bar_available_at: datetime
    evaluated_bars: int
    ambiguity_policy_applied: bool


@dataclass(frozen=True)
class PaperPnl:
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    net_return_on_entry_notional: Decimal


def size_paper_terms(signal: SignalTerms, policy: PaperRiskPolicy) -> PaperExactTerms:
    """Size down to the policy grid using a conservative stop-loss budget."""

    slip = policy.market_slippage_bps / BPS
    entry_fill = signal.entry_reference * (
        ONE + slip if signal.side is PaperSide.LONG else ONE - slip
    )
    stop_fill = signal.stop_price * (
        ONE - slip if signal.side is PaperSide.LONG else ONE + slip
    )
    loss_per_unit = abs(entry_fill - stop_fill) + (
        (entry_fill + stop_fill) * policy.fee_bps_per_side / BPS
    )
    risk_budget = policy.equity * policy.risk_fraction
    max_notional = policy.equity * policy.max_notional_fraction
    by_risk = risk_budget / loss_per_unit
    # New legacy proposals also reserve room for an admitted adverse price drift
    # and execution slippage. Otherwise sizing exactly to the notional cap makes
    # the shared-ledger approval correctly reject almost every tight-stop setup.
    maximum_entry = signal.entry_reference * (ONE + policy.max_entry_drift_bps / BPS) * (ONE + slip)
    by_notional = max_notional / maximum_entry
    quantity = _round_down(min(by_risk, by_notional), policy.quantity_quantum)
    if quantity <= ZERO:
        raise ValueError("paper quantity rounds to zero")
    worst_case_loss = quantity * loss_per_unit
    if worst_case_loss > risk_budget or quantity * signal.entry_reference > max_notional:
        raise AssertionError("paper sizing exceeded a hard policy cap")
    canonical = {
        "entry_reference": _decimal(signal.entry_reference),
        "fee_bps_per_side": _decimal(policy.fee_bps_per_side),
        "market_slippage_bps": _decimal(policy.market_slippage_bps),
        "max_entry_drift_bps": _decimal(policy.max_entry_drift_bps),
        "max_notional": _decimal(max_notional),
        "quantity": _decimal(quantity),
        "quantity_unit": "BASE",
        "risk_budget": _decimal(risk_budget),
        "same_bar_policy": "STOP_FIRST",
        "side": signal.side.value,
        "sizing_policy_version": policy.policy_version,
        "stop_price": _decimal(signal.stop_price),
        "take_profit_price": _decimal(signal.take_profit_price),
        "time_stop_bars": signal.time_stop_bars,
        "worst_case_loss": _decimal(worst_case_loss),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PaperExactTerms(
        side=signal.side,
        quantity=quantity,
        quantity_unit="BASE",
        entry_reference=signal.entry_reference,
        stop_price=signal.stop_price,
        take_profit_price=signal.take_profit_price,
        time_stop_bars=signal.time_stop_bars,
        risk_budget=risk_budget,
        worst_case_loss=worst_case_loss,
        max_notional=max_notional,
        fee_bps_per_side=policy.fee_bps_per_side,
        market_slippage_bps=policy.market_slippage_bps,
        max_entry_drift_bps=policy.max_entry_drift_bps,
        same_bar_policy="STOP_FIRST",
        sizing_policy_version=policy.policy_version,
        digest=digest,
    )


def entry_drift_bps(reference: Decimal, observed: Decimal) -> Decimal:
    if not reference.is_finite() or not observed.is_finite() or reference <= ZERO or observed <= ZERO:
        raise ValueError("reference and observed prices must be finite and positive")
    return abs(observed - reference) / reference * BPS


def apply_entry_slippage(side: PaperSide, observed_price: Decimal, slippage_bps: Decimal) -> Decimal:
    _validate_price_and_bps(observed_price, slippage_bps)
    factor = slippage_bps / BPS
    return observed_price * (ONE + factor if side is PaperSide.LONG else ONE - factor)


def evaluate_paper_exit(
    terms: PaperExactTerms,
    bars: Iterable[PaperBar],
    *,
    entered_after: datetime,
) -> PaperExit | None:
    """Evaluate final bars strictly after the entry watermark.

    A bar touching both barriers is resolved STOP_FIRST because OHLC data does
    not reveal the intrabar path.  Favorable target gaps receive no price
    improvement; adverse stop gaps fill at the bar open plus market slippage.
    """

    if entered_after.tzinfo is None:
        raise ValueError("entered_after must include a timezone")
    eligible = sorted(
        (
            bar
            for bar in bars
            if bar.available_at.astimezone(timezone.utc)
            > entered_after.astimezone(timezone.utc)
        ),
        key=lambda item: (item.open_time, item.revision, item.payload_hash),
    )
    for count, bar in enumerate(eligible, start=1):
        if terms.side is PaperSide.LONG:
            stop_hit = bar.low <= terms.stop_price
            target_hit = bar.high >= terms.take_profit_price
        else:
            stop_hit = bar.high >= terms.stop_price
            target_hit = bar.low <= terms.take_profit_price

        if stop_hit:
            trigger = terms.stop_price
            if terms.side is PaperSide.LONG:
                raw_fill = min(terms.stop_price, bar.open)
                fill = raw_fill * (ONE - terms.market_slippage_bps / BPS)
            else:
                raw_fill = max(terms.stop_price, bar.open)
                fill = raw_fill * (ONE + terms.market_slippage_bps / BPS)
            return PaperExit(
                PaperCloseReason.SL_HIT,
                trigger,
                fill,
                bar.open_time,
                bar.available_at,
                count,
                ambiguity_policy_applied=target_hit,
            )
        if target_hit:
            return PaperExit(
                PaperCloseReason.TP_HIT,
                terms.take_profit_price,
                terms.take_profit_price,
                bar.open_time,
                bar.available_at,
                count,
                ambiguity_policy_applied=False,
            )
        if count >= terms.time_stop_bars:
            factor = terms.market_slippage_bps / BPS
            fill = bar.close * (ONE - factor if terms.side is PaperSide.LONG else ONE + factor)
            return PaperExit(
                PaperCloseReason.TIME_STOP,
                bar.close,
                fill,
                bar.open_time,
                bar.available_at,
                count,
                ambiguity_policy_applied=False,
            )
    return None


def paper_pnl(
    terms: PaperExactTerms,
    *,
    entry_fill_price: Decimal,
    exit_fill_price: Decimal,
) -> PaperPnl:
    if any(
        not value.is_finite() or value <= ZERO
        for value in (entry_fill_price, exit_fill_price)
    ):
        raise ValueError("fill prices must be finite and positive")
    direction = ONE if terms.side is PaperSide.LONG else Decimal("-1")
    gross = direction * (exit_fill_price - entry_fill_price) * terms.quantity
    fees = (
        (entry_fill_price + exit_fill_price)
        * terms.quantity
        * terms.fee_bps_per_side
        / BPS
    )
    net = gross - fees
    notional = entry_fill_price * terms.quantity
    return PaperPnl(gross, fees, net, net / notional)


def _round_down(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _validate_price_and_bps(price: Decimal, basis_points: Decimal) -> None:
    if not price.is_finite() or price <= ZERO:
        raise ValueError("price must be finite and positive")
    if not basis_points.is_finite() or not ZERO <= basis_points <= Decimal("100"):
        raise ValueError("basis points must be in [0, 100]")
