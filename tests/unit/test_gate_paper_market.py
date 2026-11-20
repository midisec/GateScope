import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
import requests

from gatescope.adapters.gate_paper_market import GatePaperMarket, MAX_RESPONSE_BYTES, PaperMarketUnavailable

NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)


def metadata(**kwargs):
    return {"name": "SOL_USDT", "type": "direct", "quanto_multiplier": "1", "order_price_round": "0.01",
            "order_size_min": "0.1", "order_size_max": "300000", "market_order_size_max": "200000",
            "in_delisting": False, "status": "trading", "taker_fee_rate": "0.00075", "enable_decimal": True,
            "mark_price": "100", "funding_rate": "-0.0001", "funding_interval": 28800, **kwargs}


def depth(**kwargs):
    return {"id": 123, "current": NOW.timestamp(), "update": NOW.timestamp() - 0.1,
            "bids": [{"p": "99.99", "s": "100.1"}], "asks": [{"p": "100.01", "s": "101.9"}], **kwargs}


class Response:
    def __init__(self, value=None, *, status=200, headers=None, body=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(value).encode()
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_content(self, _):
        yield from self.chunks if self.chunks is not None else [self.body]


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.auth = ("not-used", "not-sent")
        self.trust_env = True
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def provider(*responses, **kwargs):
    session = Session(responses)
    return GatePaperMarket(session, clock=lambda: NOW, **kwargs), session


def test_only_exact_public_gets_preserve_fractional_sizes_and_original_timestamps():
    market, session = provider(Response(metadata()), Response(depth()))
    snapshot = market.get("SOL_USDT")
    assert snapshot.spec.min_contracts == D("0.1")
    assert snapshot.book.bids[0].contracts == D("100.1")
    assert snapshot.book.source_at == NOW
    assert snapshot.book.updated_at == NOW - timedelta(seconds=0.1)
    assert snapshot.funding_rate == D("-0.0001")
    assert snapshot.to_dict()["funding_rate_unit"] == "DECIMAL_RATIO_PER_FUNDING_INTERVAL"
    assert snapshot.to_dict()["funding_simulated"] is False
    assert session.auth is None and not session.trust_env
    assert [row[0] for row in session.calls] == [
        "https://api.gateio.ws/api/v4/futures/usdt/contracts/SOL_USDT",
        "https://api.gateio.ws/api/v4/futures/usdt/order_book",
    ]
    for _, request in session.calls:
        assert request["allow_redirects"] is False and request["stream"] is True
        assert set(request["headers"]) == {"Accept", "Accept-Encoding", "User-Agent", "X-Gate-Size-Decimal"}
        assert request["headers"]["X-Gate-Size-Decimal"] == "1"
        assert request["timeout"].total <= 5
        assert request["timeout"].connect_timeout <= 1
    assert session.calls[1][1]["params"] == {"contract": "SOL_USDT", "limit": 100, "with_id": "true", "interval": "0"}
    market.close()
    assert session.closed


def test_real_session_starts_without_inherited_credentials_cookies_proxy_or_retries():
    session = requests.Session()
    session.headers["Authorization"] = "not-a-real-token"
    session.auth = ("not-real", "not-real")
    session.cookies["session"] = "not-real"
    session.proxies["https"] = "http://127.0.0.1:9"
    market = GatePaperMarket(session)
    assert session.auth is None and not session.headers and not session.cookies and not session.proxies
    assert session.get_adapter("https://api.gateio.ws").max_retries.total == 0
    market.close()


def test_metadata_cache_is_short_lived_and_unchanged_quotes_share_id():
    ticks = [0.0]
    market, session = provider(Response(metadata()), Response(depth()), Response(depth()),
                               Response(metadata(mark_price="101")), Response(depth()), monotonic=lambda: ticks[0])
    first = market.get("SOL_USDT")
    ticks[0] = 29
    second = market.get("SOL_USDT")
    assert first.book.quote_id == second.book.quote_id
    assert len(session.calls) == 3
    ticks[0] = 31
    third = market.get("SOL_USDT")
    assert third.mark_price == D("101") and len(session.calls) == 5


def test_expired_metadata_failure_never_uses_stale_success():
    ticks = [0.0]
    market, session = provider(Response(metadata()), Response(depth()), Response(status=503), monotonic=lambda: ticks[0])
    market.get("SOL_USDT")
    ticks[0] = 31
    with pytest.raises(PaperMarketUnavailable, match="GATE_PUBLIC_REQUEST_FAILED"):
        market.get("SOL_USDT")
    assert len(session.calls) == 3


@pytest.mark.parametrize("raw", ["30", "3600", "garbage", "nan", "inf", "-1"])
def test_rate_limits_do_not_block_or_retry_forever(raw):
    sleeps = []
    market, session = provider(Response(status=429, headers={"Retry-After": raw}), sleep=sleeps.append)
    with pytest.raises(PaperMarketUnavailable, match="GATE_RATE_LIMITED") as caught:
        market.get("SOL_USDT")
    assert caught.value.retry_after >= 0 and not sleeps and len(session.calls) == 1


def test_retry_after_short_delay_is_honoured_exactly_once():
    sleeps = []
    first = Response(status=429, headers={"Retry-After": "0.1"})
    market, session = provider(first, Response(metadata()), Response(depth()), sleep=sleeps.append)
    market.get("SOL_USDT")
    assert sleeps == [0.1] and len(session.calls) == 3 and first.closed


def test_repeated_short_rate_limit_stops_on_second_response():
    sleeps = []
    market, session = provider(Response(status=429, headers={"Retry-After": "0"}),
                               Response(status=429, headers={"Retry-After": "0"}), sleep=sleeps.append)
    with pytest.raises(PaperMarketUnavailable, match="GATE_RATE_LIMITED"):
        market.get("SOL_USDT")
    assert sleeps == [0] and len(session.calls) == 2


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 500, 503])
def test_redirects_and_http_errors_fail_closed(status):
    market, session = provider(Response(status=status, headers={"Location": "https://example.invalid/"}))
    with pytest.raises(PaperMarketUnavailable, match="GATE_PUBLIC_REQUEST_FAILED"):
        market.get("SOL_USDT")
    assert len(session.calls) == 1


@pytest.mark.parametrize("response", [
    Response(body=b"not json"), Response(body=b'{"value":NaN}'),
    Response([]), Response({"label": "INVALID_CREDENTIALS", "message": "never disclose this"}),
    Response(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
    Response(chunks=[b"x" * MAX_RESPONSE_BYTES, b"x"]),
])
def test_bounded_body_and_business_error_validation(response):
    market, _ = provider(response)
    with pytest.raises(PaperMarketUnavailable) as caught:
        market.get("SOL_USDT")
    assert "never disclose" not in str(caught.value)
    assert response.closed


def test_network_exception_is_sanitized_and_not_retried():
    market, session = provider(requests.ReadTimeout("sensitive URL should not escape"))
    with pytest.raises(PaperMarketUnavailable, match="^GATE_PUBLIC_NETWORK_UNAVAILABLE$"):
        market.get("SOL_USDT")
    assert len(session.calls) == 1


@pytest.mark.parametrize("symbol", ["SOL/BTC", "../SOL_USDT", "SOL_USDT?x=y", "SOL_USDT#x", "SOL_BTC", "SOL_%55SDT", "_USDT", "SOL_USDT\n"])
def test_symbol_cannot_change_host_or_endpoint(symbol):
    market, session = provider()
    with pytest.raises(ValueError):
        market.get(symbol)
    assert not session.calls


def test_unicode_symbol_is_exactly_encoded_as_one_path_segment():
    market, session = provider(Response(metadata(name="龙虾_USDT")), Response(depth()))
    assert market.get("龙虾_USDT").spec.symbol == "龙虾_USDT"
    assert session.calls[0][0].endswith("/%E9%BE%99%E8%99%BE_USDT")
    assert session.calls[1][1]["params"]["contract"] == "龙虾_USDT"


@pytest.mark.parametrize("patch", [
    {"name": "BTC_USDT"}, {"type": "inverse"}, {"in_delisting": True}, {"in_delisting": None},
    {"status": "delisted"}, {"order_size_min": 0}, {"quanto_multiplier": "NaN"},
    {"taker_fee_rate": "-0.1"}, {"order_price_round": "0"}, {"order_size_max": "0"},
])
def test_invalid_contract_is_not_cached(patch):
    market, _ = provider(Response(metadata(**patch)))
    with pytest.raises(PaperMarketUnavailable):
        market.get("SOL_USDT")
    assert not market._cache


@pytest.mark.parametrize("patch", [
    {"id": None}, {"id": True}, {"current": None}, {"current": NOW.timestamp() * 1000},
    {"current": NOW.timestamp() - 6, "update": NOW.timestamp() - 7},
    {"current": NOW.timestamp() + 3}, {"update": NOW.timestamp() + 1},
    {"asks": []}, {"asks": None}, {"asks": [None]}, {"asks": [{"p": "100.001", "s": "1"}]},
    {"asks": [{"p": "99.98", "s": "1"}]}, {"bids": [{"p": "99.99", "s": "0"}]},
    {"asks": [{"p": "100.02", "s": "1"}, {"p": "100.01", "s": "1"}]},
])
def test_invalid_stale_or_misaligned_order_book_never_mints_quote(patch):
    market, _ = provider(Response(metadata()), Response(depth(**patch)))
    with pytest.raises(PaperMarketUnavailable):
        market.get("SOL_USDT")


def test_monotonic_deadline_checked_after_body_consumption():
    ticks = [0]
    class SlowResponse(Response):
        def iter_content(self, _):
            ticks[0] = 6
            yield self.body
    market, session = provider(SlowResponse(metadata()), monotonic=lambda: ticks[0])
    with pytest.raises(PaperMarketUnavailable, match="GATE_QUOTE_BUDGET_EXCEEDED"):
        market.get("SOL_USDT")
    assert len(session.calls) == 1


@pytest.mark.parametrize("limit", [0, 1, 51, 101])
def test_only_bounded_exchange_depth_sizes_supported(limit):
    with pytest.raises(ValueError):
        GatePaperMarket(Session([]), depth_limit=limit)
