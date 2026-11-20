from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from gatescope_executor.oms import (
    IntentKind,
    OrderIntent,
    OrderState,
    OrderTracker,
    ProtectionAction,
    ProtectionWatchdog,
)


def intent(kind: IntentKind = IntentKind.ENTRY) -> OrderIntent:
    return OrderIntent(
        intent_id=UUID("10000000-0000-0000-0000-000000000001"),
        account_id=UUID("10000000-0000-0000-0000-000000000002"),
        instrument_id=UUID("10000000-0000-0000-0000-000000000003"),
        kind=kind,
        side="BUY",
        quantity=Decimal("1.25"),
    )


def test_unknown_submit_requires_reconcile_and_reuses_client_order_id() -> None:
    tracker = OrderTracker(intent())
    first = tracker.begin_submit()
    tracker.submit_timed_out()
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        tracker.begin_submit()
    tracker.reconcile_not_found()
    second = tracker.begin_submit()
    assert first.client_order_id == second.client_order_id
    assert tracker.submit_attempts == 2


def test_conflicting_ack_and_duplicate_fills_fail_closed() -> None:
    tracker = OrderTracker(intent())
    tracker.begin_submit()
    tracker.acknowledge("exchange-1")
    tracker.acknowledge("exchange-1")
    with pytest.raises(RuntimeError, match="conflicting"):
        tracker.acknowledge("exchange-2")
    assert tracker.record_fill("fill-1", Decimal("0.25"))
    assert not tracker.record_fill("fill-1", Decimal("0.25"))
    assert tracker.state is OrderState.PARTIALLY_FILLED
    assert tracker.record_fill("fill-2", Decimal("1.00"))
    assert tracker.state is OrderState.FILLED

    never_submitted = OrderTracker(intent())
    with pytest.raises(RuntimeError, match="never submitted"):
        never_submitted.record_fill("impossible", Decimal("0.1"))


def test_all_non_entry_commands_are_reduce_only() -> None:
    for kind in (IntentKind.REDUCE_ONLY_EXIT, IntentKind.PROTECTIVE_STOP, IntentKind.TAKE_PROFIT):
        assert OrderTracker(intent(kind)).begin_submit().reduce_only is True
    assert OrderTracker(intent()).begin_submit().reduce_only is False


def test_partial_fill_starts_deadline_and_timeout_forces_de_risk_actions() -> None:
    watchdog = ProtectionWatchdog(hard_deadline_ms=5_000)
    watchdog.record_entry_fill(Decimal("0.4"), fill_event_monotonic_ms=10_000)
    assert watchdog.required_actions(now_monotonic_ms=14_999, entry_remainder_open=True) == ()
    assert watchdog.required_actions(now_monotonic_ms=15_000, entry_remainder_open=True) == (
        ProtectionAction.CANCEL_ENTRY_REMAINDER,
        ProtectionAction.SUBMIT_REDUCE_ONLY_EXIT,
        ProtectionAction.FREEZE_NEW_ENTRIES,
    )


def test_confirmed_exchange_stop_covers_position_and_removes_deadline() -> None:
    watchdog = ProtectionWatchdog()
    watchdog.record_entry_fill(Decimal("1"), fill_event_monotonic_ms=1_000)
    watchdog.confirm_stop(
        Decimal("1"),
        active=True,
        reduce_only_or_close_position=True,
        trigger_price=Decimal("29000"),
    )
    status = watchdog.status()
    assert status.protected
    assert status.deadline_monotonic_ms is None
    assert watchdog.required_actions(now_monotonic_ms=100_000, entry_remainder_open=False) == ()


def test_invalid_or_overcovering_stop_is_never_counted_as_protection() -> None:
    watchdog = ProtectionWatchdog(dust=Decimal("0.001"))
    watchdog.record_entry_fill(Decimal("1"), fill_event_monotonic_ms=1_000)
    with pytest.raises(ValueError, match="active"):
        watchdog.confirm_stop(
            Decimal("1"), active=False, reduce_only_or_close_position=True,
            trigger_price=Decimal("29000"),
        )
    with pytest.raises(RuntimeError, match="over-cover"):
        watchdog.confirm_stop(
            Decimal("1.01"), active=True, reduce_only_or_close_position=True,
            trigger_price=Decimal("29000"),
        )
    assert not watchdog.status().protected


def test_reduce_fill_cannot_reverse_and_flat_position_clears_protection() -> None:
    watchdog = ProtectionWatchdog()
    watchdog.record_entry_fill(Decimal("0.5"), fill_event_monotonic_ms=1_000)
    watchdog.confirm_stop(
        Decimal("0.5"), active=True, reduce_only_or_close_position=True,
        trigger_price=Decimal("29000"),
    )
    with pytest.raises(RuntimeError, match="reverse"):
        watchdog.record_reduce_fill(Decimal("0.6"))
    watchdog.record_reduce_fill(Decimal("0.5"))
    assert watchdog.status().open_quantity == 0
    assert watchdog.status().valid_stop_coverage == 0
