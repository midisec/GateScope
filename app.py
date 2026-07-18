#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate market explorer, local candle cache, chart-pattern scanner and alerts."""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Literal, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from database import Database
from gate_client import GateClient, GateError, INTERVAL_SECONDS, MAX_HISTORY_POINTS
from pattern_engine import PATTERN_CATALOG, CATALOG_BY_ID, scan_patterns
from scanner import BackgroundScanner
from opportunity_scanner import OpportunityScanner


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
DB_PATH = DATA_DIR / "gate_patterns.db"
INDEX_PATH = STATIC_DIR / "index.html"
VENDOR_PATH = STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js"
VENDOR_URL = (
    "https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.0/"
    "dist/lightweight-charts.standalone.production.js"
)

INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"]
RANGES: dict[str, Optional[int]] = {
    "1d": 86400,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "180d": 180 * 86400,
    "1y": 365 * 86400,
    "all": None,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "proxy": "http://127.0.0.1:7890",
    "host": "127.0.0.1",
    "port": 8777,
    "open_browser": True,
    "update_seconds": 15,
    "catalog_refresh_seconds": 1800,
    "default_interval": "15m",
    "default_range": "30d",
    "default_watch_refresh_seconds": 15,
    "max_pattern_bars": 5000,
}


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception as exc:
            print("配置文件读取失败，将使用默认配置：", exc)
    return config


CONFIG = load_config()
# 历史表现统计至少使用 5000 根 K 线，以获得更有参考价值的样本量。
CONFIG["max_pattern_bars"] = max(5000, int(CONFIG.get("max_pattern_bars", 5000)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "vendor").mkdir(parents=True, exist_ok=True)
DB = Database(DB_PATH)
GATE = GateClient(str(CONFIG.get("proxy") or ""))
SCANNER = BackgroundScanner(
    DB,
    GATE,
    update_seconds=int(CONFIG["update_seconds"]),
    catalog_refresh_seconds=int(CONFIG["catalog_refresh_seconds"]),
)
OPPORTUNITY = OpportunityScanner(DB, GATE, INTERVALS)


def ensure_chart_library() -> None:
    if VENDOR_PATH.exists() and VENDOR_PATH.stat().st_size > 100_000:
        return
    try:
        response = GATE.session.get(VENDOR_URL, timeout=(10, 45))
        response.raise_for_status()
        if len(response.content) < 100_000:
            raise RuntimeError("下载的 Lightweight Charts 文件尺寸异常")
        tmp = VENDOR_PATH.with_suffix(".tmp")
        tmp.write_bytes(response.content)
        tmp.replace(VENDOR_PATH)
        print("已缓存 Lightweight Charts：", VENDOR_PATH)
    except Exception as exc:
        print("提示：图表库缓存失败，前端将尝试 CDN：", exc)


class WatchlistCreate(BaseModel):
    instrument_id: str
    default_interval: str = "15m"
    refresh_seconds: int = Field(default=15, ge=5, le=3600)
    selected_patterns: list[str] = Field(default_factory=list)


class WatchlistUpdate(BaseModel):
    default_interval: Optional[str] = None
    refresh_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    selected_patterns: Optional[list[str]] = None
    enabled: Optional[bool] = None


class PatternScanRequest(BaseModel):
    instrument_id: str
    interval: str = "15m"
    range: str = "30d"
    patterns: list[str] = Field(default_factory=list)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    confirmed_only: bool = False
    max_bars: int = Field(default=5000, ge=50, le=10000)


class AlertRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    instrument_id: str
    interval: str = "15m"
    patterns: list[str] = Field(min_length=1)
    match_mode: Literal["any", "all"] = "any"
    min_confidence: float = Field(default=0.65, ge=0, le=1)
    confirmed_only: bool = False
    lookback_bars: int = Field(default=500, ge=50, le=5000)
    coincidence_bars: int = Field(default=5, ge=1, le=100)
    cooldown_seconds: int = Field(default=1800, ge=60, le=604800)
    browser_notify: bool = True
    sound: bool = True
    enabled: bool = True


class AlertRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    interval: Optional[str] = None
    patterns: Optional[list[str]] = None
    match_mode: Optional[Literal["any", "all"]] = None
    min_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    confirmed_only: Optional[bool] = None
    lookback_bars: Optional[int] = Field(default=None, ge=50, le=5000)
    coincidence_bars: Optional[int] = Field(default=None, ge=1, le=100)
    cooldown_seconds: Optional[int] = Field(default=None, ge=60, le=604800)
    browser_notify: Optional[bool] = None
    sound: Optional[bool] = None
    enabled: Optional[bool] = None


class ReadEventsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class OpportunitySettingsUpdate(BaseModel):
    watchlist_ids: list[int] = Field(default_factory=list)
    intervals: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    min_confidence: float = Field(default=0.60, ge=0, le=1)
    min_win_rate: float = Field(default=0.55, ge=0, le=1)
    min_samples: int = Field(default=12, ge=1, le=500)
    confirmed_only: bool = False
    active_bars: int = Field(default=5, ge=1, le=100)
    max_bars: int = Field(default=5000, ge=300, le=9380)
    horizons: list[int] = Field(default_factory=lambda: [5, 10, 20, 50])
    auto_enabled: bool = False
    auto_seconds: int = Field(default=900, ge=300, le=86400)


app = FastAPI(title="GateScope", version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=ensure_chart_library, daemon=True).start()
    SCANNER.start()
    OPPORTUNITY.start()
    if DB.instrument_count() == 0:
        threading.Thread(target=_initial_catalog_refresh, daemon=True).start()


@app.on_event("shutdown")
def shutdown() -> None:
    OPPORTUNITY.stop()
    SCANNER.stop()


def _initial_catalog_refresh() -> None:
    try:
        count = SCANNER.refresh_catalog()
        print(f"Gate 市场目录已初始化：{count} 个交易标的")
    except Exception as exc:
        print("Gate 市场目录初始化失败：", exc)


def validate_interval(interval: str) -> str:
    if interval not in INTERVALS:
        raise HTTPException(400, f"不支持的周期：{interval}")
    return interval


def validate_range(range_name: str) -> str:
    if range_name not in RANGES:
        raise HTTPException(400, f"不支持的范围：{range_name}")
    return range_name


def validate_patterns(patterns: list[str]) -> list[str]:
    invalid = sorted(set(patterns) - set(CATALOG_BY_ID))
    if invalid:
        raise HTTPException(400, f"未知图表形态：{', '.join(invalid)}")
    return sorted(set(patterns))


def requested_window(range_name: str, interval: str) -> tuple[int, int]:
    now = int(time.time())
    duration = RANGES[range_name]
    if duration is None:
        start = now - (MAX_HISTORY_POINTS - 10) * INTERVAL_SECONDS[interval]
    else:
        start = now - duration
    return start, now


def ensure_candles(
    instrument_id: str,
    interval: str,
    start_ts: int,
    end_ts: int,
) -> dict[str, Any]:
    instrument = DB.get_instrument(instrument_id)
    if not instrument:
        raise HTTPException(404, "交易标的不存在，请先刷新 Gate 市场目录")
    local_min, local_max = DB.candle_bounds(instrument_id, interval)
    truncated = False
    fetched = 0
    try:
        if local_min is None or local_max is None:
            rows, was_truncated = GATE.fetch_candles(instrument, interval, start_ts, end_ts)
            fetched += DB.upsert_candles(instrument_id, interval, rows)
            truncated = truncated or was_truncated
        else:
            step = INTERVAL_SECONDS[interval]
            if start_ts < local_min - step:
                rows, was_truncated = GATE.fetch_candles(instrument, interval, start_ts, local_min - step)
                fetched += DB.upsert_candles(instrument_id, interval, rows)
                truncated = truncated or was_truncated
            if end_ts > local_max:
                rows, was_truncated = GATE.fetch_candles(instrument, interval, max(start_ts, local_max - 2 * step), end_ts)
                fetched += DB.upsert_candles(instrument_id, interval, rows)
                truncated = truncated or was_truncated
    except GateError as exc:
        cached = DB.get_candles(instrument_id, interval, start_ts, end_ts, 20000)
        if not cached:
            raise HTTPException(502, str(exc)) from exc
        return {
            "instrument": instrument,
            "candles": cached,
            "truncated": truncated,
            "fetched": fetched,
            "warning": f"实时更新失败，已返回本地缓存：{exc}",
        }
    candles = DB.get_candles(instrument_id, interval, start_ts, end_ts, 20000)
    return {
        "instrument": instrument,
        "candles": candles,
        "truncated": truncated,
        "fetched": fetched,
        "warning": None,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    return {
        "pattern_catalog": PATTERN_CATALOG,
        "intervals": INTERVALS,
        "ranges": list(RANGES),
        "watchlist": DB.list_watchlist(),
        "alert_rules": DB.list_alert_rules(),
        "unread_alerts": DB.unread_alert_count(),
        "instrument_count": DB.instrument_count(),
        "scanner": SCANNER.status,
        "opportunity": {
            "settings": OPPORTUNITY.get_settings(),
            "status": OPPORTUNITY.status,
            "latest_run": DB.latest_opportunity_run(),
        },
        "config": {
            "default_interval": CONFIG["default_interval"],
            "default_range": CONFIG["default_range"],
            "default_watch_refresh_seconds": CONFIG["default_watch_refresh_seconds"],
            "proxy_enabled": bool(CONFIG.get("proxy")),
        },
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "scanner": SCANNER.status,
        "instrument_count": DB.instrument_count(),
        "watchlist_count": len(DB.list_watchlist()),
        "unread_alerts": DB.unread_alert_count(),
        "opportunity": OPPORTUNITY.status,
        "latest_opportunity_run": DB.latest_opportunity_run(),
        "database": str(DB_PATH),
    }


@app.get("/api/instruments")
def instruments(
    q: str = "",
    market: str = Query(default="all", pattern=r"^(all|spot|stock|futures:usdt|futures:btc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return {
        "items": DB.search_instruments(q, market, limit, offset),
        "catalog_count": DB.instrument_count(),
    }


@app.post("/api/instruments/refresh")
def refresh_instruments() -> dict[str, Any]:
    try:
        count = SCANNER.refresh_catalog()
    except GateError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "updated": count, "catalog_count": DB.instrument_count()}


@app.get("/api/watchlist")
def list_watchlist() -> list[dict[str, Any]]:
    return DB.list_watchlist()


@app.post("/api/watchlist")
def create_watchlist(payload: WatchlistCreate) -> dict[str, Any]:
    validate_interval(payload.default_interval)
    validate_patterns(payload.selected_patterns)
    if not DB.get_instrument(payload.instrument_id):
        raise HTTPException(404, "交易标的不存在")
    item = DB.add_watchlist(
        payload.instrument_id,
        payload.default_interval,
        payload.refresh_seconds,
        payload.selected_patterns,
    )
    SCANNER.wake()
    return item


@app.patch("/api/watchlist/{watchlist_id}")
def update_watchlist(watchlist_id: int, payload: WatchlistUpdate) -> dict[str, Any]:
    data = payload.model_dump(exclude_none=True)
    if "default_interval" in data:
        validate_interval(data["default_interval"])
    if "selected_patterns" in data:
        validate_patterns(data["selected_patterns"])
        data["selected_patterns_json"] = data.pop("selected_patterns")
    item = DB.update_watchlist(watchlist_id, **data)
    if not item:
        raise HTTPException(404, "自选不存在")
    SCANNER.wake()
    return item


@app.delete("/api/watchlist/{watchlist_id}")
def delete_watchlist(watchlist_id: int) -> dict[str, Any]:
    DB.delete_watchlist(watchlist_id)
    return {"ok": True}


@app.get("/api/candles")
def candles(
    instrument_id: str,
    interval: str = "15m",
    range: str = "30d",
) -> dict[str, Any]:
    validate_interval(interval)
    validate_range(range)
    start, end = requested_window(range, interval)
    payload = ensure_candles(instrument_id, interval, start, end)
    payload.update(
        {
            "interval": interval,
            "range": range,
            "requested_start": start,
            "requested_end": end,
            "available_start": payload["candles"][0]["ts"] if payload["candles"] else None,
            "available_end": payload["candles"][-1]["ts"] if payload["candles"] else None,
        }
    )
    return payload


@app.get("/api/candles/export")
def export_candles(
    instrument_id: str,
    interval: str = "15m",
    range: str = "30d",
) -> StreamingResponse:
    validate_interval(interval)
    validate_range(range)
    start, end = requested_window(range, interval)
    payload = ensure_candles(instrument_id, interval, start, end)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for row in payload["candles"]:
        writer.writerow([row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]])
    symbol = payload["instrument"]["symbol"].replace("/", "_")
    filename = f"{symbol}_{interval}_{range}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/patterns/catalog")
def pattern_catalog() -> list[dict[str, Any]]:
    return PATTERN_CATALOG


@app.post("/api/patterns/scan")
def scan(payload: PatternScanRequest) -> dict[str, Any]:
    validate_interval(payload.interval)
    validate_range(payload.range)
    patterns = validate_patterns(payload.patterns) if payload.patterns else list(CATALOG_BY_ID)
    start, end = requested_window(payload.range, payload.interval)
    instrument = DB.get_instrument(payload.instrument_id)
    if not instrument:
        raise HTTPException(404, "交易标的不存在")
    cached = DB.get_candles(payload.instrument_id, payload.interval, start, end, 20000)
    if cached:
        market_data = {
            "instrument": instrument,
            "candles": cached,
            "truncated": False,
            "warning": None,
        }
    else:
        market_data = ensure_candles(payload.instrument_id, payload.interval, start, end)
    result = scan_patterns(
        market_data["candles"],
        selected_patterns=patterns,
        min_confidence=payload.min_confidence,
        confirmed_only=payload.confirmed_only,
        max_bars=payload.max_bars,
        max_events_per_pattern=20,
        include_statistics=True,
        performance_horizons=(5, 10, 20, 50),
    )
    result.update(
        {
            "instrument": market_data["instrument"],
            "interval": payload.interval,
            "range": payload.range,
            "truncated": market_data["truncated"],
            "warning": market_data["warning"],
        }
    )
    return result


@app.get("/api/alerts/rules")
def alert_rules() -> list[dict[str, Any]]:
    return DB.list_alert_rules()


@app.post("/api/alerts/rules")
def create_alert_rule(payload: AlertRuleCreate) -> dict[str, Any]:
    validate_interval(payload.interval)
    patterns = validate_patterns(payload.patterns)
    if not patterns:
        raise HTTPException(400, "至少选择一个图表形态")
    if not DB.get_instrument(payload.instrument_id):
        raise HTTPException(404, "交易标的不存在")
    data = payload.model_dump()
    data["patterns"] = patterns
    rule = DB.create_alert_rule(data)
    SCANNER.wake()
    return rule


@app.patch("/api/alerts/rules/{rule_id}")
def update_alert_rule(rule_id: int, payload: AlertRuleUpdate) -> dict[str, Any]:
    data = payload.model_dump(exclude_none=True)
    if "interval" in data:
        validate_interval(data["interval"])
    if "patterns" in data:
        data["patterns"] = validate_patterns(data["patterns"])
        if not data["patterns"]:
            raise HTTPException(400, "至少选择一个图表形态")
    rule = DB.update_alert_rule(rule_id, data)
    if not rule:
        raise HTTPException(404, "告警规则不存在")
    SCANNER.wake()
    return rule


@app.delete("/api/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int) -> dict[str, Any]:
    DB.delete_alert_rule(rule_id)
    return {"ok": True}


@app.get("/api/alerts/events")
def alert_events(
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "items": DB.list_alert_events(since_id, limit),
        "unread": DB.unread_alert_count(),
    }


@app.post("/api/alerts/events/read")
def read_alert_events(payload: ReadEventsRequest) -> dict[str, Any]:
    DB.mark_events_read(payload.ids or None)
    return {"ok": True, "unread": DB.unread_alert_count()}


@app.get("/api/opportunities/settings")
def opportunity_settings() -> dict[str, Any]:
    return OPPORTUNITY.get_settings()


@app.put("/api/opportunities/settings")
def update_opportunity_settings(payload: OpportunitySettingsUpdate) -> dict[str, Any]:
    invalid_intervals = sorted(set(payload.intervals) - set(INTERVALS))
    if invalid_intervals:
        raise HTTPException(400, f"不支持的扫描周期：{', '.join(invalid_intervals)}")
    validate_patterns(payload.patterns)
    valid_watch_ids = {int(row["id"]) for row in DB.list_watchlist()}
    invalid_watch = sorted(set(int(v) for v in payload.watchlist_ids) - valid_watch_ids)
    if invalid_watch:
        raise HTTPException(400, "部分自选已不存在，请刷新页面后重试")
    data = payload.model_dump()
    data["horizons"] = sorted({int(v) for v in payload.horizons if int(v) > 0 and int(v) <= 500})
    if not data["horizons"]:
        raise HTTPException(400, "至少保留一个回测持有周期")
    return OPPORTUNITY.save_settings(data)


@app.post("/api/opportunities/run")
def run_opportunity_scan() -> dict[str, Any]:
    return {"ok": True, "status": OPPORTUNITY.request_run()}


@app.get("/api/opportunities/status")
def opportunity_status() -> dict[str, Any]:
    latest = DB.latest_opportunity_run()
    return {"status": OPPORTUNITY.status, "latest_run": latest}


@app.get("/api/opportunities/results")
def opportunity_results(run_id: Optional[int] = Query(default=None, ge=1)) -> dict[str, Any]:
    return DB.opportunity_results(run_id)


@app.get("/api/opportunities/runs")
def opportunity_runs(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    return DB.list_opportunity_runs(limit)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def open_browser_later(host: str, port: int) -> None:
    time.sleep(1.2)
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    webbrowser.open(f"http://{url_host}:{port}")


def main() -> None:
    host = str(os.environ.get("HOST") or CONFIG["host"])
    port = int(os.environ.get("PORT") or CONFIG["port"])
    if bool(CONFIG.get("open_browser")) and os.environ.get("NO_BROWSER") != "1":
        threading.Thread(target=open_browser_later, args=(host, port), daemon=True).start()
    uvicorn.run("app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
