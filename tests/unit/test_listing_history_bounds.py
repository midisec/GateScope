from datetime import datetime, timezone
from uuid import UUID

import pytest

from gatescope.adapters.market_history_bounds import history_bounds
from gatescope.domain.history import listing_year_bounds, year_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS


NOW = datetime(2026, 9, 12, 12, 0, 30, tzinfo=timezone.utc)
LISTED = 1_773_138_212
INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, metadata, *, venue="GATE", market="FUTURES_USDT", earlier=None):
        self.metadata = metadata
        self.venue = venue
        self.market = market
        self.earlier = earlier
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        if "FROM instruments" in statement:
            return Result((self.venue, self.market, "龙虾_USDT", self.metadata))
        return Result((self.earlier,) if self.earlier is not None else None)


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"])
def test_listing_bucket_is_included_for_every_watchlist_timeframe(timeframe):
    step = TIMEFRAME_SECONDS[timeframe]
    conn = Connection({"create_time": LISTED, "name": "龙虾_USDT"})
    result = history_bounds(conn, INSTRUMENT, timeframe, NOW)
    assert result["start"] == LISTED // step * step
    assert result["start"] <= LISTED < result["start"] + step
    assert result["end"] == int(NOW.timestamp()) // step * step - step
    assert result["target_bars"] == (result["end"] - result["start"]) // step + 1
    assert result["requested_start"] == year_bounds(NOW, timeframe)[0]
    assert result["range_policy"] == "LISTING_CLIPPED"
    assert result["listing_time"] == LISTED
    assert "available_at<=%s" in conn.calls[0][0]
    assert "effective_at<=%s" in conn.calls[0][0]
    assert "ORDER BY revision DESC LIMIT 1" in conn.calls[0][0]
    assert "ORDER BY open_time LIMIT 1" in conn.calls[1][0]


@pytest.mark.parametrize("value", [None, 0, 1, -1, True, False, 1.5, "1.5", "nan", "", "１２３", [], {}, LISTED * 1000, int(NOW.timestamp()) + 1, "1" * 1000])
def test_invalid_or_future_listing_metadata_preserves_full_year(value):
    conn = Connection({"create_time": value}, earlier=int(NOW.timestamp()) - 7200)
    result = history_bounds(conn, INSTRUMENT, "1h", NOW)
    start, end, target = year_bounds(NOW, "1h")
    assert (result["start"], result["end"], result["target_bars"]) == (start, end, target)
    assert result["range_policy"] == "ROLLING_YEAR" and result["listing_time"] is None
    assert len(conn.calls) == 1  # Cache age must never be used as listing evidence.


@pytest.mark.parametrize("venue,market,metadata", [
    ("GATE", "SPOT", {"create_time": LISTED}),
    ("OTHER", "FUTURES_USDT", {"create_time": LISTED}),
    ("GATE", "FUTURES_USDT", {"name": "OTHER_USDT", "create_time": LISTED}),
    ("GATE", "FUTURES_USDT", None),
    ("GATE", "FUTURES_USDT", []),
])
def test_only_current_gate_futures_metadata_is_trusted(venue, market, metadata):
    conn = Connection(metadata, venue=venue, market=market)
    assert history_bounds(conn, INSTRUMENT, "1h", NOW)["target_bars"] == 8760
    assert len(conn.calls) == 1


def test_ascii_timestamp_string_and_btc_contract_are_supported():
    conn = Connection({"create_time": str(LISTED)}, market="FUTURES_BTC")
    assert history_bounds(conn, str(INSTRUMENT), "1h", NOW)["listing_time"] == LISTED


def test_earlier_valid_cache_widens_but_later_cache_never_shrinks():
    expected = listing_year_bounds(NOW, "1h", listing_time=LISTED)
    earlier = expected["start"] - 7200
    actual = history_bounds(Connection({"create_time": LISTED}, earlier=earlier), INSTRUMENT, "1h", NOW)
    assert actual["start"] == earlier
    assert actual["target_bars"] == expected["target_bars"] + 2
    assert listing_year_bounds(NOW, "1h", listing_time=LISTED, first_cached_time=LISTED + 100000) == expected
    assert listing_year_bounds(NOW, "1h", first_cached_time=earlier)["target_bars"] == 8760


def test_older_than_one_year_listing_does_not_extend_the_requested_year():
    start, end, target = year_bounds(NOW, "1h")
    actual = history_bounds(Connection({"create_time": start - 1000}), INSTRUMENT, "1h", NOW)
    assert (actual["start"], actual["end"], actual["target_bars"]) == (start, end, target)
    assert actual["range_policy"] == "ROLLING_YEAR"


def test_no_closed_listing_bucket_uses_compatible_nonzero_fallback_then_clips():
    listing = int(NOW.timestamp()) - 10
    conn = Connection({"create_time": listing})
    initial = history_bounds(conn, INSTRUMENT, "1h", NOW)
    assert initial["target_bars"] == 8760 and initial["range_policy"] == "ROLLING_YEAR"
    next_close = datetime.fromtimestamp(listing // 3600 * 3600 + 3600, timezone.utc)
    after = history_bounds(conn, INSTRUMENT, "1h", next_close)
    assert after["target_bars"] == 1 and after["start"] == after["end"]
    assert after["range_policy"] == "LISTING_CLIPPED"
