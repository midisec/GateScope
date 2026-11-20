"""Frozen signal diagrams may never select a different lifecycle transition."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository


def fixture_signal():
    start = 1700000000
    rows = [
        {"ts": start + i * 3600, "open": 100.0, "high": 103.0, "low": 98.0, "close": 101.0, "volume": 10.0}
        for i in range(5)
    ]
    points = [{"time": rows[i]["ts"], "price": rows[i]["high"], "label": str(i)} for i in (0, 3)]
    event = {
        "context_id": "a" * 64,
        "id": "b" * 64,
        "evidence_id": "c" * 64,
        "detector_version": "structure-study-3.2",
        "phase": "CONFIRMED",
        "pattern": "double_top",
        "name": "双顶",
        "points": points,
        "lines": [{"from": points[0], "to": points[1]}],
        "levels": {},
        "observed_time": rows[-1]["ts"] + 3600,
    }
    record = {
        "latest": deepcopy(event),
        "phase": "CONFIRMED",
        "observed_time": event["observed_time"],
        "snapshot_at": datetime.fromtimestamp(event["observed_time"] + 5, timezone.utc).isoformat(),
    }
    signal = {
        "id": str(uuid4()),
        "instrument_id": str(uuid4()),
        "symbol": "BTC_USDT",
        "timeframe": "1h",
        "payload": {"matched": [{"source_event": event}]},
    }
    return signal, record, rows


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Pool:
    def __init__(self, record, *, available=True):
        self.record = record
        self.available = available
        self.queries = []

    @contextmanager
    def public_transaction(self):
        yield self

    def execute(self, query, args=()):
        self.queries.append((query, args))
        if "statement_timeout" in query:
            return Result(None)
        if "event_json->'latest'->>'evidence_id'=%s" in query:
            assert (
                args[0] == "a" * 64
                and args[1] == "b" * 64
                and args[-2] == "structure-study-3.2"
                and args[-1] == "c" * 64
            )
            return Result((self.record,) if self.available else None)
        if "event_json->>'observed_time'" in query:
            return Result(("INVALIDATED", str(self.record["observed_time"] + 7200)))
        pytest.fail("unexpected archive query: " + query)


def test_notification_chart_selects_exact_evidence_and_original_bar_revision(monkeypatch):
    signal, record, rows = fixture_signal()
    pool = Pool(record)
    calls = []

    def snapshot(connection, instrument, tf, limit, as_of, *, end_time=None, strict=True):
        calls.append((as_of, end_time, strict))
        return deepcopy(rows), {}

    monkeypatch.setattr("gatescope.adapters.postgres_telegram_market.snapshot", snapshot)
    chart, text = TelegramMarketRepository(pool).signal_chart(signal)
    assert calls == [
        (
            datetime.fromisoformat(record["snapshot_at"]),
            datetime.fromtimestamp(record["observed_time"], timezone.utc),
            True,
        )
    ]
    assert chart["bars"] == rows and chart["event"] == record["latest"]
    assert chart["layout"] == "PATTERN" and chart["values"] == {} and chart["indicator_versions"] == {}
    assert "通知时：已确认" in text and "未下单" in text


def test_missing_notified_evidence_never_substitutes_first_or_latest_record(monkeypatch):
    signal, record, _ = fixture_signal()
    pool = Pool(record, available=False)
    monkeypatch.setattr(
        "gatescope.adapters.postgres_telegram_market.snapshot",
        lambda *a, **k: pytest.fail("wrong archive fallback"),
    )
    chart, text = TelegramMarketRepository(pool).signal_chart(signal)
    assert chart is None and "不使用首次识别图或最新形态代替" in text
    assert len(pool.queries) == 2


def test_source_geometry_must_match_archived_transition(monkeypatch):
    signal, record, _ = fixture_signal()
    pool = Pool(record)
    signal["payload"]["matched"][0]["source_event"]["points"][0]["price"] = 999.0
    monkeypatch.setattr(
        "gatescope.adapters.postgres_telegram_market.snapshot",
        lambda *a, **k: pytest.fail("mismatched evidence"),
    )
    chart, text = TelegramMarketRepository(pool).signal_chart(signal)
    assert chart is None and "精确原始图暂不可用" in text


@pytest.mark.parametrize("gap", [False, True])
def test_latest_query_preserves_original_prices_lines_and_plan(monkeypatch, gap):
    signal, record, rows = fixture_signal()
    pool = Pool(record)
    future = [
        {**rows[-1], "ts": record["observed_time"] + (i + int(gap)) * 3600, "close": 99.0} for i in range(2)
    ]

    def snapshot(connection, instrument, tf, limit, as_of, *, end_time=None, strict=True):
        if end_time:
            return deepcopy(rows), {}
        # Corrected historical bars must NOT replace the original revisions.
        return [{**row, "close": 999.0} for row in rows] + future, {}

    monkeypatch.setattr("gatescope.adapters.postgres_telegram_market.snapshot", snapshot)
    chart, text = TelegramMarketRepository(pool).signal_chart(signal, latest=True)
    assert chart["bars"][: len(rows)] == rows
    assert chart["event"] == record["latest"] and "最新归档：已失效" in text
    assert "形态状态不等于实际持仓或止盈止损结果" in text
    if gap:
        assert chart["bars"] == rows and "后续 K 线缓存不连续" in text
    else:
        assert chart["bars"] == rows + future
