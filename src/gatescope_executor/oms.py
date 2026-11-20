"""Deterministic OMS and protection invariants without a live exchange adapter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID


class OrderState(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class IntentKind(str, Enum):
    ENTRY = "ENTRY"
    REDUCE_ONLY_EXIT = "REDUCE_ONLY_EXIT"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"


@dataclass(frozen=True)
class OrderIntent:
    intent_id: UUID
    account_id: UUID
    instrument_id: UUID
    kind: IntentKind
    side: str
    quantity: Decimal
    revision: int = 1

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("order side must be BUY or SELL")
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ValueError("order quantity must be finite and positive")
        if self.revision < 1:
            raise ValueError("order revision must be positive")

    @property
    def client_order_id(self) -> str:
        material = f"{self.intent_id}:{self.revision}:{self.kind.value}".encode()
        return "gs-" + hashlib.sha256(material).hexdigest()[:28]


@dataclass(frozen=True)
class SubmitCommand:
    client_order_id: str
    intent_id: UUID
    kind: IntentKind
    side: str
    quantity: Decimal
    reduce_only: bool


class OrderTracker:
    """A timeout enters UNKNOWN; only reconciliation can permit a resubmit."""

    TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED})

    def __init__(self, intent: OrderIntent) -> None:
        self.intent = intent
        self.state = OrderState.PENDING_SUBMIT
        self.exchange_order_id: str | None = None
        self.cumulative_filled = Decimal("0")
        self._fill_ids: set[str] = set()
        self.submit_attempts = 0

    def begin_submit(self) -> SubmitCommand:
        if self.state is not OrderState.PENDING_SUBMIT:
            raise RuntimeError(f"cannot submit order in state {self.state.value}")
        self.state = OrderState.SUBMITTING
        self.submit_attempts += 1
        return SubmitCommand(
            client_order_id=self.intent.client_order_id,
            intent_id=self.intent.intent_id,
            kind=self.intent.kind,
            side=self.intent.side,
            quantity=self.intent.quantity,
            reduce_only=self.intent.kind is not IntentKind.ENTRY,
        )

    def submit_timed_out(self) -> None:
        if self.state is not OrderState.SUBMITTING:
            raise RuntimeError("only a submitting order can time out")
        self.state = OrderState.UNKNOWN

    def acknowledge(self, exchange_order_id: str) -> None:
        if self.state not in {OrderState.SUBMITTING, OrderState.UNKNOWN, OrderState.ACKED}:
            raise RuntimeError(f"cannot acknowledge order in state {self.state.value}")
        cleaned = exchange_order_id.strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("exchange_order_id is invalid")
        if self.exchange_order_id is not None and self.exchange_order_id != cleaned:
            raise RuntimeError("client order id resolved to conflicting exchange orders")
        self.exchange_order_id = cleaned
        self.state = OrderState.ACKED

    def reconcile_not_found(self) -> None:
        if self.state is not OrderState.UNKNOWN:
            raise RuntimeError("not-found reconciliation is only valid from UNKNOWN")
        self.state = OrderState.PENDING_SUBMIT

    def reject(self) -> None:
        if self.state not in {OrderState.SUBMITTING, OrderState.UNKNOWN, OrderState.ACKED}:
            raise RuntimeError(f"cannot reject order in state {self.state.value}")
        if self.cumulative_filled:
            raise RuntimeError("an order with fills cannot become REJECTED")
        self.state = OrderState.REJECTED

    def cancel(self) -> None:
        if self.state not in {
            OrderState.ACKED, OrderState.PARTIALLY_FILLED, OrderState.UNKNOWN,
        }:
            raise RuntimeError(f"cannot cancel order in state {self.state.value}")
        self.state = OrderState.CANCELED

    def record_fill(self, fill_id: str, quantity: Decimal) -> bool:
        if self.state in {OrderState.CANCELED, OrderState.REJECTED}:
            raise RuntimeError("terminal order cannot accept a new fill")
        if self.state is OrderState.PENDING_SUBMIT:
            raise RuntimeError("an order that was never submitted cannot have a fill")
        cleaned = fill_id.strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("fill_id is invalid")
        if cleaned in self._fill_ids:
            return False
        if quantity <= 0 or not quantity.is_finite():
            raise ValueError("fill quantity must be finite and positive")
        projected = self.cumulative_filled + quantity
        if projected > self.intent.quantity:
            raise RuntimeError("cumulative fill exceeds requested quantity")
        self._fill_ids.add(cleaned)
        self.cumulative_filled = projected
        self.state = (
            OrderState.FILLED
            if self.cumulative_filled == self.intent.quantity
            else OrderState.PARTIALLY_FILLED
        )
        return True


class ProtectionAction(str, Enum):
    CANCEL_ENTRY_REMAINDER = "CANCEL_ENTRY_REMAINDER"
    SUBMIT_REDUCE_ONLY_EXIT = "SUBMIT_REDUCE_ONLY_EXIT"
    FREEZE_NEW_ENTRIES = "FREEZE_NEW_ENTRIES"
    CANCEL_OR_RESIZE_EXCESS_STOP = "CANCEL_OR_RESIZE_EXCESS_STOP"


@dataclass(frozen=True)
class ProtectionStatus:
    open_quantity: Decimal
    valid_stop_coverage: Decimal
    uncovered_quantity: Decimal
    deadline_monotonic_ms: int | None
    protected: bool


class ProtectionWatchdog:
    """Tracks exchange-confirmed stop coverage from the corrected fill event time."""

    def __init__(self, *, hard_deadline_ms: int = 5_000, dust: Decimal = Decimal("0")) -> None:
        if not 100 <= hard_deadline_ms <= 5_000:
            raise ValueError("hard protection deadline must be between 100 and 5,000 ms")
        if dust < 0 or not dust.is_finite():
            raise ValueError("dust must be finite and non-negative")
        self.hard_deadline_ms = hard_deadline_ms
        self.dust = dust
        self.open_quantity = Decimal("0")
        self.valid_stop_coverage = Decimal("0")
        self.deadline_monotonic_ms: int | None = None

    def record_entry_fill(self, quantity: Decimal, *, fill_event_monotonic_ms: int) -> None:
        _positive_quantity(quantity)
        if fill_event_monotonic_ms < 0:
            raise ValueError("fill event monotonic time must be non-negative")
        self.open_quantity += quantity
        candidate = fill_event_monotonic_ms + self.hard_deadline_ms
        if self.uncovered_quantity > self.dust:
            self.deadline_monotonic_ms = (
                candidate
                if self.deadline_monotonic_ms is None
                else min(self.deadline_monotonic_ms, candidate)
            )

    def confirm_stop(
        self,
        total_covered_quantity: Decimal,
        *,
        active: bool,
        reduce_only_or_close_position: bool,
        trigger_price: Decimal,
    ) -> None:
        if not active or not reduce_only_or_close_position:
            raise ValueError("only active reduce-only/close-position protection is valid")
        _positive_quantity(trigger_price)
        if total_covered_quantity < 0 or not total_covered_quantity.is_finite():
            raise ValueError("stop coverage must be finite and non-negative")
        if total_covered_quantity > self.open_quantity + self.dust:
            raise RuntimeError("protective stop would over-cover the open position")
        self.valid_stop_coverage = total_covered_quantity
        if self.uncovered_quantity <= self.dust:
            self.deadline_monotonic_ms = None

    def invalidate_stop(self, *, observed_monotonic_ms: int) -> None:
        if observed_monotonic_ms < 0:
            raise ValueError("observed monotonic time must be non-negative")
        self.valid_stop_coverage = Decimal("0")
        if self.open_quantity > self.dust:
            self.deadline_monotonic_ms = observed_monotonic_ms + self.hard_deadline_ms

    def record_reduce_fill(self, quantity: Decimal) -> None:
        _positive_quantity(quantity)
        if quantity > self.open_quantity:
            raise RuntimeError("reduce fill would reverse the position")
        self.open_quantity -= quantity
        if self.open_quantity == 0:
            self.valid_stop_coverage = Decimal("0")
            self.deadline_monotonic_ms = None

    @property
    def uncovered_quantity(self) -> Decimal:
        return max(Decimal("0"), self.open_quantity - self.valid_stop_coverage)

    def status(self) -> ProtectionStatus:
        uncovered = self.uncovered_quantity
        return ProtectionStatus(
            open_quantity=self.open_quantity,
            valid_stop_coverage=self.valid_stop_coverage,
            uncovered_quantity=uncovered,
            deadline_monotonic_ms=self.deadline_monotonic_ms,
            protected=uncovered <= self.dust and self.valid_stop_coverage <= self.open_quantity + self.dust,
        )

    def required_actions(
        self, *, now_monotonic_ms: int, entry_remainder_open: bool,
    ) -> tuple[ProtectionAction, ...]:
        if now_monotonic_ms < 0:
            raise ValueError("current monotonic time must be non-negative")
        if self.valid_stop_coverage > self.open_quantity + self.dust:
            return (
                ProtectionAction.CANCEL_OR_RESIZE_EXCESS_STOP,
                ProtectionAction.FREEZE_NEW_ENTRIES,
            )
        if self.uncovered_quantity <= self.dust:
            return ()
        deadline = self.deadline_monotonic_ms
        if deadline is None or now_monotonic_ms < deadline:
            return ()
        actions: list[ProtectionAction] = []
        if entry_remainder_open:
            actions.append(ProtectionAction.CANCEL_ENTRY_REMAINDER)
        actions.extend((
            ProtectionAction.SUBMIT_REDUCE_ONLY_EXIT,
            ProtectionAction.FREEZE_NEW_ENTRIES,
        ))
        return tuple(actions)


def _positive_quantity(value: Decimal) -> None:
    if value <= 0 or not value.is_finite():
        raise ValueError("value must be finite and positive")
