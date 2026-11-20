"""Verify SOL charts/patterns using only the previously published MCP tool name.

No tool-list refresh, new tool name, extra scope, upstream call or order is used.
The existing snapshot's bounded background cache-refresh policy remains intact.
Only sanitized counts, timing and optional public chart PNGs are recorded.
"""
from __future__ import annotations

import base64
import hashlib
import io
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx2 as httpx
from PIL import Image

from smoke_mcp_market_export import result_payload, rpc
from smoke_mcp_research import chart_contract, shape_contract


def run(url: str, bearer: str, *, image_directory: Path | None = None) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("credential-free HTTPS MCP endpoint is required")
    report = {"cases": [], "only_existing_tool_name_used": True, "tool_list_refresh_required": False,
              "openai_called": False, "orders_changed": False}
    with httpx.Client(headers={"Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream"}, timeout=40, follow_redirects=False) as client:
        init = rpc(client, url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "cached-legacy-tool-smoke", "version": "1"}}, 1)
        assert init["serverInfo"]["version"] == "1.3.0"
        report["server_version"] = init["serverInfo"]["version"]
        client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
        for sequence, timeframe in enumerate(("5m", "15m", "1h"), 2):
            started = time.monotonic()
            result = rpc(client, url, "tools/call", {"name": "market_analysis_snapshot", "arguments": {
                "symbol": "SOL_USDT", "market": "futures:usdt", "timeframe": timeframe, "bar_limit": 100}}, sequence)
            data = result_payload(result)
            assert data["version"] == "market-analysis-2" and data["market_data_version"] == "market-analysis-1"
            assert data["research_only"] is True and data["image_status"] == "READY"
            chart = data["chart"]
            assert chart["candles"] == data["candles"][-150:]
            assert chart["captured_at"] == data["as_of"]
            indicators = chart_contract(chart, timeframe, symbol="SOL_USDT")
            assert all(indicators.values())
            shapes = data["structures"]
            assert shapes["timeframe"] == timeframe and len(shapes["items"]) <= 5
            assert shapes["status"] in {"READY", "PARTIAL", "STALE", "NOT_REQUESTED", "NOT_READY"}
            for item in shapes["items"]:
                shape_contract(item["shape_summary"], timeframe)
            images = [item for item in result["content"] if item["type"] == "image"]
            assert len(images) == 1 and images[0]["mimeType"] == "image/png"
            png = base64.b64decode(images[0]["data"], validate=True)
            with Image.open(io.BytesIO(png)) as image:
                assert image.size == (1200, 900)
                image.verify()
            if image_directory is not None:
                (image_directory / f"legacy-sol-{timeframe}.png").write_bytes(png)
            report["cases"].append({"tool": "market_analysis_snapshot", "symbol": "SOL_USDT",
                "timeframe": timeframe, "elapsed_seconds": round(time.monotonic()-started, 3),
                "version": data["version"], "content_types": [item["type"] for item in result["content"]],
                "image_status": data["image_status"], "png_bytes": len(png),
                "image_sha256": hashlib.sha256(png).hexdigest(), "closed_candles": len(chart["candles"]),
                "ready_indicators": indicators, "structure_status": shapes["status"],
                "structure_count": len(shapes["items"]), "last_closed_at": chart["last_closed_at"],
                "captured_at": chart["captured_at"], "quality_flags": chart["quality_flags"]})
    report["ok"] = len(report["cases"]) == 3
    return report
