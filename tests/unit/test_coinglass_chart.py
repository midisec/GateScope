from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from gatescope.adapters.coinglass import CoinglassClient
from gatescope.adapters.coinglass_chart import fetch_chart

OPEN = 1_800_000_000
NOW = OPEN + 180
INSTRUMENT = {
    "id": "local-btc",
    "venue": "GATE",
    "market": "FUTURES_USDT",
    "symbol": "BTC_USDT",
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "settle_asset": "USDT",
}
PAIRS = {
    "Gate": [
        {
            "instrument_id": "BTC_USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "settlement_currency": "USDT",
            "funding_interval": 8,
        }
    ]
}
KEY = "cg_" + "A" * 40


class Response:
    def __init__(self, data=None, *, status=200, code="0", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.payload = {"code": code, "data": data}
        self.content = json.dumps(self.payload).encode()

    def json(self):
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def responses():
    return [
        Response([{"time": OPEN * 1000, "close": "100"}]),
        Response([{"time": OPEN * 1000, "close": ".01"}]),
        Response([{"time": OPEN * 1000, "global_account_long_short_ratio": 1.2}]),
        Response([{"time": OPEN * 1000, "long_liquidation_usd": 10, "short_liquidation_usd": 20}]),
    ]


def test_fixed_origin_exact_queries_and_default_unknown_funding():
    session = Session(Response(PAIRS), *responses())
    result = fetch_chart(CoinglassClient(KEY, session=session), INSTRUMENT, "1m", NOW)
    assert len(session.calls) == 5
    assert result["match"]["status"] == "EXACT"
    assert result["status"] == "PARTIAL"
    assert result["series"][1]["unit_status"] == "UNKNOWN"
    assert not result["series"][1]["points"]
    assert len(result["snapshot_id"]) == 64
    assert KEY not in json.dumps(result)
    for call in session.calls:
        assert urlparse(call["url"]).hostname == "vip2.coinglass.site"
        assert KEY not in call["url"]
        assert call["headers"]["X-API-Key"] == KEY
        assert call["allow_redirects"] is False
    query = parse_qs(urlparse(session.calls[1]["url"]).query)
    assert query == {
        "exchange": ["Gate"],
        "symbol": ["BTC_USDT"],
        "interval": ["1m"],
        "limit": ["256"],
        "end_time": [str(NOW * 1000)],
        "unit": ["usd"],
    }


def test_verified_funding_contract_has_no_extra_scaling():
    session = Session(*responses())
    result = fetch_chart(
        CoinglassClient(KEY, session=session),
        INSTRUMENT,
        "1m",
        NOW,
        supported_pairs=PAIRS,
        funding_unit_status="VERIFIED",
    )
    assert result["status"] == "READY"
    assert result["series"][1]["points"][0]["value"] == 0.01


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_history_429_stops_every_following_call_and_never_retries(position):
    items = responses()
    items[position] = Response(status=429, headers={"Retry-After": "120"})
    session = Session(*items)
    sleeps = []
    client = CoinglassClient(KEY, session=session, sleep=sleeps.append)
    result = fetch_chart(client, INSTRUMENT, "1m", NOW, supported_pairs=PAIRS)
    assert len(session.calls) == position + 1
    assert sleeps == []
    assert result["retry_after_seconds"] == 120
    assert any(series["status"] == "RATE_LIMITED" for series in result["series"])
    assert client.MAX_ATTEMPTS == 2


def test_catalog_429_does_not_continue_or_disclose_raw_error():
    session = Session(Response(status=429, headers={"Retry-After": "2"}))
    result = fetch_chart(CoinglassClient(KEY, session=session), INSTRUMENT, "1m", NOW)
    assert result["status"] == "RATE_LIMITED"
    assert result["retry_after_seconds"] == 2
    assert len(session.calls) == 1


def test_http_200_business_failure_is_local_partial_and_safe():
    items = responses()
    items[0] = Response({"raw": KEY}, code="10001")
    result = fetch_chart(
        CoinglassClient(KEY, session=Session(*items)), INSTRUMENT, "1m", NOW, supported_pairs=PAIRS
    )
    assert result["status"] == "PARTIAL"
    assert result["series"][0]["status"] == "ERROR"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize("change", [{"venue": None}, {"market": "spot"}, {"symbol": "../secret"}])
def test_bad_local_dimensions_never_make_http_request(change):
    session = Session()
    result = fetch_chart(CoinglassClient(KEY, session=session), {**INSTRUMENT, **change}, "1m", NOW)
    assert result["status"] == "UNSUPPORTED"
    assert session.calls == []


def test_no_exact_pair_never_fetches_a_fuzzy_replacement():
    session = Session()
    result = fetch_chart(
        CoinglassClient(KEY, session=session), INSTRUMENT, "1m", NOW, supported_pairs={"Gate": []}
    )
    assert result["status"] == "UNSUPPORTED"
    assert session.calls == []


def test_single_attempt_option_cannot_expand_existing_policy():
    client = CoinglassClient(KEY, session=Session())
    with pytest.raises(ValueError):
        client._get("/api/futures/supported-exchange-pairs", {}, max_attempts=3)


def test_worker_skips_unusable_funding_and_spaces_meaningful_reads(monkeypatch):
    items = responses()
    session = Session(items[0], items[2], items[3])
    sleeps = []
    monkeypatch.setattr("gatescope.adapters.coinglass_chart.time.sleep", sleeps.append)
    result = fetch_chart(
        CoinglassClient(KEY, session=session),
        INSTRUMENT,
        "1m",
        NOW,
        supported_pairs=PAIRS,
        skip_unverified_funding=True,
        request_spacing_seconds=2.1,
    )
    assert len(session.calls) == 3 and sleeps == [2.1, 2.1]
    assert all("funding-rate" not in item["url"] for item in session.calls)
    assert result["series"][1]["unit_status"] == "UNKNOWN"
    assert len([s for s in result["series"] if s["points"]]) == 4


def test_progress_exposes_real_first_metric_before_later_http_and_keeps_full_identity():
    items = responses()
    session = Session(items[0], items[2], items[3])
    snapshots = []
    calls_at_progress = []

    def progress(bundle):
        calls_at_progress.append(len(session.calls))
        snapshots.append(bundle)

    result = fetch_chart(
        CoinglassClient(KEY, session=session),
        INSTRUMENT,
        "1m",
        NOW,
        supported_pairs=PAIRS,
        skip_unverified_funding=True,
        on_progress=progress,
    )
    assert calls_at_progress == [1, 2, 3]
    first = snapshots[0]
    assert first["status"] == "PARTIAL" and first["refresh_status"] == "COLLECTING"
    assert first["instrument_id"] == INSTRUMENT["id"] and first["timeframe"] == "1m"
    assert first["match"]["status"] == "EXACT" and first["scope"] == result["scope"]
    assert {row["id"] for row in first["series"]} == {
        "oi",
        "funding",
        "lsr",
        "liquidation_long",
        "liquidation_short",
    }
    assert [row["id"] for row in first["series"] if row["points"]] == ["oi"]
    assert all(
        row["status"] == "DEFERRED"
        for row in first["series"]
        if row["id"] in {"lsr", "liquidation_long", "liquidation_short"}
    )
    assert next(row for row in first["series"] if row["id"] == "funding")["unit_status"] == "UNKNOWN"
    oi = first["series"][0]
    assert oi["fetched_at"] == oi["points"][0]["available_at"]
    assert oi["expires_at"] > oi["fetched_at"] and oi["as_of"] == OPEN + 60
    assert len(first["snapshot_id"]) == 64 and first["snapshot_id"] != snapshots[-1]["snapshot_id"]
    assert KEY not in json.dumps(snapshots)
    # Earlier callbacks stay frozen when later groups are collected.
    assert len([row for row in first["series"] if row["points"]]) == 1


def test_progress_never_publishes_empty_or_error_only_collections():
    session = Session(Response([]), Response(status=429, headers={"Retry-After": "60"}))
    snapshots = []
    result = fetch_chart(
        CoinglassClient(KEY, session=session),
        INSTRUMENT,
        "1m",
        NOW,
        supported_pairs=PAIRS,
        skip_unverified_funding=True,
        on_progress=snapshots.append,
    )
    assert snapshots == [] and result["status"] == "RATE_LIMITED"
    assert len(session.calls) == 2


def test_progress_callback_cannot_mutate_the_final_provider_evidence():
    items = responses()
    session = Session(items[0], items[2], items[3])

    def mutate(bundle):
        bundle["scope"]["symbol"] = "UNTRUSTED"
        bundle["series"][0]["points"][0]["value"] = 0

    result = fetch_chart(
        CoinglassClient(KEY, session=session),
        INSTRUMENT,
        "1m",
        NOW,
        supported_pairs=PAIRS,
        skip_unverified_funding=True,
        on_progress=mutate,
    )
    assert result["scope"]["symbol"] == "BTC_USDT"
    assert result["series"][0]["points"][0]["value"] == 100
