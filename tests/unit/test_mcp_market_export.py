from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from psycopg.errors import QueryCanceled

from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_mcp import VerifiedMcpToken
from gatescope.application.mcp_tools import McpAnalysisService, McpDataUnavailable, McpToolDenied
from gatescope.domain.market import TIMEFRAME_SECONDS


IDENTIFIER = "30000000-0000-0000-0000-000000000033"
INSTRUMENT = {"id": IDENTIFIER, "market": "futures_usdt", "settle": "usdt", "symbol": "BTC_USDT",
              "base": "BTC", "quote": "USDT", "status": "ACTIVE"}


def principal(*scopes: str) -> VerifiedMcpToken:
    return VerifiedMcpToken(
        id=UUID(int=1), tenant_id=UUID(int=2), subject_user_id=UUID(int=3), client_id=UUID(int=4),
        scopes=tuple(scopes), expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


class Access:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []

    def audit_invocation(self, identity: VerifiedMcpToken, **payload: Any) -> None:
        self.audits.append(payload)


class Market:
    def __init__(self, count: int = 100, timeframe: str = "15m") -> None:
        self.calls: list[Any] = []
        self.search_results = [INSTRUMENT]
        step = TIMEFRAME_SECONDS[timeframe]
        now = int(datetime.now(timezone.utc).timestamp())
        self.current_open = now // step * step
        self.rows = [{
            "ts": self.current_open - (count - index) * step,
            "open": 100 + index, "high": 103 + index, "low": 99 + index, "close": 102 + index,
            "volume": 10 + index, "updated_at": self.current_open - (count - index - 1) * step,
            "venue": "GATE", "market": "FUTURES_USDT", "revision": 7,
            "is_final": True, "quality_flags": ["HISTORICAL_BACKFILL"],
        } for index in range(count)]
        self.live = {
            "open_time": datetime.fromtimestamp(self.current_open, timezone.utc).isoformat(),
            "close_time": datetime.fromtimestamp(self.current_open + step, timezone.utc).isoformat(),
            "available_at": datetime.now(timezone.utc).isoformat(),
            "open": "199", "high": "209", "low": "198", "close": "205", "volume": "2",
            "is_final": False,
        }

    def get_instrument(self, identifier: str) -> dict[str, Any] | None:
        self.calls.append(("instrument", identifier))
        return INSTRUMENT if identifier == IDENTIFIER else None

    def search_instruments(self, query: str, market: str, limit: int, offset: int) -> list[dict[str, Any]]:
        self.calls.append(("search", query, market, limit, offset))
        return self.search_results

    def get_canonical_candles(self, identifier: str, timeframe: str, start: Any, end: Any, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("candles", identifier, timeframe, start, end, limit))
        return self.rows[-limit:]

    def cache_status(self, identifier: str, timeframe: str) -> dict[str, Any]:
        return {"active": True, "history_status": "COMPLETE", "stale": False, "live_bar": self.live}


class Coinglass:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.context = {
            "version": "coinglass-context-1", "status": "READY", "snapshot_id": "snapshot-1",
            "scope": {"exchange": "Gate", "market": "FUTURES_USDT", "instrument_id": IDENTIFIER},
            "match": {"status": "EXACT"}, "as_of": datetime.now(timezone.utc).isoformat(),
            "series": [{"id": "open_interest", "label": "OI", "unit": "USD", "native_timeframe": "15m",
                        "points": [{"time": 1, "close_time": 2, "available_at": 3, "value": "1000"}]}],
            "quality": {"flags": []},
        }

    def get(self, tenant: UUID, user: UUID, identifier: UUID, timeframe: str, request: bool = False) -> dict[str, Any]:
        self.calls.append((tenant, user, identifier, timeframe, request))
        return self.context


def service(market: Market | None = None, coinglass: Coinglass | None = None) -> tuple[McpAnalysisService, Access]:
    access = Access()
    return McpAnalysisService(market or Market(), object(), access, coinglass=coinglass), access  # type: ignore[arg-type]


def test_combined_snapshot_has_aligned_final_indicators_live_and_authorized_context() -> None:
    market, context = Market(), Coinglass()
    instance, access = service(market, context)
    identity = principal("market:read", "analysis:read")
    result = instance.market_analysis_snapshot(identity, symbol="BTC_USDT", market="futures:usdt", bar_limit=100)
    assert result["version"] == "market-analysis-1"
    assert len(result["candles"]) == 100
    assert result["latest_closed"] == result["candles"][-1]
    assert result["latest_closed"]["revision"] == 7
    assert result["quality_flags"] == ["HISTORICAL_BACKFILL"]
    assert result["realtime"]["candle"]["is_final"] is False
    assert result["realtime"]["used_for_indicators"] is False
    assert len(result["indicators"]) == 8
    assert all(item["open_time"] == result["latest_closed"]["open_time"] for item in result["indicators"])
    assert all(item["bar_revision"] == 7 and item["ready"] for item in result["indicators"])
    assert all(item["parameters"] is not None for item in result["indicators"])
    assert result["statistics"]["return_sample_count"] == 99
    assert result["statistics"]["strategy_win_rate"] is None
    # Reading queues owner-scoped cache warming; it never contacts the provider inline.
    assert context.calls == [(identity.tenant_id, identity.subject_user_id, UUID(IDENTIFIER), "15m", True)]
    assert result["policies"]["coinglass"] == "AUTHORIZED_USER_CACHE_WITH_ASYNC_REFRESH_NO_INLINE_UPSTREAM"
    assert access.audits[-1]["tool_name"] == "market_analysis_snapshot"
    assert len(access.audits) == 1


@pytest.mark.parametrize("tool_name", ["market_analysis_snapshot", "coinglass_get_snapshot"])
def test_same_client_selects_instrument_and_period_per_call_without_shared_selection(
    tool_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_id = str(UUID(int=54))
    instruments = {
        IDENTIFIER: INSTRUMENT,
        second_id: {**INSTRUMENT, "id": second_id, "symbol": "龙虾_USDT", "base": "龙虾"},
    }
    selections = [(IDENTIFIER, "1m"), (second_id, "4h"), (IDENTIFIER, "1d"), (IDENTIFIER, "1m")]
    windows = {key: Market(timeframe=key[1]) for key in selections}
    market, context = Market(), Coinglass()
    for index, window in enumerate(windows.values()):
        for row in window.rows:
            for field in ("open", "high", "low", "close"):
                row[field] += index * 1000

    def search(query: str, selected_market: str, limit: int, offset: int) -> list[dict[str, Any]]:
        market.calls.append(("search", query, selected_market, limit, offset))
        return [item for item in instruments.values() if item["symbol"] == query]

    def candles(identifier: str, timeframe: str, start: Any, end: Any, limit: int) -> list[dict[str, Any]]:
        market.calls.append(("candles", identifier, timeframe, start, end, limit))
        return windows[(identifier, timeframe)].rows[-limit:]

    def cached_context(
        tenant: UUID, user: UUID, identifier: UUID, timeframe: str, request: bool = False,
    ) -> dict[str, Any]:
        context.calls.append((tenant, user, identifier, timeframe, request))
        result = deepcopy(context.context)
        result["snapshot_id"] = f"{identifier}:{timeframe}"
        result["scope"]["instrument_id"] = str(identifier)
        result["series"][0]["native_timeframe"] = timeframe
        return result

    monkeypatch.setattr(market, "search_instruments", search)
    monkeypatch.setattr(market, "get_canonical_candles", candles)
    monkeypatch.setattr(
        market, "cache_status",
        lambda identifier, timeframe: windows[(identifier, timeframe)].cache_status(identifier, timeframe),
    )
    monkeypatch.setattr(context, "get", cached_context)
    instance, access = service(market, context)
    identity = principal("mcp:connect", "market:read", "analysis:read")
    tool = getattr(instance, tool_name)
    results = [
        tool(identity, symbol=instruments[identifier]["symbol"], market="futures:usdt", timeframe=timeframe)
        for identifier, timeframe in selections
    ]

    for result, (identifier, timeframe) in zip(results, selections, strict=True):
        assert result["instrument"]["id"] == identifier
        assert result["timeframe"] == timeframe
        assert result["coinglass"]["snapshot_id"] == f"{identifier}:{timeframe}"
        assert result["coinglass"]["scope"]["instrument_id"] == identifier
        assert result["coinglass"]["series"][0]["native_timeframe"] == timeframe
        if tool_name == "market_analysis_snapshot":
            assert len(result["candles"]) == 100
            assert result["latest_closed"]["instrument_id"] == identifier
            assert result["latest_closed"]["timeframe"] == timeframe
            assert result["latest_closed"]["close"] == str(windows[(identifier, timeframe)].rows[-1]["close"])
    assert results[0]["coinglass"] == results[-1]["coinglass"]
    assert context.calls == [
        (identity.tenant_id, identity.subject_user_id, UUID(identifier), timeframe, True)
        for identifier, timeframe in selections
    ]
    assert [event["tool_name"] for event in access.audits] == [tool_name] * len(selections)
    if tool_name == "market_analysis_snapshot":
        assert [(call[1], call[2]) for call in market.calls if call[0] == "candles"] == selections
        assert results[0]["candles"] == results[-1]["candles"]
    else:
        assert all(call[0] != "candles" for call in market.calls)


@pytest.mark.parametrize("scopes", [("analysis:read",), ("market:read",), ()])
def test_combined_denies_before_any_market_or_provider_read(scopes: tuple[str, ...]) -> None:
    market, context = Market(), Coinglass()
    instance, access = service(market, context)
    with pytest.raises(McpToolDenied):
        instance.market_analysis_snapshot(principal(*scopes), instrument_id=IDENTIFIER)
    assert market.calls == [] and context.calls == []
    assert access.audits[-1]["outcome"] == "DENIED"


@pytest.mark.parametrize("arguments", [
    {}, {"symbol": "BTC"}, {"symbol": "BTC_USDT", "market": "all"},
    {"symbol": "BTC_USDT", "market": "spot"},
    {"symbol": " BTC_USDT", "market": "futures:usdt"},
    {"instrument_id": "not-a-uuid"},
    {"instrument_id": IDENTIFIER, "symbol": "BTC_USDT"},
    {"instrument_id": IDENTIFIER, "market": "futures:usdt"},
    {"instrument_id": IDENTIFIER, "timeframe": "3m"},
    {"instrument_id": IDENTIFIER, "bar_limit": 2_001},
    {"instrument_id": IDENTIFIER, "bar_limit": 49},
    {"instrument_id": IDENTIFIER, "indicator_ids": []},
    {"instrument_id": IDENTIFIER, "indicator_ids": ["rsi", "rsi"]},
    {"instrument_id": IDENTIFIER, "indicator_ids": ["unknown"]},
])
def test_combined_rejects_ambiguous_unbounded_or_unknown_inputs(arguments: dict[str, Any]) -> None:
    instance, access = service()
    with pytest.raises(ValueError):
        instance.market_analysis_snapshot(principal("market:read", "analysis:read"), **arguments)
    assert access.audits[-1]["safe_error_code"] == "INVALID_ARGUMENT"


def test_exact_symbol_still_rejects_duplicate_instruments() -> None:
    market = Market()
    market.search_results = [INSTRUMENT, {**INSTRUMENT, "id": str(UUID(int=50))}]
    instance, _ = service(market)
    with pytest.raises(ValueError, match="ambiguous"):
        instance.market_analysis_snapshot(principal("market:read", "analysis:read"), symbol="BTC_USDT", market="futures:usdt")


def test_gap_restarts_indicator_warmup_and_does_not_invent_adjacent_returns() -> None:
    market = Market()
    del market.rows[89]
    instance, _ = service(market)
    result = instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert result["quality"]["missing_bars"] == 1
    assert result["quality"]["contiguous_indicator_bars"] == 10
    assert "NON_CONTIGUOUS_WINDOW" in result["quality_flags"]
    assert result["statistics"]["return_sample_count"] == 97
    rsi = next(item for item in result["indicators"] if item["indicator_id"] == "rsi")
    assert rsi["ready"] is False and rsi["values"]["rsi"] is None
    assert rsi["bars_evaluated"] == 10


def test_future_and_non_final_candles_are_never_used_as_closed_evidence() -> None:
    market = Market()
    market.rows[-1]["is_final"] = False
    market.rows[-2]["updated_at"] = int(datetime.now(timezone.utc).timestamp()) + 60
    market.rows[-3]["updated_at"] = market.rows[-3]["ts"]
    instance, _ = service(market)
    result = instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert len(result["candles"]) == 97
    assert {"NON_FINAL_CANDLE_EXCLUDED", "FUTURE_CANDLE_EXCLUDED", "INVALID_AVAILABILITY_EXCLUDED"} <= set(result["quality_flags"])
    assert all(item["open_time"] == result["latest_closed"]["open_time"] for item in result["indicators"])


def test_empty_cache_stays_null_and_reports_indicator_readiness() -> None:
    instance, _ = service(Market(0))
    result = instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert result["latest_closed"] is None
    assert result["statistics"]["high"] is None
    assert result["coinglass"]["status"] == "NO_DATA"
    assert all(item["ready"] is False for item in result["indicators"])
    assert "NO_CLOSED_CANDLES" in result["quality_flags"]


def test_coinglass_cache_states_and_no_sensitive_or_gateway_details() -> None:
    context = Coinglass()
    context.context.update({"status": "WARMING", "api_key": "private-value", "account": {"remaining": 999}})
    context.context["scope"]["credential"] = "private-value"
    context.context["series"][0]["raw_response"] = {"credential": "private-value"}
    context.context.update(as_of=100, fetched_at=200)
    context.context["series"][0].update(unit_status="UNKNOWN", quality_flags=["UNIT_UNKNOWN_OR_MISMATCH"], funding_interval_seconds=28800)
    instance, _ = service(coinglass=context)
    result = instance.coinglass_get_snapshot(principal("analysis:read"), instrument_id=IDENTIFIER)
    assert result["coinglass"]["status"] == "WARMING"
    assert "private-value" not in repr(result) and "remaining" not in repr(result)
    assert context.calls[0][-1] is True
    assert result["data_watermark"] == 200 and result["market_period_as_of"] == 100
    assert result["coinglass"]["series"][0]["unit_status"] == "UNKNOWN"
    assert result["coinglass"]["series"][0]["quality_flags"] == ["UNIT_UNKNOWN_OR_MISMATCH"]


@pytest.mark.parametrize("tool_name", ["market_analysis_snapshot", "coinglass_get_snapshot"])
def test_coinglass_refresh_contract_preserves_original_times_and_safe_cooldown(tool_name: str) -> None:
    context = Coinglass()
    context.context.update(status="STALE", fetched_at=100, expires_at=200, as_of=90,
        retry_at=500, retry_after_seconds=30, last_attempt_at=470, refresh_status="RATE_LIMITED",
        cache_retained=True, next_refresh_at=500, refresh_requested=True,
        last_refresh_status="RATE_LIMITED", last_safe_errors=[{"group": "oi", "code": "RATE_LIMITED"}], last_reason="PROVIDER_RATE_LIMITED",
        account={"remaining": 123}, provider_key="not-exported-test-value")
    context.context["series"][0].update(status="STALE", fetched_at=100, expires_at=200, as_of=90,
        cache_retained=True, refresh_status="DEFERRED")
    original = deepcopy(context.context)
    instance, _ = service(coinglass=context)
    result = getattr(instance, tool_name)(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    exported = result["coinglass"]
    for field in ("status", "fetched_at", "expires_at", "as_of", "retry_at", "retry_after_seconds",
                  "last_attempt_at", "refresh_status", "cache_retained", "next_refresh_at", "refresh_requested",
                  "last_refresh_status", "last_safe_errors", "last_reason"):
        assert exported[field] == original[field]
    assert exported["series"][0]["fetched_at"] == 100
    assert exported["series"][0]["expires_at"] == 200
    assert exported["series"][0]["status"] == "STALE"
    assert exported["series"][0]["as_of"] == 90
    assert exported["series"][0]["cache_retained"] is True
    assert exported["series"][0]["refresh_status"] == "DEFERRED"
    assert exported["series"][0]["points"] == original["series"][0]["points"]
    assert "not-exported-test-value" not in repr(result) and "remaining" not in repr(result)
    assert context.context == original and context.calls[0][-1] is True


def test_stale_live_candle_is_separate_and_not_marked_fresh() -> None:
    market = Market()
    market.live["available_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    instance, _ = service(market)
    result = instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert result["realtime"]["status"] == "STALE"
    assert result["realtime"]["used_for_indicators"] is False


class QueryConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> QueryConnection:
        self.queries.append((sql, params))
        return self

    def fetchone(self) -> tuple[int]:
        return (200,)

    def fetchall(self) -> list[tuple[Any, ...]]:
        values = [100, 200] if "open_time ASC" in self.queries[-1][0] else [200, 100]
        return [(value, Decimal(1), Decimal(2), Decimal(1), Decimal(2), Decimal(10), value + 60,
                 "GATE", "FUTURES_USDT", 3, [], True) for value in values]


class QueryPool:
    def __init__(self) -> None:
        self.connection = QueryConnection()

    @contextmanager
    def public_transaction(self) -> Any:
        yield self.connection


@pytest.mark.parametrize("start,end,latest", [(None, None, True), (100, None, False), (None, 200, False), (100, 200, False)])
def test_canonical_latest_selection_and_explicit_historical_range_are_distinct(start: Any, end: Any, latest: bool, monkeypatch) -> None:
    pool = QueryPool()
    database = PostgresMarketReadDatabase(pool)  # type: ignore[arg-type]
    monkeypatch.setattr(database, "latest_candle_time", lambda identifier, timeframe: 200)
    rows = database.get_canonical_candles(IDENTIFIER, "1m", start, end, 2)
    sql, params = pool.connection.queries[-1]
    assert f"open_time {'DESC' if latest else 'ASC'}, revision DESC" in sql
    assert "is_final=true" in sql and "available_at<=now()" in sql
    assert [row["ts"] for row in rows] == [100, 200]
    assert rows[-1]["revision"] == 3
    assert ("open_time >= to_timestamp(%s)" in sql) == (start is not None or latest)
    assert ("open_time <= to_timestamp(%s)" in sql) == (end is not None or latest)
    if latest:
        assert params[2:4] == [140, 200]
        assert "DISTINCT ON (open_time)" in sql
        assert "ORDER BY open_time DESC, revision DESC" in sql
        assert "venue=(SELECT venue FROM instruments WHERE id=%s)" in sql
        assert "market=(SELECT market FROM instruments WHERE id=%s)" in sql
        assert pool.connection.queries[0][0] == "SET LOCAL statement_timeout='5s'"
    else:
        assert "DISTINCT ON (venue, market, instrument_id, timeframe, open_time)" in sql
    assert len(pool.connection.queries) == 3
    assert pool.connection.queries[:2] == [
        ("SET LOCAL statement_timeout='5s'", None), ("SET LOCAL lock_timeout='1s'", None),
    ]
    assert params[-1] == 2


def test_canonical_latest_no_cache_does_not_fall_back_to_unbounded_sort(monkeypatch) -> None:
    pool = QueryPool()
    database = PostgresMarketReadDatabase(pool)  # type: ignore[arg-type]
    monkeypatch.setattr(database, "latest_candle_time", lambda identifier, timeframe: None)
    assert database.get_canonical_candles(IDENTIFIER, "1m", None, None, 100) == []
    assert pool.connection.queries == []


def test_latest_watermark_lookup_also_has_local_query_timeout() -> None:
    pool = QueryPool()
    database = PostgresMarketReadDatabase(pool)  # type: ignore[arg-type]
    assert database.latest_candle_time(IDENTIFIER, "1m") == 200
    assert pool.connection.queries[0][0] == "SET LOCAL statement_timeout='5s'"
    assert "open_time >= %s" in pool.connection.queries[1][0]


def test_mcp_query_timeout_is_safe_audited_and_retryable(monkeypatch) -> None:
    market = Market()
    instance, access = service(market)

    def slow(*args):
        raise QueryCanceled("private query and transport details")

    monkeypatch.setattr(market, "get_canonical_candles", slow)
    with pytest.raises(McpDataUnavailable, match="retry after five seconds") as caught:
        instance.market_analysis_snapshot(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER)
    assert "private query" not in str(caught.value)
    assert access.audits[-1]["outcome"] == "ERROR"
    assert access.audits[-1]["safe_error_code"] == "DATA_QUERY_TIMEOUT"
