from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gatescope.domain.market import Bar, BarRevisionBook, GapReconciler


def make_bar(
    minute: int,
    *,
    revision: int = 1,
    close: str = "101",
    flags: frozenset[str] = frozenset(),
    is_final: bool = True,
) -> Bar:
    opened = datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc)
    return Bar(
        venue="GATE", market="FUTURES", instrument_id="btc", timeframe="1m",
        open_time=opened, close_time=opened + timedelta(minutes=1),
        open=Decimal("100"), high=Decimal("102"), low=Decimal("99"),
        close=Decimal(close), volume=Decimal("10"), is_final=is_final,
        revision=revision, available_at=opened + timedelta(minutes=1), quality_flags=flags,
    )


def test_bar_enforces_time_and_ohlc_invariants() -> None:
    with pytest.raises(ValueError, match="duration"):
        replace(make_bar(0), close_time=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="high"):
        replace(make_bar(0), high=Decimal("100"), close=Decimal("101"))
    with pytest.raises(ValueError, match="timezone"):
        replace(make_bar(0), open_time=datetime(2026, 1, 1))


def test_visibility_and_legacy_flags_block_live() -> None:
    bar = make_bar(0, flags=frozenset({"LEGACY_FINALITY_ASSUMED"}))
    assert bar.visible_at(bar.available_at)
    assert not bar.live_eligible_at(bar.available_at)
    assert not make_bar(0, is_final=False).visible_at(bar.available_at)


def test_revision_book_is_append_only_and_idempotent() -> None:
    book = BarRevisionBook()
    first = make_bar(0)
    assert book.append(first) == (first, True)
    assert book.append(first) == (first, False)
    revised = make_bar(0, revision=2, close="100.5")
    assert book.append(revised) == (revised, True)
    assert book.current() == [revised]
    assert book.facts() == (first, revised)
    with pytest.raises(ValueError, match="expected revision 3"):
        book.append(make_bar(0, revision=4, close="100.25"))


def test_gap_reconciliation_and_watermark() -> None:
    reconciler = GapReconciler()
    bars = [make_bar(0), make_bar(2)]
    gap = reconciler.find_gaps(bars)[0]
    assert gap.missing_count == 1
    assert reconciler.contiguous_watermark(bars) is None
    complete = reconciler.reconcile(bars, lambda start, end: [make_bar(1)])
    assert [bar.open_time.minute for bar in complete] == [0, 1, 2]
    assert reconciler.contiguous_watermark(complete) == complete[-1].available_at
