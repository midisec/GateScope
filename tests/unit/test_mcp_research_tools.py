from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from psycopg.errors import LockNotAvailable, QueryCanceled
from mcp.server.transport_security import TransportSecuritySettings

from apps.mcp_server.main import _principal
from apps.mcp_server.research import ResearchMCPServer, TOOL_SCOPES, register_research_tools
from gatescope.application.mcp_tools import McpAnalysisService, McpDataUnavailable, McpToolDenied
from tests.unit.test_mcp_market_export import Access, Coinglass, IDENTIFIER, Market, principal


class Structures:
    def __init__(self):
        self.calls = []

    def current(self, instrument, timeframe, **kwargs):
        self.calls.append(("current", instrument, timeframe, kwargs))
        return {"items": [{"id": "case", "context_id": "context", "phase": "FORMING",
                           "confidence": 0.8, "initial_prediction": {"target": 200},
                           "historical_reference": {"sample_count": 0, "rate": None}}],
                "summary": {"total": 1}, "status": "READY", "limit": kwargs["limit"]}

    def study(self, instrument, timeframe, **kwargs):
        self.calls.append(("study", instrument, timeframe, kwargs))
        return {"items": [], "status": "NOT_REQUESTED", "report": None}

    def events(self, instrument, timeframe, **kwargs):
        self.calls.append(("events", instrument, timeframe, kwargs))
        return {"items": [{"phase": "INVALIDATED", "source_mode": kwargs["source"]}], "has_more": False}

    def detail(self, instrument, timeframe, context, candidate, **kwargs):
        self.calls.append(("detail", instrument, timeframe, context, candidate, kwargs))
        if candidate == "absent":
            return None
        return {"items": [], "chart": {"geometry": {"lines": []}}, "source_mode": "RECORDED"}


class Research:
    def __init__(self):
        self.calls = []

    def watchlist(self, tenant, user, **kwargs):
        self.calls.append(("watchlist", tenant, user, kwargs))
        return {"items": [{"instrument_id": UUID(IDENTIFIER)}], "has_more": False}

    def backtests(self, tenant, user, **kwargs):
        self.calls.append(("backtests", tenant, user, kwargs))
        return {"items": [{"run_id": UUID(int=42)}], "has_more": False}

    def backtest_detail(self, tenant, user, run, **kwargs):
        self.calls.append(("detail", tenant, user, run, kwargs))
        if run == UUID(int=404):
            raise LookupError("RESEARCH_NOT_FOUND")
        return {"items": [], "section": kwargs["section"], "availability": "NOT_APPLICABLE"}


def service():
    access, structures, research = Access(), Structures(), Research()
    instance = McpAnalysisService(Market(), object(), access, coinglass=Coinglass(),
                                 structures=structures, research=research)
    return instance, access, structures, research


def test_structure_tools_forward_filters_identity_and_do_not_invent_statistics():
    instance, access, reader, _ = service()
    identity = principal("analysis:read")
    result = instance.structure_current(identity, instrument_id=IDENTIFIER, family="wave", confidence_min=.7,
                                        confidence_max=.9, phase="FORMING", limit=5, offset=10)
    assert reader.calls[0][1:3] == (UUID(IDENTIFIER), "15m")
    assert reader.calls[0][-1]["family"] == "wave"
    assert reader.calls[0][-1]["offset"] == 10
    assert result["items"][0]["historical_reference"]["rate"] is None
    assert result["research_only"] and "NO_ORDERS" in result["read_policy"]
    assert "principal" not in access.audits[-1]["arguments"]
    assert instance.structure_study(identity, instrument_id=IDENTIFIER)["status"] == "NOT_REQUESTED"
    result = instance.structure_event_list(identity, instrument_id=IDENTIFIER, source="REPLAY", phase="INVALIDATED")
    assert result["items"][0]["source_mode"] == "REPLAY"
    result = instance.structure_event_detail(identity, instrument_id=IDENTIFIER, context_id="context", candidate_id="case", bar_limit=0)
    assert result["chart"]["geometry"]["lines"] == []
    assert reader.calls[-1][-1]["bar_limit"] == 0


@pytest.mark.parametrize("method,kwargs", [
    ("structure_current", {"confidence_min": float("nan")}),
    ("structure_current", {"confidence_min": .8, "confidence_max": .7}),
    ("structure_current", {"unscored_only": True, "confidence_min": .8}),
    ("structure_current", {"family": "unknown"}),
    ("structure_current", {"phase": "WON"}),
    ("structure_current", {"limit": True}),
    ("structure_current", {"offset": -1}),
    ("structure_event_list", {"source": "ALL"}),
    ("structure_event_list", {"context_id": "a" * 129}),
    ("structure_event_detail", {"context_id": "x", "candidate_id": "x", "bar_limit": 501}),
    ("structure_study", {"timeframe": "2h"}),
])
def test_bad_structure_inputs_are_audited_and_rejected_before_reads(method, kwargs):
    instance, access, reader, _ = service()
    with pytest.raises(ValueError):
        getattr(instance, method)(principal("analysis:read"), instrument_id=IDENTIFIER, **kwargs)
    assert not reader.calls
    assert access.audits[-1]["safe_error_code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("method,kwargs", [
    ("watchlist_list", {}), ("research_backtest_list", {}),
    ("research_backtest_detail", {"run_id": str(UUID(int=42))}),
])
def test_private_research_requires_explicit_scope_before_any_data_read(method, kwargs):
    instance, access, _, reader = service()
    with pytest.raises(McpToolDenied, match="research:read"):
        getattr(instance, method)(principal("market:read", "analysis:read"), **kwargs)
    assert not reader.calls and access.audits[-1]["outcome"] == "DENIED"
    identity = principal("research:read")
    result = getattr(instance, method)(identity, **kwargs)
    assert reader.calls[-1][1:3] == (identity.tenant_id, identity.subject_user_id)
    assert result["research_only"]


def test_missing_private_or_public_evidence_is_indistinguishable_safe_error():
    instance, access, _, _ = service()
    with pytest.raises(McpDataUnavailable, match="not found or not accessible"):
        instance.research_backtest_detail(principal("research:read"), run_id=str(UUID(int=404)))
    assert access.audits[-1]["safe_error_code"] == "EVIDENCE_NOT_FOUND"
    with pytest.raises(McpDataUnavailable, match="not found or not accessible"):
        instance.structure_event_detail(principal("analysis:read"), instrument_id=IDENTIFIER,
                                         context_id="context", candidate_id="absent")


def test_indicator_series_explicit_parameters_pagination_and_gap_warmup():
    instance, _, _, _ = service()
    identity = principal("market:read", "analysis:read")
    first = instance.indicator_get_series(identity, instrument_id=IDENTIFIER, indicator_id="ema",
                                         parameters={"period": 20}, limit=12)
    assert len(first["items"]) == 12 and first["parameters"] == {"period": 20}
    assert first["next_offset"] == 12 and first["items"][0]["ready"] is False
    second = instance.indicator_get_series(identity, instrument_id=IDENTIFIER, indicator_id="ema",
        parameters={"period": 20}, offset=12, limit=12, end_epoch=first["window_end_epoch"])
    assert second["items"][0]["open_time"] > first["items"][-1]["open_time"]
    assert "NOT_POINT_IN_TIME" in second["policies"]["historical_availability"]
    del instance.market.rows[-11]
    broken = instance.indicator_get_series(identity, instrument_id=IDENTIFIER, parameters={"period": 20})
    assert broken["quality"]["missing_bars"] == 1 and broken["total"] == 10
    assert all(not item["ready"] for item in broken["items"])


@pytest.mark.parametrize("kwargs", [
    {"parameters": {"period": True}}, {"parameters": {"period": float("inf")}},
    {"parameters": {"secret": "not-supported"}}, {"parameters": []},
    {"indicator_id": "unknown"}, {"end_epoch": -1}, {"limit": 501},
    {"bar_limit": 2001}, {"end_epoch": 999999999999},
])
def test_indicator_series_rejects_invalid_inputs(kwargs):
    instance, _, _, _ = service()
    with pytest.raises(ValueError):
        instance.indicator_get_series(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER, **kwargs)


def test_multi_period_bundle_is_local_only_and_explicitly_non_atomic():
    instance, access, reader, _ = service()
    result = instance.market_research_bundle(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER, timeframes=["15m"], bar_limit=100)
    assert len(result["items"]) == 1 and result["items"][0]["status"] == "READ"
    assert reader.calls[-1][-1]["limit"] == 5
    assert instance.coinglass.calls[-1][-1] is False
    assert "NON_ATOMIC" in result["alignment_policy"]
    assert access.audits[-1]["tool_name"] == "market_research_bundle"


@pytest.mark.parametrize("error", [QueryCanceled, LockNotAvailable])
def test_bundle_reports_query_failure_without_fabricating_a_snapshot(monkeypatch, error):
    instance, _, _, _ = service()
    def timed_out(*args, **kwargs):
        raise error()
    monkeypatch.setattr(instance, "market_preview", timed_out)
    result = instance.market_research_bundle(principal("market:read", "analysis:read"),
                                             instrument_id=IDENTIFIER, timeframes=["1h"])
    assert result["items"] == [{"timeframe": "1h", "status": "NOT_READ", "reason": "DATA_QUERY_TIMEOUT"}]


@pytest.mark.parametrize("periods", [[], ["1m"]*2, ["1m", "5m", "15m", "30m", "1h"], ["2h"], "1h", [None]])
def test_bundle_rejects_ambiguous_or_excessive_timeframes(periods):
    instance, _, _, _ = service()
    with pytest.raises(ValueError):
        instance.market_research_bundle(principal("market:read", "analysis:read"), instrument_id=IDENTIFIER, timeframes=periods)


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h"])
def test_chart_uses_exact_closed_candles_ema9_21_rsi14_and_time_metadata(timeframe):
    from gatescope.application.mcp_chart import render_research_chart
    from gatescope.indicators import IndicatorNode, compute_batch
    from gatescope.application.mcp_tools import _final_bars
    instance, _, _, _ = service()
    instance.market = Market(count=250, timeframe=timeframe)
    result = instance.market_chart_snapshot(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER, timeframe=timeframe, bar_limit=100)
    assert len(result["candles"]) == 100 and all(item["is_final"] for item in result["candles"])
    assert result["instrument"]["venue"] == "GATE" and result["includes_forming_candle"] is False
    assert result["last_closed_at"] == result["candles"][-1]["close_time"]
    assert {item["instance_id"]: item["parameters"] for item in result["indicators"]} == {
        "ema9": {"period": 9}, "ema21": {"period": 21}, "rsi14": {"period": 14}}
    bars, _ = _final_bars(result["instrument"], IDENTIFIER, timeframe, instance.market.rows,
                          datetime.fromisoformat(result["captured_at"]))
    points = compute_batch(instance.registry, [IndicatorNode("ema9", "ema", {"period": 9})], bars)
    assert result["overlays"]["ema9"][-1]["value"] == points[-1].values["ema"]
    assert render_research_chart(result).startswith(b"\x89PNG")


def test_optional_forming_is_never_used_for_chart_indicators():
    instance, _, _, _ = service()
    identity = principal("market:read", "analysis:read")
    closed = instance.market_chart_snapshot(identity, instrument_id=IDENTIFIER, bar_limit=50)
    forming = instance.market_chart_snapshot(identity, instrument_id=IDENTIFIER, bar_limit=50, include_forming=True)
    assert forming["includes_forming_candle"] and not forming["candles"][-1]["is_final"]
    assert len(forming["candles"]) == 50 and forming["closed_bar_count"] == 49
    for key in forming["overlays"]:
        assert forming["overlays"][key][-1] == closed["overlays"][key][-1]
        assert forming["overlays"][key][-1]["open_time"] != forming["candles"][-1]["open_time"]


def test_oauth_research_scope_does_not_unlock_legacy_paper_tools(monkeypatch):
    token = SimpleNamespace(client_id=str(uuid4()), scopes=["mcp:connect", "research:read"],
        expires_at=int((datetime.now(timezone.utc)+timedelta(minutes=10)).timestamp()),
        claims={"oauth_market_only": True, "token_id": str(uuid4()), "tenant_id": str(uuid4()),
                "subject_user_id": str(uuid4())})
    monkeypatch.setattr("apps.mcp_server.main.get_access_token", lambda: token)
    assert "research:read" in _principal(research_only=True).scopes
    with pytest.raises(PermissionError, match="market research"):
        _principal(market_only=False)
    token.scopes = ["mcp:connect", "analysis:read"]
    with pytest.raises(PermissionError, match="research:read"):
        _principal(research_only=True)


def test_real_sdk_wire_preserves_auth_metadata_and_missing_scope_challenge():
    instance, _, reader, private = service()
    def identity(*, research_only=False):
        if research_only:
            raise PermissionError("scope")
        return principal("analysis:read", "market:read")
    server = ResearchMCPServer("test-research")
    register_research_tools(server, instance, identity, "https://trade.example")
    app = server.streamable_http_app(json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["127.0.0.1"], allowed_origins=["http://127.0.0.1"]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        response = client.post("/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert response.status_code == 200
        tool = next(item for item in response.json()["result"]["tools"] if item["name"] == "watchlist_list")
        assert tool["securitySchemes"][0]["scopes"] == ["mcp:connect", "research:read"]
        assert tool["_meta"]["securitySchemes"] == tool["securitySchemes"]
        denied = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2,
            "method": "tools/call", "params": {"name": "watchlist_list", "arguments": {}}}).json()["result"]
        assert denied["isError"] is True and "research:read" in denied["_meta"]["mcp/www_authenticate"][0]
        assert not private.calls
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3,
            "method": "tools/call", "params": {"name": "structure_current", "arguments": {
                "instrument_id": IDENTIFIER, "limit": 1}}}).json()["result"]
        assert result.get("isError") is not True
        assert result["structuredContent"]["items"][0]["id"] == "case"
        assert reader.calls[-1][-1]["limit"] == 1


def test_all_new_sdk_wrappers_use_only_explicit_arguments():
    instance, _, _, private = service()
    identity = principal("mcp:connect", "market:read", "analysis:read", "research:read")
    server = ResearchMCPServer("test-research")
    register_research_tools(server, instance, lambda **kwargs: identity, "https://trade.example")
    cases = {
        "structure_current": {"instrument_id": IDENTIFIER},
        "structure_study": {"instrument_id": IDENTIFIER},
        "structure_event_list": {"instrument_id": IDENTIFIER},
        "structure_event_detail": {"instrument_id": IDENTIFIER, "context_id": "context", "candidate_id": "case"},
        "watchlist_list": {}, "research_backtest_list": {},
        "research_backtest_detail": {"run_id": str(UUID(int=42))},
        "indicator_get_series": {"instrument_id": IDENTIFIER},
        "market_research_bundle": {"instrument_id": IDENTIFIER, "timeframes": ["15m"]},
        "market_chart_snapshot": {"instrument_id": IDENTIFIER, "bar_limit": 50},
    }
    for name, values in cases.items():
        result = anyio.run(server.call_tool, name, values)
        assert not result.is_error, name
        assert result.structured_content["research_only"] is True
        assert name in TOOL_SCOPES
        if name == "market_chart_snapshot":
            assert any(item.type == "image" for item in result.content)
    assert len(private.calls) == 3
