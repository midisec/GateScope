"""Bounded production MCP research reads; tokens and response bodies stay in memory."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx2 as httpx
from PIL import Image

from smoke_mcp_market_export import result_payload, rpc


def shape_contract(shape: dict, timeframe: str) -> None:
    assert {"name", "timeframe", "key_levels", "trigger_price", "invalidation_price", "is_confirmed"} <= shape.keys()
    assert shape["timeframe"] == timeframe and isinstance(shape["key_levels"], dict)
    assert shape["name"] is None or isinstance(shape["name"], str)
    phase = shape.get("phase")
    if phase in {"FORMING", "FORMED", "CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED"}:
        assert shape["is_confirmed"] is (phase == "CONFIRMED")
    else:
        assert shape["is_confirmed"] is None
    for key in ("trigger_price", "invalidation_price"):
        if shape[key] is not None:
            assert not isinstance(shape[key], bool) and math.isfinite(float(shape[key])) and float(shape[key]) > 0


def chart_contract(data: dict, timeframe: str, *, symbol: str = "BTC_USDT") -> dict:
    candles = data["candles"]
    assert data["timeframe"] == timeframe
    assert 30 <= len(candles) <= 100 and all(item["is_final"] is True for item in candles)
    assert data["last_closed_at"] == candles[-1]["close_time"]
    assert data["includes_forming_candle"] is False and data["instrument"]["venue"] == "GATE"
    assert data["instrument"]["symbol"] == symbol and data["instrument"]["market"] == "futures_usdt"
    captured = datetime.fromisoformat(data["captured_at"])
    assert captured.utcoffset() is not None
    by_time = {item["open_time"]: item for item in candles}
    assert len(by_time) == len(candles) and list(by_time) == sorted(by_time)
    steps = {"5m": 300, "15m": 900, "1h": 3600}
    step = steps[timeframe]
    for item in candles:
        opened, closed, available = (datetime.fromisoformat(item[key]) for key in ("open_time", "close_time", "available_at"))
        assert (closed-opened).total_seconds() == step and closed <= available <= captured
        assert item["timeframe"] == timeframe and item["instrument_id"] == data["instrument"]["id"]
        assert item["venue"] == "GATE" and item["market"] == "FUTURES_USDT"
        opened_price, high, low, close, volume = (float(item[key]) for key in ("open", "high", "low", "close", "volume"))
        assert all(math.isfinite(value) for value in (opened_price, high, low, close, volume))
        assert 0 < low <= min(opened_price, close) <= max(opened_price, close) <= high and volume >= 0
    expected = {"ema9": ("ema", 9), "ema21": ("ema", 21), "rsi14": ("rsi", 14)}
    assert set(data["overlays"]) == set(expected)
    metadata = {item["instance_id"]: item for item in data["indicators"]}
    assert set(metadata) == set(expected)
    ready = {}
    for name, (indicator, period) in expected.items():
        assert metadata[name]["indicator_id"] == indicator and metadata[name]["parameters"] == {"period": period}
        assert metadata[name]["version"] and metadata[name]["input_bars"] >= 0
        points = data["overlays"][name]
        times = [point["open_time"] for point in points]
        assert times == sorted(set(times)) and set(times) <= by_time.keys()
        previous = None
        ready[name] = 0
        for point in points:
            bar = by_time[point["open_time"]]
            assert point["bar_revision"] == bar["revision"] and point["available_at"] == bar["available_at"]
            value = point["value"]
            assert isinstance(point["ready"], bool) and (value is not None) == point["ready"]
            if value is not None:
                value = float(value)
                assert math.isfinite(value) and (0 <= value <= 100 if indicator == "rsi" else value > 0)
                ready[name] += 1
                if indicator == "ema" and previous is not None:
                    previous_time, previous_value = previous
                    elapsed = (datetime.fromisoformat(point["open_time"])-datetime.fromisoformat(previous_time)).total_seconds()
                    if elapsed == step:
                        expected_value = previous_value + 2 / (period + 1) * (float(bar["close"])-previous_value)
                        assert math.isclose(value, expected_value, rel_tol=1e-10, abs_tol=1e-10)
                previous = (point["open_time"], value)
            else:
                previous = None
    return ready


def run(url: str, bearer: str, *, private: bool = True, image_directory: Path | None = None) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.path != "/mcp" or not parsed.hostname
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("credential-free HTTPS MCP endpoint is required")
    report = {"cases": [], "warnings": [], "openai_called": False, "orders_changed": False}

    def warning(tool, reason, timeframe=None):
        report["warnings"].append({"tool": tool, "timeframe": timeframe, "reason": reason})

    def status(tool, data, *, ready=("READY",), timeframe=None):
        value = data.get("status")
        assert isinstance(value, str) and value not in {"TIMEOUT", "UNAVAILABLE", "FAILED", "ERROR"}
        if value not in ready:
            warning(tool, value, timeframe)

    with httpx.Client(headers={"Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/event-stream"}, timeout=40, follow_redirects=False) as client:
        init = rpc(client, url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "gatescope-research-smoke", "version": "1"}}, 1)
        client.headers["MCP-Protocol-Version"] = init["protocolVersion"]
        client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()
        listing = rpc(client, url, "tools/list", {}, 2)["tools"]
        names = {item["name"] for item in listing}
        expected = {"market_chart_snapshot", "market_research_bundle", "indicator_get_series", "structure_current",
            "structure_study", "structure_event_list", "structure_event_detail", "watchlist_list",
            "research_backtest_list", "research_backtest_detail"}
        assert expected <= names
        from apps.mcp_server.paper import PAPER_WRITE_TOOLS
        assert all(item["annotations"]["readOnlyHint"] == (item["name"] not in PAPER_WRITE_TOOLS) for item in listing)
        for item in listing:
            if item["name"] in expected:
                assert item["securitySchemes"] == item["_meta"]["securitySchemes"]
        report["tool_count"] = len(listing)
        sequence = 2

        def call(name, arguments):
            nonlocal sequence
            sequence += 1
            started = time.monotonic()
            result = rpc(client, url, "tools/call", {"name": name, "arguments": arguments}, sequence)
            data = result_payload(result)
            assert data["research_only"] is True
            report["cases"].append({"tool": name, "timeframe": arguments.get("timeframe"),
                "elapsed_seconds": round(time.monotonic()-started, 3),
                "status": data.get("status", data.get("availability")),
                "items": len(data.get("items", [])), "quality_flags": data.get("quality_flags", [])})
            if data.get("availability") in {"REPORT_NOT_READY", "UNAVAILABLE"}:
                warning(name, data["availability"], arguments.get("timeframe"))
            return data, result

        selection = {"symbol": "BTC_USDT", "market": "futures:usdt"}
        for timeframe in ("5m", "15m", "1h"):
            data, result = call("market_chart_snapshot", {**selection, "timeframe": timeframe, "bar_limit": 100})
            candles = data["candles"]
            status("market_chart_snapshot", data, timeframe=timeframe)
            ready_counts = chart_contract(data, timeframe)
            if not all(ready_counts.values()):
                warning("market_chart_snapshot", "INDICATOR_WARMUP_OR_GAP", timeframe)
            images = [item for item in result["content"] if item["type"] == "image"]
            assert len(images) == 1 and images[0]["mimeType"] == "image/png"
            png = base64.b64decode(images[0]["data"], validate=True)
            image = Image.open(io.BytesIO(png))
            image.verify()
            assert image.size == (1200, 900)
            report["cases"][-1].update(image_size=list(image.size), png_bytes=len(png),
                image_sha256=hashlib.sha256(png).hexdigest(), closed_candles=len(candles),
                last_closed_at=data["last_closed_at"], captured_at=data["captured_at"], ready_indicators=ready_counts)
            if image_directory is not None:
                (image_directory / f"mcp-chart-{timeframe}.png").write_bytes(png)

        current, _ = call("structure_current", {**selection, "timeframe": "15m", "limit": 3})
        status("structure_current", current, timeframe="15m")
        for item in current["items"]:
            shape_contract(item["shape_summary"], "15m")
        if not current["items"]:
            warning("structure_current", "NO_CURRENT_CANDIDATE_TO_CHECK", "15m")
        study, _ = call("structure_study", {**selection, "timeframe": "15m"})
        status("structure_study", study, ready=("COMPLETE", "SUCCEEDED", "READY"), timeframe="15m")
        if study.get("report_status") != "READY" or not isinstance(study.get("report"), dict):
            warning("structure_study", study.get("report_status") or "NO_STORED_REPORT", "15m")
        events, _ = call("structure_event_list", {**selection, "timeframe": "15m", "limit": 2})
        status("structure_event_list", events, timeframe="15m")
        if events.get("items"):
            case = events["items"][0]
            candidate = case.get("candidate_id") or case.get("id")
            assert isinstance(candidate, str) and candidate
            detail, _ = call("structure_event_detail", {**selection, "timeframe": "15m",
                "context_id": case["context_id"], "candidate_id": candidate,
                "transition_limit": 2, "bar_limit": 100})
            status("structure_event_detail", detail, timeframe="15m")
            assert detail["context_id"] == case["context_id"] and detail["candidate_id"] == candidate
            assert detail["source_mode"] == case["source_mode"] == "RECORDED"
            assert detail["model_version"] == case["model_version"]
            shape_contract(detail["shape_summary"], "15m")
            shape_contract(detail["first_shape_summary"], "15m")
            assert {"initial_prediction", "first_record", "latest_record", "transitions"} <= detail.keys()
            assert detail["chart"]["view"] == "ORIGINAL" and len(detail["chart"]["bars"]) <= 100
            if detail["chart"]["status"] != "READY":
                warning("structure_event_detail", "ORIGINAL_CHART_" + detail["chart"]["status"], "15m")
        else:
            warning("structure_event_detail", "NO_RECORDED_CANDIDATE_TO_CHECK", "15m")
        series, _ = call("indicator_get_series", {**selection, "timeframe": "15m", "indicator_id": "ema",
            "parameters": {"period": 21}, "bar_limit": 200, "limit": 200})
        assert series["parameters"]["period"] == 21 and series["indicator_id"] == "ema"
        assert series["timeframe"] == "15m" and len(series["items"]) <= 200
        series_times = [item["open_time"] for item in series["items"]]
        assert series_times == sorted(set(series_times))
        ready_series = [item for item in series["items"] if item["ready"]]
        for point in ready_series:
            assert math.isfinite(float(point["values"]["ema"])) and float(point["values"]["ema"]) > 0
        if not ready_series:
            warning("indicator_get_series", "NO_READY_INDICATOR_POINTS", "15m")
        bundle, _ = call("market_research_bundle", {**selection, "timeframes": ["5m", "15m", "1h"], "bar_limit": 50})
        assert [item["timeframe"] for item in bundle["items"]] == ["5m", "15m", "1h"]
        for item in bundle["items"]:
            if item["status"] != "READ":
                assert item["status"] == "NOT_READ" and item.get("reason")
                warning("market_research_bundle", item["reason"], item["timeframe"])
                continue
            assert item["market"]["timeframe"] == item["timeframe"]
            status("market_research_bundle.structures", item["structures"], timeframe=item["timeframe"])
        if private:
            call("watchlist_list", {"limit": 3})
            runs, _ = call("research_backtest_list", {"limit": 2})
            if runs.get("items"):
                run_id = runs["items"][0]["run_id"]
                for section in ("summary", "validation", "trades", "inputs"):
                    call("research_backtest_detail", {"run_id": run_id, "section": section, "limit": 3})
            else:
                report["private_backtest_detail"] = "NO_EXISTING_RUN_TO_CHECK"
                warning("research_backtest_detail", "NO_EXISTING_RUN_TO_CHECK")
    report["ok"] = True
    report["data_ready"] = not report["warnings"]
    report["outcome"] = "PASS" if report["data_ready"] else "CONTRACT_PASS_EVIDENCE_PARTIAL"
    return report


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        report = run(os.environ["GATESCOPE_MCP_URL"], os.environ["GATESCOPE_MCP_TOKEN"])
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__, "openai_called": False}
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["ok"] else 1)
