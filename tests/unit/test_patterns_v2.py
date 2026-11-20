from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

import pytest

from gatescope.domain.market import Bar
from gatescope.indicators.patterns_v2 import CausalPatternLedger


def make_bar(index: int, *, is_final: bool = True, flags: frozenset[str] = frozenset()) -> Bar:
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    close = Decimal(str(100 + index))
    return Bar(
        venue="GATE", market="FUTURES", instrument_id="btc", timeframe="1m",
        open_time=opened, close_time=opened + timedelta(minutes=1),
        open=Decimal("100"), high=close + 1, low=Decimal("99"),
        close=close, volume=Decimal("10"), is_final=is_final,
        revision=1, available_at=opened + timedelta(minutes=1), quality_flags=flags,
    )


class UpdatingScanner:
    def __init__(self) -> None:
        self.orders: list[int] = []

    def __call__(self, candles: Sequence[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.orders.append(int(kwargs["pivot_order"]))
        if len(candles) < 3:
            return {"events": []}
        confirmed = len(candles) >= 4
        confirmation_time = int(candles[3]["ts"]) if confirmed else None
        points = [
            {"time": int(candles[0]["ts"]), "price": 100.0, "label": "1"},
            {"time": int(candles[1]["ts"]), "price": 101.0, "label": "2"},
        ]
        if confirmation_time is not None:
            points.append({"time": confirmation_time, "price": 103.0, "label": "✓"})
        return {
            "events": [{
                "pattern": "double_bottom",
                "start_time": int(candles[0]["ts"]),
                "end_time": confirmation_time or int(candles[1]["ts"]),
                "points": points,
                "confidence": 0.8 if confirmed else 0.6,
                "confirmed": confirmed,
                "direction": "bullish",
            }]
        }


def test_future_bars_supersede_but_never_mutate_old_facts() -> None:
    scanner = UpdatingScanner()
    ledger = CausalPatternLedger(scanner=scanner, pivot_order=3, max_bars=20)
    assert ledger.append(make_bar(0)) == ()
    assert ledger.append(make_bar(1)) == ()
    first = ledger.append(make_bar(2))[0]
    frozen_payload = first.payload_json
    second = ledger.append(make_bar(3))[0]
    assert ledger.append(make_bar(4)) == ()

    assert scanner.orders == [3, 3, 3, 3, 3]
    assert first.revision == 1
    assert second.revision == 2
    assert second.series_id == first.series_id
    assert second.supersedes_fact_id == first.fact_id
    assert first.payload_json == frozen_payload
    assert not first.payload()["confirmed"]
    assert second.payload()["confirmed"]
    assert ledger.facts() == (first, second)
    assert ledger.current() == (second,)
    assert ledger.current(as_of=first.available_at) == (first,)
    assert first.available_at >= make_bar(2).available_at


def test_legacy_quality_is_carried_and_blocks_live_eligibility() -> None:
    ledger = CausalPatternLedger(scanner=UpdatingScanner(), max_bars=20)
    ledger.append(make_bar(0, flags=frozenset({"AVAILABLE_AT_UNKNOWN"})))
    ledger.append(make_bar(1))
    fact = ledger.append(make_bar(2))[0]
    assert fact.input_quality_flags == ("AVAILABLE_AT_UNKNOWN",)
    assert not fact.live_eligible


def test_ledger_rejects_future_output_gaps_and_nonfinal_input() -> None:
    def future_scanner(candles: Sequence[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        future = int(candles[-1]["ts"]) + 60
        return {"events": [{
            "pattern": "bad", "start_time": int(candles[0]["ts"]), "end_time": future,
            "points": [{"time": future, "price": 100.0}], "confidence": 0.5, "confirmed": False,
        }]}

    ledger = CausalPatternLedger(scanner=future_scanner, max_bars=20)
    with pytest.raises(ValueError, match="future"):
        ledger.append(make_bar(0))

    ledger = CausalPatternLedger(scanner=UpdatingScanner(), max_bars=20)
    with pytest.raises(ValueError, match="final"):
        ledger.append(make_bar(0, is_final=False))
    ledger.append(make_bar(0))
    with pytest.raises(ValueError, match="contiguous"):
        ledger.append(make_bar(2))


def test_payload_accessor_returns_a_copy() -> None:
    ledger = CausalPatternLedger(scanner=UpdatingScanner(), max_bars=20)
    for index in range(3):
        facts = ledger.append(make_bar(index))
    fact = facts[0]
    payload = fact.payload()
    payload["confirmed"] = True
    assert fact.payload()["confirmed"] is False
