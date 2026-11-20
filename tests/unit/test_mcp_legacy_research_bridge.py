from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import UUID

import pytest
from psycopg.errors import QueryCanceled

from gatescope.application.mcp_chart import render_research_chart
from gatescope.application.mcp_tools import McpToolDenied, _final_bars
from gatescope.indicators import IndicatorNode, compute_batch
from tests.unit.test_mcp_market_export import IDENTIFIER, Coinglass, Market, principal, service


class Structures:
    def __init__(self):
        self.calls = []
        self.payload = {
            "status": "READY", "items": [{
                "id": "example", "phase": "FORMED", "confidence": .8,
                "shape_summary": {
                    "pattern_name": "Ascending triangle", "timeframe": "15m",
                    "key_levels": {"support": {"kind": "LINE", "a": 100, "b": 105}},
                    "trigger_price": None, "invalidation_price": None, "is_confirmed": False,
                    "field_notes": ["DYNAMIC_CONFIRMATION_LINE_NOT_A_FIXED_PRICE"],
                },
                "historical_reference": {"sample_count": None, "rate": None, "status": "UNKNOWN"},
            }], "total": 1,
        }

    def current(self, instrument, timeframe, **kwargs):
        self.calls.append((instrument, timeframe, kwargs))
        return deepcopy(self.payload)


def make_service(count=500, timeframe="15m"):
    instance, access = service(Market(count=count, timeframe=timeframe), Coinglass())
    structures = Structures()
    instance.structures = structures
    return instance, access, structures


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h"])
def test_legacy_bridge_preserves_fields_and_renders_same_candle_window(timeframe):
    instance, access, structures = make_service(timeframe=timeframe)
    identity = principal("market:read", "analysis:read")
    result = instance.market_analysis_with_research(identity, symbol="BTC_USDT",
        market="futures:usdt", timeframe=timeframe, indicator_ids=["atr"], bar_limit=500)
    assert result["version"] == "market-analysis-2"
    assert result["market_data_version"] == "market-analysis-1"
    assert len(result["candles"]) == 500 and len(result["indicators"]) == 1
    assert result["indicators"][0]["indicator_id"] == "atr"
    assert result["latest_closed"] == result["candles"][-1]
    assert result["coinglass"]["status"] == "READY"
    # Exactly the legacy candle read and provider-cache request, no enrichment fetch.
    assert [call for call in instance.market.calls if call[0] == "candles"] == [
        ("candles", IDENTIFIER, timeframe, None, None, 500)]
    assert len(instance.coinglass.calls) == 1 and instance.coinglass.calls[0][-1] is True
    assert "NO_REFRESH" not in result["policies"]["coinglass"]
    assert "read_policy" not in result
    chart = result["chart"]
    assert chart["status"] == "READY"
    assert chart["candles"] == result["candles"][-150:]
    assert chart["captured_at"] == result["as_of"]
    assert chart["last_closed_at"] == result["latest_closed"]["close_time"]
    assert chart["instrument"]["venue"] == "GATE" and chart["timeframe"] == timeframe
    assert chart["includes_forming_candle"] is False and result["realtime"]["candle"]["is_final"] is False
    assert chart["source_window"] == {
        "source_field": "candles", "source_as_of": result["as_of"], "source_closed_bars": 500,
        "displayed_closed_bars": 150, "display_limit": 150, "display_truncated": True,
        "indicator_input_bars": 500, "indicator_input_start": result["candles"][0]["open_time"],
        "extra_candle_queries": 0,
    }
    bars, _ = _final_bars(result["instrument"], IDENTIFIER, timeframe,
        instance.market.rows, datetime.fromisoformat(result["as_of"]))
    points = compute_batch(instance.registry, [IndicatorNode("ema9", "ema", {"period": 9})], bars)
    assert chart["overlays"]["ema9"][-1]["value"] == points[-1].values["ema"]
    assert all(item["input_bars"] == 500 for item in chart["indicators"])
    assert render_research_chart(chart).startswith(b"\x89PNG")
    assert structures.calls[0][:2] == (UUID(IDENTIFIER), timeframe)
    assert structures.calls[0][-1]["limit"] == 5 and structures.calls[0][-1]["offset"] == 0
    assert {entry["tool_name"] for entry in access.audits} == {"market_analysis_snapshot", "structure_current"}
    assert result["evidence_alignment"]["atomic_market_and_structures"] is False
    assert result["evidence_alignment"]["market_as_of"] == result["as_of"]
    assert result["evidence_alignment"]["structures_as_of"] == result["structures"]["as_of"]


def test_bridge_does_not_reinterpret_dynamic_levels_formation_or_unknown_history():
    instance, _, structures = make_service()
    result = instance.market_analysis_with_research(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER)
    assert result["structures"]["items"] == structures.payload["items"]
    shape = result["structures"]["items"][0]
    assert shape["shape_summary"]["trigger_price"] is None
    assert shape["shape_summary"]["invalidation_price"] is None
    assert shape["phase"] == "FORMED" and shape["shape_summary"]["is_confirmed"] is False
    assert shape["historical_reference"]["sample_count"] is None
    assert shape["historical_reference"]["rate"] is None
    assert not any(key in result for key in ("watchlists", "backtests", "trades", "research"))


def test_bridge_retains_every_original_snapshot_field_except_declared_version(monkeypatch):
    instance, _, _ = make_service()
    identity = principal("market:read", "analysis:read")
    original = instance.market_analysis_snapshot(identity, instrument_id=IDENTIFIER, bar_limit=50)
    before = deepcopy(original)
    monkeypatch.setattr(instance, "market_analysis_snapshot", lambda *args, **kwargs: original)
    result = instance.market_analysis_with_research(identity, instrument_id=IDENTIFIER, bar_limit=50)
    assert original == before
    assert all(result[key] == value for key, value in original.items() if key != "version")
    assert result["chart"]["candles"] == original["candles"]
    assert result["chart"]["source_window"]["display_truncated"] is False


def test_bridge_gap_warmup_is_contiguous_only_and_has_no_invented_points():
    instance, _, _ = make_service(count=200)
    del instance.market.rows[-11]
    result = instance.market_analysis_with_research(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER)
    chart = result["chart"]
    assert chart["source_window"]["indicator_input_bars"] == 10
    assert chart["source_window"]["indicator_input_start"] == result["candles"][-10]["open_time"]
    assert chart["quality"]["missing_bars"] == 1
    assert len(chart["overlays"]["ema21"]) == 10
    assert all(item["value"] is None and not item["ready"] for item in chart["overlays"]["ema21"])
    assert chart["candles"] == result["candles"][-150:]


def test_bridge_no_candles_is_explicit_no_data_not_fake_chart():
    instance, _, _ = make_service(count=0)
    result = instance.market_analysis_with_research(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER)
    assert result["chart"]["status"] == "NO_DATA"
    assert result["chart"]["candles"] == [] and result["chart"]["last_closed_at"] is None
    assert result["chart"]["source_window"]["indicator_input_start"] is None
    assert result["structures"]["status"] == "READY"


@pytest.mark.parametrize("component", ["chart", "structures"])
@pytest.mark.parametrize("error", [ValueError, RuntimeError, QueryCanceled])
def test_optional_evidence_failure_preserves_market_with_static_unknown_reason(monkeypatch, component, error):
    instance, _, _ = make_service()
    def fail(*args, **kwargs):
        raise error("sensitive implementation detail must not escape")
    monkeypatch.setattr(instance, "_chart_evidence" if component == "chart" else "structure_current", fail)
    result = instance.market_analysis_with_research(principal("market:read", "analysis:read"),
        instrument_id=IDENTIFIER)
    assert result["version"] == "market-analysis-2" and result["latest_closed"]
    assert result[component]["status"] == "UNAVAILABLE"
    assert "sensitive" not in str(result)
    assert result["chart" if component == "structures" else "structures"]["status"] == "READY"
    if component == "structures":
        assert result[component]["total"] is None and result[component]["historical_win_rate"] is None


@pytest.mark.parametrize("component", ["chart", "structures"])
def test_optional_evidence_auth_failure_is_never_downgraded(monkeypatch, component):
    instance, _, _ = make_service()
    def fail(*args, **kwargs):
        raise McpToolDenied("scope denied")
    monkeypatch.setattr(instance, "_chart_evidence" if component == "chart" else "structure_current", fail)
    with pytest.raises(McpToolDenied):
        instance.market_analysis_with_research(principal("market:read", "analysis:read"),
            instrument_id=IDENTIFIER)


@pytest.mark.parametrize("scopes", [(), ("market:read",), ("analysis:read",), ("research:read",)])
def test_bridge_legacy_scope_rejection_precedes_any_market_or_structure_read(scopes):
    instance, _, structures = make_service()
    with pytest.raises(McpToolDenied):
        instance.market_analysis_with_research(principal(*scopes), instrument_id=IDENTIFIER)
    assert not instance.market.calls and not instance.coinglass.calls and not structures.calls


@pytest.mark.parametrize("kwargs", [
    {"timeframe": "2h"}, {"bar_limit": 49}, {"bar_limit": 2001},
    {"indicator_ids": []}, {"indicator_ids": ["invalid"]},
    {"symbol": "BTC_USDT", "market": "futures:usdt"},
])
def test_bridge_keeps_legacy_argument_validation(kwargs):
    instance, _, structures = make_service()
    with pytest.raises(ValueError):
        instance.market_analysis_with_research(principal("market:read", "analysis:read"),
            instrument_id=IDENTIFIER, **kwargs)
    assert not structures.calls
