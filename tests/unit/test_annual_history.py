from datetime import datetime, timedelta, timezone
import gzip
from urllib.parse import quote, urlsplit

import pytest

from gate_client import GateRateLimited
from gatescope.domain.history import year_bounds, eligible_gap
from gatescope.adapters.gate_history import (
    ArchiveFormatError,
    GateHistoryArchive,
    ArchiveUnavailable,
    aggregate_rows,
    month_bounds,
)


@pytest.mark.parametrize(
    "timeframe,count",
    [
        ("1m", 525600),
        ("5m", 105120),
        ("15m", 35040),
        ("30m", 17520),
        ("1h", 8760),
        ("4h", 2190),
        ("8h", 1095),
        ("1d", 365),
    ],
)
def test_rolling_year_has_exact_closed_bar_goal(timeframe, count):
    start, end, n = year_bounds(datetime(2026, 9, 10, 12, 0, 30, tzinfo=timezone.utc), timeframe)
    assert n == count and end < 1789050000
    assert (end - start) / (n - 1) == 365 * 86400 / count


def test_suppressed_month_does_not_starve_older_gaps_or_misalign():
    assert eligible_gap([(0, 3600)], [(1800, 3659)], 60) == (0, 1740)
    assert eligible_gap([(0, 3600)], [(1800, 2399)], 60) == (2400, 3600)
    assert eligible_gap([(0, 3600)], [(0, 3659)], 60) is None
    assert eligible_gap([(0, 100000 * 60)], [], 60) == ((100000 - 899) * 60, 100000 * 60)


def test_aggregation_requires_all_source_bars_and_preserves_ohlcv():
    rows = [(i * 300, 100 + i, 102 + i, 99 + i, 101 + i, 10) for i in range(7)]
    result = list(aggregate_rows(rows, 300, 900))
    assert result == [(0, 100, 104, 99, 103, 30), (900, 103, 107, 102, 106, 30)]
    assert list(aggregate_rows([r for r in rows if r[0] != 300], 300, 900)) == [result[1]]


class Response:
    def __init__(self, code=200, payload=b"", headers=None):
        self.status_code = code
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def iter_content(self, size):
        yield self.payload


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_archive_cached_once_reused_between_15m_and_30m(tmp_path):
    start, _ = month_bounds(1757000000)
    payload = gzip.compress(
        "".join(f"{start + i * 300},10,{101 + i},{102 + i},{99 + i},{100 + i}\n" for i in range(6)).encode()
    )
    session = Session(Response(payload=payload))
    archive = GateHistoryArchive(tmp_path, session)
    instrument = {"symbol": "BTC_USDT", "market": "futures", "settle": "usdt"}
    first, meta = archive.fetch(instrument, "15m", start, start + 900)
    second, _ = archive.fetch(instrument, "30m", start, start)
    assert len(session.calls) == 1 and len(first) == 2 and len(second) == 1 and meta["aggregated"]
    url, options = session.calls[0]
    assert url.startswith("https://download.gatedata.org/futures_usdt/candlesticks_5m/")
    assert not options["allow_redirects"] and "headers" not in options
    assert not list(tmp_path.rglob(".pending-*"))


@pytest.mark.parametrize(
    "code,exception", [(404, ArchiveUnavailable), (302, ValueError), (429, GateRateLimited)]
)
def test_archive_finite_retry_rejects_redirects_and_unknown_files(tmp_path, code, exception):
    archive = GateHistoryArchive(tmp_path, Session(Response(code, headers={"Retry-After": "123"})))
    with pytest.raises(exception) as caught:
        archive.fetch({"symbol": "BTC_USDT", "market": "futures"}, "1m", 1757000000, 1757000060)
    if code == 429:
        assert caught.value.retry_after == 123
    assert not list(tmp_path.rglob(".pending-*"))


def test_archive_corrupt_oversized_line_and_path_escape_rejected(tmp_path):
    archive = GateHistoryArchive(tmp_path, Session(Response(payload=gzip.compress(b"1" * 5000))))
    with pytest.raises(ValueError, match="EXPANSION_LIMIT"):
        archive.fetch({"symbol": "BTC_USDT", "market": "futures"}, "1m", 1757000000, 1757000060)
    assert not list(tmp_path.rglob("*.gz"))
    with pytest.raises(ValueError, match="UNSUPPORTED_ARCHIVE_SYMBOL"):
        archive.fetch({"symbol": "../secret", "market": "futures"}, "1m", 1757000000, 1757000060)


@pytest.mark.parametrize("period,step", [("1m", 60), ("5m", 300)])
def test_chinese_contract_gzip_download_encodes_url_and_reuses_literal_cache(tmp_path, period, step):
    start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    payload = gzip.compress("".join(f"{start + i * step},10,101,102,99,100\n" for i in range(3)).encode())
    session = Session(Response(payload=payload))
    archive = GateHistoryArchive(tmp_path, session)
    instrument = {"symbol": "龙虾_USDT", "market": "futures", "settle": "usdt"}
    first, metadata = archive.fetch(instrument, period, start, start + 2 * step)
    assert first == [(start + i * step, 100, 102, 99, 101, 10) for i in range(3)]
    assert metadata["source"] == "GATE_MONTHLY_ARCHIVE" and not metadata["aggregated"]
    url, options = session.calls[0]
    assert (
        url
        == f"https://download.gatedata.org/futures_usdt/candlesticks_{period}/202608/%E9%BE%99%E8%99%BE_USDT-202608.csv.gz"
    )
    assert url.isascii() and not options["allow_redirects"]
    assert options["timeout"] == (3, 8) and options["stream"] and "headers" not in options
    assert not session.trust_env
    path = tmp_path / f"futures_usdt/candlesticks_{period}/202608/龙虾_USDT-202608.csv.gz"
    assert path.read_bytes() == payload
    # A worker restart must use the same on-disk object, not download it again.
    restarted_session = Session(Response(503))
    second, _ = GateHistoryArchive(tmp_path, restarted_session).fetch(
        instrument, period, start, start + 2 * step
    )
    assert second == first and restarted_session.calls == [] and len(session.calls) == 1
    assert not list(tmp_path.rglob(".pending-*"))


@pytest.mark.parametrize("symbol", ["É_USDT", "Ａ_ＵＳＤＴ", "Abc_USDT", "龙" * 80, "𠀀" * 60])
def test_unicode_identity_is_not_normalized_and_filesystem_byte_limit_is_safe(tmp_path, symbol):
    start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    session = Session(Response(payload=gzip.compress(f"{start},10,101,102,99,100\n".encode())))
    archive = GateHistoryArchive(tmp_path, session)
    rows, _ = archive.fetch({"symbol": symbol, "market": "futures"}, "1m", start, start)
    assert len(rows) == 1
    url = session.calls[0][0]
    assert urlsplit(url).netloc == "download.gatedata.org"
    assert url.endswith("/" + quote(symbol + "-202608.csv.gz", safe=""))
    files = list(tmp_path.rglob("*.gz"))
    assert len(files) == 1 and files[0].name == symbol + "-202608.csv.gz"
    assert len(files[0].name.encode("utf-8")) <= 255


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        None,
        123,
        "A" * 81,
        "𠀀" * 61,
        "../secret",
        "BTC/USDT",
        r"BTC\USDT",
        "BTC.USDT",
        "BTC%2FUSDT",
        "BTC?key=secret",
        "BTC#fragment",
        "https://evil.test",
        "BTC@USDT",
        "BTC:USDT",
        "BTC;USDT",
        "BTC&USDT",
        "BTC=USDT",
        "BTC+USDT",
        "BTC[USDT]",
        "BTC USDT",
        "BTC\nUSDT",
        "BTC\rUSDT",
        "BTC\x00USDT",
        "BTC\x1fUSDT",
        "BTC\u200bUSDT",
        "BTC\u202eUSDT",
        "BTC／USDT",
        "BTC😀USDT",
        "E\u0301_USDT",
    ],
)
def test_invalid_archive_identifiers_fail_before_network_and_never_echo_input(tmp_path, symbol):
    session = Session(Response())
    archive = GateHistoryArchive(tmp_path, session)
    with pytest.raises(ArchiveFormatError) as caught:
        archive.fetch({"symbol": symbol, "market": "futures"}, "1m", 1757000000, 1757000060)
    assert str(caught.value) == caught.value.reason == "UNSUPPORTED_ARCHIVE_SYMBOL"
    assert caught.value.retry_after == 86400 and not session.calls
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "market,settle", [("futures", "../usdt"), ("futures", None), ("unknown", "usdt"), (None, "usdt")]
)
def test_unsupported_archive_market_has_distinct_safe_format_error(tmp_path, market, settle):
    session = Session(Response())
    archive = GateHistoryArchive(tmp_path, session)
    with pytest.raises(ArchiveFormatError, match="UNSUPPORTED_ARCHIVE_MARKET"):
        archive.fetch(
            {"symbol": "龙虾_USDT", "market": market, "settle": settle}, "1m", 1757000000, 1757000060
        )
    assert not session.calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "header,expected", [("123", 123), ("-10", 1), ("bad", 30), ("NaN", 30), ("inf", 30), ("1e100", 30)]
)
def test_archive_rate_limit_retry_after_is_bounded_without_transport_retry(tmp_path, header, expected):
    session = Session(Response(429, headers={"Retry-After": header}))
    with pytest.raises(GateRateLimited) as caught:
        GateHistoryArchive(tmp_path, session).fetch(
            {"symbol": "龙虾_USDT", "market": "futures"}, "1m", 1757000000, 1757000060
        )
    assert caught.value.retry_after == expected and len(session.calls) == 1


def test_archive_rate_limit_accepts_http_date(tmp_path):
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    session = Session(Response(429, headers={"Retry-After": retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")}))
    with pytest.raises(GateRateLimited) as caught:
        GateHistoryArchive(tmp_path, session).fetch(
            {"symbol": "龙虾_USDT", "market": "futures"}, "1m", 1757000000, 1757000060
        )
    assert 118 <= caught.value.retry_after <= 120 and len(session.calls) == 1


def test_sparse_archive_proves_every_hole_in_one_bounded_window(tmp_path):
    start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    payload = gzip.compress(
        "".join(f"{start + i * 60},10,101,102,99,100\n" for i in range(0, 900, 2)).encode()
    )
    session = Session(Response(payload=payload))
    values, meta = GateHistoryArchive(tmp_path, session).fetch(
        {"symbol": "龙虾_USDT", "market": "futures"}, "1m", start, start + 899 * 60
    )
    assert len(values) == 450 and len(meta["missing_ranges"]) == 450
    assert meta["queried_from"] == start and meta["queried_to"] == start + 899 * 60
    assert meta["missing_ranges"] == [(start + i * 60, start + i * 60) for i in range(1, 900, 2)]
    assert len(session.calls) == 1


def test_archive_page_budget_counts_missing_slots_before_downloading(tmp_path):
    start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    session = Session(Response(payload=gzip.compress(b"")))
    with pytest.raises(ValueError, match="ARCHIVE_PAGE_TOO_LARGE"):
        GateHistoryArchive(tmp_path, session).fetch(
            {"symbol": "龙虾_USDT", "market": "futures"}, "1m", start, start + 900 * 60
        )
    assert not session.calls and not list(tmp_path.iterdir())


def test_cross_month_empty_aggregation_proves_only_the_actually_queried_month(tmp_path):
    start = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    # Actual production shape: June's first two 15m groups each lack one 5m
    # source bar; the first fully supported 15m opens at 00:30.
    payload = gzip.compress(
        "".join(f"{start + i * 300},10,101,102,99,100\n" for i in (0, 2, 3, 4, 6, 7, 8)).encode()
    )
    archive = GateHistoryArchive(tmp_path, Session(Response(payload=payload)))
    values, meta = archive.fetch(
        {"symbol": "龙虾_USDT", "market": "futures"}, "15m", start - 898 * 900, start + 900
    )
    assert values == []
    assert meta["queried_from"] == start and meta["queried_to"] == start + 900
    assert meta["missing_ranges"] == [(start, start + 900)]
    assert all(left >= start for left, _ in meta["missing_ranges"])
    values, meta = archive.fetch({"symbol": "龙虾_USDT", "market": "futures"}, "15m", start, start + 2700)
    assert [row[0] for row in values] == [start + 1800]
    assert meta["missing_ranges"] == [(start, start + 900), (start + 2700, start + 2700)]


def test_complete_window_does_not_defer_unqueried_or_existing_bars(tmp_path):
    start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
    payload = gzip.compress("".join(f"{start + i * 60},10,101,102,99,100\n" for i in range(10)).encode())
    values, meta = GateHistoryArchive(tmp_path, Session(Response(payload=payload))).fetch(
        {"symbol": "龙虾_USDT", "market": "futures"}, "1m", start + 60, start + 8 * 60
    )
    assert len(values) == 8 and meta["missing_ranges"] == []
    assert meta["queried_from"] == start + 60 and meta["queried_to"] == start + 8 * 60
