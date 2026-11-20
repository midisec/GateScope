from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from gatescope.adapters.annual_structure_study import page


class Connection:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        return self

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("timeframe,step", [("1m", 60), ("1h", 3600), ("1d", 86400)])
def test_next_open_search_and_revision_sort_share_asof_but_sort_only_a_bounded_window(timeframe, step):
    connection, instrument = Connection(), uuid4()
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert page(connection, instrument, timeframe, as_of, 100, 9000, 200, limit=64) == []
    assert len(connection.calls) == 1
    query, params = connection.calls[0]
    anchor, window = query.split("SELECT recent.*", 1)
    assert "WITH next_bar AS MATERIALIZED" in anchor
    assert "ORDER BY open_time ASC LIMIT 1" in anchor and "revision DESC" not in anchor
    assert "CROSS JOIN LATERAL" in window
    assert "open_time>=n.open_time AND open_time<n.open_time+make_interval(secs=>%s)" in window
    assert "open_time>=to_timestamp(%s) AND open_time>to_timestamp(%s)" in window
    assert "ORDER BY open_time ASC,revision DESC LIMIT %s" in window
    assert query.count("available_at<=%s AND created_at<=%s AND close_time<=%s") == 2
    assert query.count("mod(extract(epoch FROM open_time),%s)=0") == 2
    assert params == (instrument, timeframe, 100, 9000, 200, as_of, as_of, as_of, step,
                      instrument, timeframe, 64 * step, 100, 200, 9000, as_of, as_of, as_of, step, 64)


def test_result_shape_and_revision_metadata_are_unchanged():
    opened = datetime(2025, 1, 1, tzinfo=timezone.utc)
    connection = Connection([(opened, Decimal("100"), Decimal("103"), Decimal("99"), Decimal("102"),
                              Decimal("10"), 2, "original-hash", ["REST_POLLING"])])
    result = page(connection, uuid4(), "1m", opened, 0, 9999999999, -1, limit=4)
    assert result == [{"ts": int(opened.timestamp()), "open": 100.0, "high": 103.0, "low": 99.0,
                       "close": 102.0, "volume": 10.0, "revision": 2,
                       "payload_hash": "original-hash", "quality_flags": ["REST_POLLING"]}]


@pytest.mark.parametrize("limit", [-1, True, 1.5, "64"])
def test_invalid_window_limit_is_rejected_before_query(limit):
    connection = Connection()
    with pytest.raises(ValueError, match="PATTERN_PAGE_LIMIT"):
        page(connection, uuid4(), "1m", datetime.now(timezone.utc), 0, 1000, -1, limit=limit)
    assert not connection.calls


def test_zero_limit_preserves_empty_page_semantics_without_a_query():
    connection = Connection()
    assert page(connection, uuid4(), "1m", datetime.now(timezone.utc), 0, 1000, -1, limit=0) == []
    assert not connection.calls
