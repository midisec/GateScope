"""Bounded read-only MCP protocol smoke; uses an existing token in memory only."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx2 as httpx


def rpc(client: httpx.Client, url: str, method: str, params: dict[str, Any], identifier: int) -> dict[str, Any]:
    response = client.post(url, json={"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError("MCP protocol error")
    return payload["result"]


def result_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError") or result.get("is_error"):
        raise RuntimeError("MCP tool error")
    structured = result.get("structuredContent", result.get("structured_content"))
    if isinstance(structured, dict):
        return structured
    for block in result.get("content", []):
        if block.get("type") == "text":
            payload = json.loads(block["text"])
            if isinstance(payload, dict):
                return payload
    raise ValueError("missing structured tool result")


def run(url: str, bearer: str, symbol: str, market: str, timeframe: str, bar_limit: int) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.path != "/mcp":
        raise ValueError("MCP URL must end in /mcp without credentials/query/fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("public MCP URL requires HTTPS")
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json, text/event-stream"}
    started = time.monotonic()
    with httpx.Client(timeout=40, follow_redirects=False) as anonymous:
        denied = anonymous.post(url, headers={"Accept": headers["Accept"]},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        if denied.status_code != 401:
            raise AssertionError("anonymous MCP request was not denied")
    with httpx.Client(headers=headers, timeout=40, follow_redirects=False) as client:
        initialized = rpc(client, url, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "gatescope-read-smoke", "version": "1.0"},
        }, 2)
        client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        notification = client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        notification.raise_for_status()
        tools = rpc(client, url, "tools/list", {}, 3)["tools"]
        names = {tool["name"] for tool in tools}
        expected = {"market_search_instruments", "market_analysis_snapshot", "coinglass_get_snapshot"}
        if not expected <= names:
            raise AssertionError("new read tools are missing")
        for tool in tools:
            from apps.mcp_server.paper import PAPER_WRITE_TOOLS
            annotations = tool.get("annotations") or {}
            if annotations.get("readOnlyHint", annotations.get("read_only_hint")) != (tool["name"] not in PAPER_WRITE_TOOLS):
                raise AssertionError("manifest tool mutability does not match the explicit PAPER write allowlist")
        search = result_payload(rpc(client, url, "tools/call", {
            "name": "market_search_instruments", "arguments": {"query": symbol, "market": market, "limit": 100},
        }, 4))
        matches = [item for item in search["items"] if item["symbol"].casefold() == symbol.casefold()
                   and item["market"] == market.replace(":", "_")]
        if len(matches) != 1:
            raise AssertionError("exact symbol/market is absent or ambiguous")
        snapshot = result_payload(rpc(client, url, "tools/call", {
            "name": "market_analysis_snapshot", "arguments": {
                "instrument_id": matches[0]["id"], "timeframe": timeframe, "bar_limit": bar_limit,
            },
        }, 5))
        if snapshot["instrument"]["id"] != matches[0]["id"] or snapshot["timeframe"] != timeframe:
            raise AssertionError("instrument/timeframe mismatch")
        if snapshot["research_only"] is not True or snapshot["realtime"]["used_for_indicators"] is not False:
            raise AssertionError("research/finality contract violated")
        candles = snapshot["candles"]
        if len(candles) > bar_limit or any(bar["is_final"] is not True for bar in candles):
            raise AssertionError("candle bound/finality violated")
        if candles and (snapshot["latest_closed"] != candles[-1] or
                        [bar["open_time"] for bar in candles] != sorted(bar["open_time"] for bar in candles)):
            raise AssertionError("latest/chronological candle contract violated")
        context = result_payload(rpc(client, url, "tools/call", {
            "name": "coinglass_get_snapshot", "arguments": {
                "symbol": symbol, "market": market, "timeframe": timeframe,
            },
        }, 6))
    return {
        "ok": True, "elapsed_seconds": round(time.monotonic() - started, 3), "anonymous_status": 401,
        "tool_count": len(tools), "closed_bars": len(candles), "indicator_count": len(snapshot["indicators"]),
        "ready_indicators": sum(item["ready"] for item in snapshot["indicators"]),
        "closed_age_seconds": snapshot["quality"]["latest_close_age_seconds"],
        "quality_flags": snapshot["quality_flags"], "realtime_status": snapshot["realtime"]["status"],
        "coinglass_status": context["coinglass"].get("status"),
        "coinglass_series": len(context["coinglass"].get("series", [])),
        "coinglass_fetched_at": context["coinglass"].get("fetched_at"),
        "coinglass_refresh_status": context["coinglass"].get("refresh_status"),
        "coinglass_refresh_state": context["coinglass"].get("refresh_state"),
        "coinglass_queue_age_seconds": context["coinglass"].get("queue_age_seconds"),
        "coinglass_cache_wait": context["coinglass"].get("cache_wait"),
        "coinglass_availability": context["coinglass"].get("availability"),
        "coinglass_refresh_requested": context["coinglass"].get("refresh_requested"),
        "coinglass_retry_after_seconds": context["coinglass"].get("retry_after_seconds"),
        "coinglass_metrics": [
            {"id": item.get("id"), "status": item.get("status"),
             "unit_status": item.get("unit_status"), "points": len(item.get("points", []))}
            for item in context["coinglass"].get("series", [])
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC_USDT")
    parser.add_argument("--market", choices=("spot", "futures:usdt", "futures:btc"), default="futures:usdt")
    parser.add_argument("--timeframe", choices=("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"), default="15m")
    parser.add_argument("--bar-limit", type=int, default=100)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if not 50 <= args.bar_limit <= 2_000:
            raise ValueError("bar-limit is outside bounds")
        url = os.environ["GATESCOPE_MCP_URL"]
        bearer = os.environ["GATESCOPE_MCP_TOKEN"]
        if not bearer or len(bearer) > 4_096:
            raise ValueError("MCP token is missing or invalid")
        print(json.dumps(run(url, bearer, args.symbol, args.market, args.timeframe, args.bar_limit), ensure_ascii=False))
        return 0
    except Exception as exc:
        # Exception text may contain URL, headers or transport diagnostics.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
