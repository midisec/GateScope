"""Conservative PAPER fills against observed Gate USDT contract depth.

This module cannot send exchange orders. All amounts are exact Decimal values;
integer contracts are intentionally a subset of Gate's supported order sizes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from functools import wraps
from typing import Mapping

MODEL_VERSION = "gate-depth-paper-1"
PARTICIPATION_RATE = Decimal("0.10")
MAX_BOOK_AGE_SECONDS = 5
MAX_FUTURE_SKEW_SECONDS = 2
ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
DISCLAIMER = "PAPER estimate, not an exchange execution or queue-position guarantee."


def _calculation_precision(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 100
            return function(*args, **kwargs)
    return wrapped


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be an exact decimal") from None
    if not result.is_finite() or (positive and result <= ZERO):
        raise ValueError(f"{name} must be finite and positive" if positive else f"{name} must be finite")
    if result and (result.adjusted() > 30 or result.adjusted() < -30):
        raise ValueError(f"{name} outside supported decimal range")
    if len(result.as_tuple().digits) > 38:
        raise ValueError(f"{name} exceeds supported precision")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    result = value.astimezone(timezone.utc)
    if not 2000 <= result.year <= 2100:
        raise ValueError(f"{name} outside supported timestamp range")
    return result


def validate_symbol(symbol: str) -> str:
    # Preserve exact Unicode contract names; reject paths, escapes and controls.
    if (not isinstance(symbol, str) or not 6 <= len(symbol) <= 80
            or len(symbol.encode("utf-8")) > 240 or not symbol.endswith("_USDT")
            or not all(character.isalnum() or character == "_" for character in symbol)
            or not symbol[:-5] or symbol.startswith("_")):
        raise ValueError("unsupported Gate USDT contract symbol")
    return symbol


def _canonical_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    multiplier: Decimal
    price_tick: Decimal
    min_contracts: Decimal
    max_contracts: Decimal
    taker_fee_rate: Decimal
    market_max_contracts: Decimal | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        for name in ("multiplier", "price_tick", "min_contracts", "max_contracts"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True))
        fee = _decimal(self.taker_fee_rate, "taker_fee_rate")
        if not ZERO <= fee <= Decimal("0.01"):
            raise ValueError("unsupported taker fee rate")
        object.__setattr__(self, "taker_fee_rate", fee)
        if self.min_contracts > self.max_contracts or self.max_contracts > Decimal("1000000000000"):
            raise ValueError("unsupported contract size bounds")
        if self.market_max_contracts is not None:
            market_max = _decimal(self.market_max_contracts, "market_max_contracts", positive=True)
            if market_max < self.min_contracts:
                raise ValueError("unsupported market contract size bounds")
            object.__setattr__(self, "market_max_contracts", market_max)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "venue": "gate", "market": "futures", "settle": "usdt",
                "multiplier": str(self.multiplier), "price_tick": str(self.price_tick),
                "min_contracts": str(self.min_contracts), "max_contracts": str(self.max_contracts),
                "market_max_contracts": str(self.market_max_contracts) if self.market_max_contracts is not None else None,
                "taker_fee_rate": str(self.taker_fee_rate), "quantity_unit": "CONTRACTS",
                "fee_source": "PUBLIC_CONTRACT_TAKER_RATE_NOT_PERSONAL_VIP_RATE", "leverage": "1",
                "collateral_policy": "FULL_NOTIONAL", "funding_simulated": False,
                "liquidation_simulated": False}


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    contracts: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, "book price", positive=True))
        object.__setattr__(self, "contracts", _decimal(self.contracts, "book contracts", positive=True))

    def to_dict(self) -> dict:
        return {"price": str(self.price), "contracts": str(self.contracts)}


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    source_at: datetime
    captured_at: datetime
    quote_id: str = ""
    updated_at: datetime | None = None
    sequence_id: str | None = None

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        source = _utc(self.source_at, "source_at")
        captured = _utc(self.captured_at, "captured_at")
        object.__setattr__(self, "source_at", source)
        object.__setattr__(self, "captured_at", captured)
        for name in ("bids", "asks"):
            values = tuple(getattr(self, name))
            if not values or len(values) > 100 or any(not isinstance(level, BookLevel) for level in values):
                raise ValueError("order book requires 1-100 valid levels on each side")
            if any((left.price <= right.price if name == "bids" else left.price >= right.price)
                   for left, right in zip(values, values[1:], strict=False)):
                raise ValueError("order book depth must be strictly price sorted")
            object.__setattr__(self, name, values)
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed or locked order book")
        if source > captured + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            raise ValueError("order book source timestamp is in the future")
        if self.updated_at is not None:
            updated = _utc(self.updated_at, "updated_at")
            if updated > source + timedelta(milliseconds=1):
                raise ValueError("order book update timestamp exceeds response time")
            object.__setattr__(self, "updated_at", updated)
        if self.sequence_id is not None and (not isinstance(self.sequence_id, str)
                or not self.sequence_id.isascii() or not self.sequence_id.isdecimal()
                or not 1 <= len(self.sequence_id) <= 30 or int(self.sequence_id) <= 0):
            raise ValueError("invalid order book sequence id")
        payload = {"symbol": self.symbol, "sequence_id": self.sequence_id,
                   "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                   "bids": [[_canonical_decimal(row.price), _canonical_decimal(row.contracts)] for row in self.bids],
                   "asks": [[_canonical_decimal(row.price), _canonical_decimal(row.contracts)] for row in self.asks]}
        # The response/capture clocks are deliberately excluded: repeatedly
        # observing one unchanged book must not mint new executable liquidity.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.quote_id and self.quote_id != digest:
            raise ValueError("order book quote_id does not match depth")
        object.__setattr__(self, "quote_id", digest)

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    def assert_fresh(self, now: datetime | None = None) -> None:
        current = _utc(now if now is not None else datetime.now(timezone.utc), "now")
        for timestamp in (self.source_at, self.captured_at):
            age = (current - timestamp).total_seconds()
            if age > MAX_BOOK_AGE_SECONDS or age < -MAX_FUTURE_SKEW_SECONDS:
                raise ValueError("order book is stale or future-dated")

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "bids": [row.to_dict() for row in self.bids],
                "asks": [row.to_dict() for row in self.asks], "source_at": self.source_at.isoformat(),
                "captured_at": self.captured_at.isoformat(),
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "sequence_id": self.sequence_id, "quote_id": self.quote_id,
                "best_bid": str(self.best_bid), "best_ask": str(self.best_ask),
                "source_clock": "EXCHANGE_RESPONSE_CURRENT", "update_clock": "LAST_BOOK_CHANGE"}


@dataclass(frozen=True)
class Fill:
    price: Decimal
    contracts: int
    base_quantity: Decimal
    notional: Decimal
    fee: Decimal

    def to_dict(self) -> dict:
        return {"price": str(self.price), "contracts": self.contracts, "base_quantity": str(self.base_quantity),
                "notional": str(self.notional), "fee": str(self.fee), "liquidity": "TAKER_ESTIMATE"}


@dataclass(frozen=True)
class MatchResult:
    status: str
    requested_contracts: int
    filled_contracts: int
    remaining_contracts: int
    fills: tuple[Fill, ...]
    vwap: Decimal | None
    notional: Decimal
    fee: Decimal
    quote_id: str
    reason: str
    resting: bool
    remainder_cancelled: bool
    participation_rate: Decimal
    model_version: str = MODEL_VERSION

    def to_dict(self) -> dict:
        return {"status": self.status, "requested_contracts": self.requested_contracts,
                "filled_contracts": self.filled_contracts, "remaining_contracts": self.remaining_contracts,
                "fills": [fill.to_dict() for fill in self.fills], "vwap": str(self.vwap) if self.vwap is not None else None,
                "notional": str(self.notional), "fee": str(self.fee), "quote_id": self.quote_id,
                "reason": self.reason, "resting": self.resting, "remainder_cancelled": self.remainder_cancelled,
                "participation_rate": str(self.participation_rate), "model_version": self.model_version,
                "execution_mode": "PAPER", "disclaimer": DISCLAIMER}


@_calculation_precision
def validate_price(price: Decimal, price_tick: Decimal, *, name: str = "price") -> Decimal:
    value = _decimal(price, name, positive=True)
    tick = _decimal(price_tick, "price_tick", positive=True)
    if value % tick != ZERO:
        raise ValueError(f"{name} is not aligned to the contract price tick")
    return value


@_calculation_precision
def match_order(
    spec: ContractSpec, book: OrderBook, *, side: str, contracts: int, order_type: str,
    time_in_force: str, limit_price: Decimal | None = None,
    max_slippage_bps: Decimal = Decimal("50"), now: datetime | None = None,
    participation_rate: Decimal = PARTICIPATION_RATE,
    consumed: Mapping[Decimal, int] | None = None,
    remaining_order: bool = False,
) -> MatchResult:
    """Sweep only observed opposite depth; never infer fills from OHLC touches.

    consumed is already-used integer contracts per price on THIS quote and side.
    The repository must atomically apply that shared budget with account changes.
    """
    if not isinstance(spec, ContractSpec) or not isinstance(book, OrderBook) or spec.symbol != book.symbol:
        raise ValueError("contract and order book identities do not match")
    book.assert_fresh(now)
    if side not in {"BUY", "SELL"} or order_type not in {"MARKET", "LIMIT"}:
        raise ValueError("unsupported order side or type")
    if time_in_force not in {"IOC", "GTC", "FOK"} or (order_type == "MARKET" and time_in_force != "IOC"):
        raise ValueError("market orders require IOC; limit orders support IOC/GTC/FOK")
    if type(remaining_order) is not bool:
        raise ValueError("remaining_order must be a boolean")
    if (type(contracts) is not int or contracts <= 0 or contracts > spec.max_contracts
            or (not remaining_order and contracts < spec.min_contracts)):
        raise ValueError("contracts must be a positive integer within exchange order bounds")
    if order_type == "MARKET" and spec.market_max_contracts is not None and contracts > spec.market_max_contracts:
        raise ValueError("contracts exceed the exchange market-order maximum")
    participation = _decimal(participation_rate, "participation_rate", positive=True)
    if participation > ONE:
        raise ValueError("participation_rate must not exceed 1")
    slippage = _decimal(max_slippage_bps, "max_slippage_bps")
    if not ZERO <= slippage <= Decimal("1000"):
        raise ValueError("max_slippage_bps must be between 0 and 1000")
    levels = book.asks if side == "BUY" else book.bids
    if order_type == "LIMIT":
        if limit_price is None:
            raise ValueError("limit price is required")
        boundary = validate_price(limit_price, spec.price_tick, name="limit_price")
    else:
        if limit_price is not None:
            raise ValueError("market orders cannot supply a limit price")
        boundary = levels[0].price * (ONE + slippage / BPS if side == "BUY" else ONE - slippage / BPS)
    for level in (*book.bids, *book.asks):
        validate_price(level.price, spec.price_tick, name="book price")
    budget = consumed or {}
    if any(not isinstance(price, Decimal) or not price.is_finite() or price <= ZERO
           or type(size) is not int or size < 0 for price, size in budget.items()):
        raise ValueError("consumed depth must map exact positive prices to nonnegative integer contracts")
    fills: list[Fill] = []
    remaining = contracts
    boundary_reached = False
    for level in levels:
        if (side == "BUY" and level.price > boundary) or (side == "SELL" and level.price < boundary):
            boundary_reached = True
            break
        capacity = int((level.contracts * participation).to_integral_value(rounding=ROUND_FLOOR))
        size = min(remaining, max(0, capacity - budget.get(level.price, 0)))
        if size:
            base = spec.multiplier * size
            notional = base * level.price
            fills.append(Fill(level.price, size, base, notional, notional * spec.taker_fee_rate))
            remaining -= size
        if remaining == 0:
            break
    if time_in_force == "FOK" and remaining:
        fills, remaining = [], contracts
        reason = "FOK_INSUFFICIENT_EXECUTABLE_DEPTH"
    elif remaining == 0:
        reason = "EXECUTABLE_DEPTH_FILLED"
    elif boundary_reached:
        reason = "SLIPPAGE_CAP" if order_type == "MARKET" else "LIMIT_NOT_CROSSED"
    else:
        reason = "INSUFFICIENT_EXECUTABLE_DEPTH"
    filled = contracts - remaining
    notional = sum((fill.notional for fill in fills), ZERO)
    fee = sum((fill.fee for fill in fills), ZERO)
    resting = remaining > 0 and time_in_force == "GTC"
    status = "FILLED" if not remaining else "PARTIALLY_FILLED" if filled else "OPEN" if resting else "CANCELLED"
    return MatchResult(status, contracts, filled, remaining, tuple(fills),
                       notional / (spec.multiplier * filled) if filled else None, notional, fee,
                       book.quote_id, reason, resting, remaining > 0 and not resting, participation)


def validate_protection(
    side: str, entry_reference: Decimal, stop_loss: Decimal | None,
    take_profit: Decimal | None, price_tick: Decimal,
) -> None:
    if side not in {"LONG", "SHORT"}:
        raise ValueError("unsupported position side")
    entry = _decimal(entry_reference, "entry_reference", positive=True)
    stop = validate_price(stop_loss, price_tick, name="stop_loss") if stop_loss is not None else None
    target = validate_price(take_profit, price_tick, name="take_profit") if take_profit is not None else None
    if stop is not None and (stop >= entry if side == "LONG" else stop <= entry):
        raise ValueError("stop loss must be beyond entry on the loss side")
    if target is not None and (target <= entry if side == "LONG" else target >= entry):
        raise ValueError("take profit must be beyond entry on the profit side")


def exit_trigger(
    side: str, book: OrderBook, *, stop_loss: Decimal | None, take_profit: Decimal | None,
    latched_reason: str | None = None, now: datetime | None = None,
) -> str | None:
    """Latch an exit intention, not a guaranteed stop-price fill.

    A long exits against bids; a short against asks. Gaps execute at the observed
    book, with partial/unfilled remainder retried by the repository. No candle
    touch, missing-book interpolation or reversal of a latched stop is allowed.
    """
    if side not in {"LONG", "SHORT"}:
        raise ValueError("unsupported position side")
    book.assert_fresh(now)
    if latched_reason is not None:
        if latched_reason not in {"STOP_LOSS", "TAKE_PROFIT", "MANUAL"}:
            raise ValueError("unsupported latched exit reason")
        return latched_reason
    price = book.best_bid if side == "LONG" else book.best_ask
    if stop_loss is not None:
        stop = _decimal(stop_loss, "stop_loss", positive=True)
        if price <= stop if side == "LONG" else price >= stop:
            return "STOP_LOSS"
    if take_profit is not None:
        target = _decimal(take_profit, "take_profit", positive=True)
        if price >= target if side == "LONG" else price <= target:
            return "TAKE_PROFIT"
    return None
