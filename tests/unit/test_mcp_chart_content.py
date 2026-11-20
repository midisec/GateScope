from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from PIL import Image

import apps.mcp_server.main as main
from apps.mcp_server.research import chart_tool_result
from tests.unit.test_mcp_market_export import IDENTIFIER, principal
from tests.unit.test_mcp_research_tools import service


@pytest.mark.parametrize("nested", [False, True])
def test_chart_content_is_real_png_and_identical_text_and_structure(nested):
    instance, *_ = service()
    chart = instance.market_chart_snapshot(principal("market:read", "analysis:read"),
                                          instrument_id=IDENTIFIER, bar_limit=50)
    payload = {"version": "market-analysis-2", "chart": chart, "structures": {"items": []}} if nested else chart
    result = chart_tool_result(payload, nested_chart=nested)
    assert not result.is_error and result.structured_content["image_status"] == "READY"
    assert "image_status" not in payload
    text, image = result.content
    assert json.loads(text.text) == result.structured_content
    assert image.type == "image" and image.mime_type == "image/png"
    png = base64.b64decode(image.data, validate=True)
    with Image.open(io.BytesIO(png)) as rendered:
        assert rendered.size == (1200, 900)


@pytest.mark.parametrize("failure", [ValueError, TypeError, OSError])
def test_image_failure_keeps_market_and_shapes_without_exception_details(monkeypatch, failure):
    def fail(_):
        raise failure("internal sensitive diagnostic must not escape")
    monkeypatch.setattr("gatescope.application.mcp_chart.render_research_chart", fail)
    payload = {"version": "market-analysis-2", "chart": {"candles": [{"close": "99.59"}]},
               "candles": [{"close": "99.59"}], "structures": {"items": [{"phase": "FORMING"}]}}
    result = chart_tool_result(payload, nested_chart=True)
    assert not result.is_error and len(result.content) == 1
    data = result.structured_content
    assert data["image_status"] == "UNAVAILABLE" and data["candles"] == payload["candles"]
    assert data["structures"] == payload["structures"]
    assert "sensitive" not in result.content[0].text


def test_empty_chart_reports_no_data_instead_of_claiming_image_success():
    result = chart_tool_result({"candles": [], "status": "NO_DATA"})
    assert result.structured_content["image_status"] == "NO_DATA"
    assert [item.type for item in result.content] == ["text"]


def test_existing_tool_name_and_old_arguments_return_image_and_shapes_over_http(monkeypatch):
    instance, _, structures, private = service()
    identity = principal("mcp:connect", "market:read", "analysis:read")
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused:unused@127.0.0.1/unused")
    monkeypatch.setenv("GATESCOPE_MCP_PUBLIC_URL", "http://127.0.0.1:8780/mcp")
    monkeypatch.setattr(main, "PostgresPool", lambda *_: SimpleNamespace(open=lambda: None, close=lambda: None))
    monkeypatch.setattr(main, "McpAnalysisService", lambda *args, **kwargs: instance)
    monkeypatch.setattr(main, "_principal", lambda **kwargs: identity)
    class Verifier:
        async def verify_token(self, token):
            return AccessToken(token=token, client_id="test", scopes=list(identity.scopes))
    monkeypatch.setattr(main, "GateScopeTokenVerifier", lambda *args: Verifier())
    server = main.create_mcp_server()
    app = server.streamable_http_app(stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    headers = {"Authorization": "Bearer test", "Accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        initialized = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "cached-old-client", "version": "1"}}}).json()["result"]
        assert initialized["serverInfo"]["version"] == "1.4.0"
        headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2,
            "method": "tools/call", "params": {"name": "market_analysis_snapshot", "arguments": {
                "symbol": "BTC_USDT", "market": "futures:usdt", "timeframe": "15m", "bar_limit": 100}}})
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError")
        data = result["structuredContent"]
        assert data["version"] == "market-analysis-2" and data["market_data_version"] == "market-analysis-1"
        assert data["image_status"] == "READY"
        assert data["chart"]["candles"] == data["candles"][-150:]
        assert set(data["chart"]["overlays"]) == {"ema9", "ema21", "rsi14"}
        assert data["structures"]["items"] and structures.calls
        assert [item["type"] for item in result["content"]] == ["text", "image"]
        assert json.loads(result["content"][0]["text"]) == data
        assert not private.calls
