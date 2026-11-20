"""Bounded, owner-isolated live-depth PAPER protection and pending fills."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import UUID

from gatescope.adapters.gate_paper_market import MarketSnapshot
from gatescope.domain.paper_matching import validate_symbol


class PaperOrderStore(Protocol):
    def pending_targets(self, limit: int = 100) -> list[dict]: ...
    def protect_positions(self, tenant_id: UUID, owner_id: UUID, instrument_id: UUID,
                          quote: MarketSnapshot | None, *, account_id: UUID | None = None,
                          now: datetime | None = None, limit: int = 10) -> dict: ...
    def list_orders(self, tenant_id: UUID, owner_id: UUID, **kwargs) -> dict: ...
    def process_pending(self, tenant_id: UUID, owner_id: UUID, order_id: UUID,
                        quote: MarketSnapshot | None, *, now: datetime | None = None) -> dict: ...


class PaperQuoteProvider(Protocol):
    def get(self, symbol: str) -> MarketSnapshot: ...


@dataclass(frozen=True)
class PaperOrderMonitorResult:
    targets: int
    protected: int
    attempted: int
    fills: int
    closed_orders: int
    unavailable: int
    failed: int
    elapsed_seconds: float
    budget_exhausted: bool


def _bounded(value: int, maximum: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


class PaperOrderMonitor:
    """Always perform the selected protection pass before ordinary orders.

    The repository owns RLS, shared quote budgets and idempotency. This loop
    passes explicit tenant/owner IDs for every operation and never writes marks
    or fabricates zero prices itself. Scheduling budgets are soft between bounded
    repository/provider calls, not thread cancellation or hard real-time promises.
    """

    def __init__(
        self, store: PaperOrderStore, market: PaperQuoteProvider, *,
        clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.market = store, market
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.monotonic = monotonic
        self._last_target_key: tuple[str, ...] | None = None
        self._cooldowns: OrderedDict[str, float] = OrderedDict()

    @staticmethod
    def _target(raw: dict) -> dict:
        return {"tenant_id": UUID(str(raw["tenant_id"])), "owner_user_id": UUID(str(raw["owner_user_id"])),
                "account_id": UUID(str(raw["account_id"])), "instrument_id": UUID(str(raw["instrument_id"])),
                "symbol": validate_symbol(raw["symbol"])}

    @staticmethod
    def _key(target: dict) -> tuple[str, ...]:
        return tuple(str(target[field]) for field in ("tenant_id", "owner_user_id", "account_id", "instrument_id", "symbol"))

    @staticmethod
    def _count(outcome: dict) -> tuple[int, int]:
        rows = outcome.get("fills") or []
        order = outcome.get("order") or {}
        return len(rows) if isinstance(rows, list) else 0, int(order.get("effect") == "CLOSE" and order.get("status") == "FILLED")

    def run_once(
        self, *, target_limit: int = 12, positions_per_target: int = 3, orders_per_target: int = 3,
        budget_seconds: float = 8.0, now: datetime | None = None,
    ) -> PaperOrderMonitorResult:
        _bounded(target_limit, 100, "target_limit")
        _bounded(positions_per_target, 10, "positions_per_target")
        _bounded(orders_per_target, 10, "orders_per_target")
        if isinstance(budget_seconds, bool) or not math.isfinite(budget_seconds) or not 0.1 <= budget_seconds <= 30:
            raise ValueError("invalid paper monitor budget")
        start = self.monotonic()
        supplied = now
        initial = supplied if supplied is not None else self.clock()
        if not isinstance(initial, datetime) or initial.tzinfo is None or initial.utcoffset() is None:
            raise ValueError("paper monitor time must include a timezone")

        def observed() -> datetime:
            # A supplied test clock is advanced by actual elapsed work. Passing
            # cycle-start as now for every order could execute expired snapshots.
            value = supplied + timedelta(seconds=self.monotonic() - start) if supplied is not None else self.clock()
            return value.astimezone(timezone.utc)

        def exhausted() -> bool:
            return self.monotonic() - start >= budget_seconds

        failed = unavailable = protected = attempted = fills = closed = visited = 0
        # Fetch a bounded scheduling window, then rotate within it on failures.
        # SQL additionally orders by last_checked_at for restart-safe fairness.
        raw_targets = self.store.pending_targets(limit=100)
        targets, keys = [], set()
        for raw in raw_targets[:100]:
            try:
                target = self._target(raw)
                key = self._key(target)
                if key not in keys:
                    targets.append(target)
                    keys.add(key)
            except (ValueError, TypeError, KeyError):
                failed += 1
        if self._last_target_key is not None:
            cursor = next((index + 1 for index, target in enumerate(targets)
                           if self._key(target) == self._last_target_key), 0)
            targets = targets[cursor:] + targets[:cursor]
        selected = targets[:target_limit]
        quotes: dict[str, MarketSnapshot | None] = {}
        fetches: dict[str, int] = {}

        def quote_for(symbol: str) -> MarketSnapshot | None:
            nonlocal unavailable
            current = quotes.get(symbol)
            if current is not None:
                try:
                    current.book.assert_fresh(observed())
                    return current
                except ValueError:
                    quotes.pop(symbol, None)
            elif symbol in quotes:
                return None
            if self._cooldowns.get(symbol, 0) > self.monotonic() or exhausted() or fetches.get(symbol, 0) >= 2:
                quotes[symbol] = None
                return None
            fetches[symbol] = fetches.get(symbol, 0) + 1
            try:
                value = self.market.get(symbol)
                if value.spec.symbol != symbol or value.book.symbol != symbol:
                    raise ValueError("paper market instrument mismatch")
                value.book.assert_fresh(observed())
                quotes[symbol] = value
                self._cooldowns.pop(symbol, None)
                return value
            except Exception as exc:
                unavailable += 1
                retry = getattr(exc, "retry_after", None)
                wait = float(retry) if isinstance(retry, (int, float)) and math.isfinite(retry) and 0 <= retry <= 86400 else 2.0
                self._cooldowns[symbol] = self.monotonic() + max(wait, 0.1)
                self._cooldowns.move_to_end(symbol)
                while len(self._cooldowns) > 128:
                    self._cooldowns.popitem(last=False)
                quotes[symbol] = None
                return None

        ready = []
        for target in selected:
            if exhausted():
                break
            visited += 1
            self._last_target_key = self._key(target)
            quote = quote_for(target["symbol"])
            try:
                outcome = self.store.protect_positions(
                    target["tenant_id"], target["owner_user_id"], target["instrument_id"], quote,
                    account_id=target["account_id"], now=observed(), limit=positions_per_target,
                )
                protected += int(outcome.get("checked", 0))
                for item in outcome.get("items", []):
                    item_fills, item_closed = self._count(item)
                    fills += item_fills
                    closed += item_closed
                ready.append(target)
            except Exception:
                # Never permit this owner's entries after its protection failed;
                # the other owners and old PAPER monitor remain independent.
                failed += 1
        for target in ready:
            if exhausted():
                break
            try:
                orders = self.store.list_orders(
                    target["tenant_id"], target["owner_user_id"], account_id=target["account_id"],
                    instrument_id=target["instrument_id"], status="ACTIVE", limit=orders_per_target,
                )["items"]
            except Exception:
                failed += 1
                continue
            for order in orders[:orders_per_target]:
                if exhausted():
                    break
                attempted += 1
                try:
                    order_id = UUID(str(order["id"]))
                    quote = quote_for(target["symbol"])
                    result = self.store.process_pending(target["tenant_id"], target["owner_user_id"], order_id,
                                                        quote, now=observed())
                    item_fills, item_closed = self._count(result)
                    fills += item_fills
                    closed += item_closed
                except Exception:
                    failed += 1
        elapsed = self.monotonic() - start
        return PaperOrderMonitorResult(visited, protected, attempted, fills, closed, unavailable,
                                       failed, elapsed, elapsed >= budget_seconds)
