#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate public market-data client with proxy, retries and history-limit handling."""

from __future__ import annotations

import math
import time
from typing import Any, Iterable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://api.gateio.ws/api/v4"
INTERVAL_SECONDS: dict[str, int] = {
    "10s": 10,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "8h": 28800,
    "1d": 86400,
    "7d": 604800,
}
MAX_HISTORY_POINTS = 9400
MAX_REQUEST_POINTS = 900


class GateError(RuntimeError):
    pass


class GateClient:
    def __init__(self, proxy: str = "", timeout: tuple[int, int] = (10, 35)) -> None:
        self.proxy = self._normalize_proxy(proxy)
        self.timeout = timeout
        self.session = self._build_session()

    @staticmethod
    def _normalize_proxy(proxy: str) -> str:
        proxy = (proxy or "").strip()
        if proxy and "://" not in proxy:
            proxy = "http://" + proxy
        return proxy

    def _build_session(self) -> requests.Session:
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "gatescope/1.0.0",
            }
        )
        session.trust_env = False
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        return session

    def get_json(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(BASE_URL + endpoint, params=params or {}, timeout=self.timeout)
        except requests.exceptions.InvalidSchema as exc:
            raise GateError(
                "SOCKS 代理缺少依赖，请执行：python3 -m pip install 'requests[socks]'"
            ) from exc
        except requests.RequestException as exc:
            raise GateError(
                f"Gate API 网络请求失败：{exc}。请确认本地代理 {self.proxy or '未设置'} 可用。"
            ) from exc

        if response.status_code != 200:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise GateError(
                f"Gate API 请求失败：HTTP {response.status_code}；URL={response.url}；返回={detail}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GateError("Gate API 返回的不是有效 JSON") from exc

    @staticmethod
    def instrument_id(market: str, settle: str, symbol: str) -> str:
        return f"{market}:{settle}:{symbol}" if settle else f"{market}::{symbol}"

    @staticmethod
    def _stock_hint(base: str, name: str, metadata: dict[str, Any]) -> bool:
        text = " ".join(
            [base, name, str(metadata.get("name", "")), str(metadata.get("title", ""))]
        ).lower()
        keywords = (
            "stock",
            "equity",
            "tokenized",
            "gstock",
            "xstock",
            "nasdaq",
            "nyse",
            "adr",
        )
        if any(word in text for word in keywords):
            return True
        # Gate 的若干股票代币以 G 结尾，例如 SKHYG、SKHYNIXG。
        # 这是启发式标记，前端会明确显示“股票候选”。
        return base.upper().endswith("G") and len(base) >= 4

    def fetch_catalog(self, include_btc_futures: bool = True) -> list[dict[str, Any]]:
        instruments: list[dict[str, Any]] = []

        currencies_raw = self.get_json("/spot/currencies")
        currency_names: dict[str, str] = {}
        if isinstance(currencies_raw, list):
            for item in currencies_raw:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("currency") or "").upper()
                name = str(item.get("name") or item.get("full_name") or code)
                if code:
                    currency_names[code] = name

        spot_raw = self.get_json("/spot/currency_pairs")
        if isinstance(spot_raw, list):
            for item in spot_raw:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("id") or "").upper()
                if not symbol:
                    continue
                base = str(item.get("base") or symbol.split("_")[0]).upper()
                quote = str(item.get("quote") or (symbol.split("_")[1] if "_" in symbol else "")).upper()
                base_name = currency_names.get(base, base)
                is_stock = self._stock_hint(base, base_name, item)
                display = f"{base_name} · {symbol}" if base_name and base_name != base else symbol
                instruments.append(
                    {
                        "id": self.instrument_id("spot", "", symbol),
                        "market": "spot",
                        "settle": "",
                        "symbol": symbol,
                        "display_name": display,
                        "base": base,
                        "quote": quote,
                        "is_stock": is_stock,
                        "status": str(item.get("trade_status") or ""),
                        "metadata": item,
                    }
                )

        settles = ["usdt"] + (["btc"] if include_btc_futures else [])
        for settle in settles:
            try:
                futures_raw = self.get_json(f"/futures/{settle}/contracts")
            except GateError:
                if settle == "btc":
                    continue
                raise
            if not isinstance(futures_raw, list):
                continue
            for item in futures_raw:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("name") or "").upper()
                if not symbol:
                    continue
                base = symbol.split("_")[0]
                display = f"{symbol} 永续"
                instruments.append(
                    {
                        "id": self.instrument_id("futures", settle, symbol),
                        "market": "futures",
                        "settle": settle,
                        "symbol": symbol,
                        "display_name": display,
                        "base": base,
                        "quote": settle.upper(),
                        "is_stock": self._stock_hint(base, display, item),
                        "status": "delisting" if bool(item.get("in_delisting")) else "tradable",
                        "metadata": item,
                    }
                )
        return instruments

    @staticmethod
    def clamp_start(start_ts: int, end_ts: int, interval: str) -> tuple[int, bool]:
        step = INTERVAL_SECONDS[interval]
        earliest = end_ts - (MAX_HISTORY_POINTS - 5) * step
        if start_ts < earliest:
            return earliest, True
        return start_ts, False

    @staticmethod
    def iter_chunks(start_ts: int, end_ts: int, step: int) -> Iterable[tuple[int, int]]:
        chunk = (MAX_REQUEST_POINTS - 1) * step
        cursor = start_ts
        while cursor <= end_ts:
            chunk_end = min(end_ts, cursor + chunk)
            yield cursor, chunk_end
            cursor = chunk_end + step

    def fetch_candles(
        self,
        instrument: dict[str, Any],
        interval: str,
        start_ts: int,
        end_ts: int,
    ) -> tuple[list[tuple[int, float, float, float, float, float]], bool]:
        if interval not in INTERVAL_SECONDS:
            raise GateError(f"不支持的周期：{interval}")
        if interval == "10s" and instrument["market"] == "spot":
            raise GateError("Gate 现货 REST K 线不支持 10s 周期")

        start_ts, truncated = self.clamp_start(start_ts, end_ts, interval)
        step = INTERVAL_SECONDS[interval]
        rows: list[tuple[int, float, float, float, float, float]] = []

        for chunk_start, chunk_end in self.iter_chunks(start_ts, end_ts, step):
            if instrument["market"] == "spot":
                payload = self.get_json(
                    "/spot/candlesticks",
                    {
                        "currency_pair": instrument["symbol"],
                        "from": chunk_start,
                        "to": chunk_end,
                        "interval": interval,
                    },
                )
                if not isinstance(payload, list):
                    raise GateError("Gate 现货 K 线返回格式异常")
                for item in payload:
                    if not isinstance(item, list) or len(item) < 6:
                        continue
                    try:
                        ts = int(float(item[0]))
                        rows.append(
                            (
                                ts,
                                float(item[5]),
                                float(item[3]),
                                float(item[4]),
                                float(item[2]),
                                float(item[1]),
                            )
                        )
                    except (TypeError, ValueError):
                        continue
            elif instrument["market"] == "futures":
                settle = instrument.get("settle") or "usdt"
                payload = self.get_json(
                    f"/futures/{settle}/candlesticks",
                    {
                        "contract": instrument["symbol"],
                        "from": chunk_start,
                        "to": chunk_end,
                        "interval": interval,
                    },
                )
                if not isinstance(payload, list):
                    raise GateError("Gate 合约 K 线返回格式异常")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    try:
                        rows.append(
                            (
                                int(float(item["t"])),
                                float(item["o"]),
                                float(item["h"]),
                                float(item["l"]),
                                float(item["c"]),
                                float(item.get("v", 0)),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
            else:
                raise GateError(f"暂不支持市场类型：{instrument['market']}")
            time.sleep(0.06)

        dedup: dict[int, tuple[int, float, float, float, float, float]] = {row[0]: row for row in rows}
        return [dedup[key] for key in sorted(dedup)], truncated

    def fetch_latest(
        self,
        instrument: dict[str, Any],
        interval: str,
        last_ts: Optional[int],
        overlap_bars: int = 3,
    ) -> list[tuple[int, float, float, float, float, float]]:
        step = INTERVAL_SECONDS[interval]
        now = int(time.time())
        if last_ts is None:
            start = now - min(500, MAX_HISTORY_POINTS - 10) * step
        else:
            start = max(now - MAX_HISTORY_POINTS * step, last_ts - overlap_bars * step)
        rows, _ = self.fetch_candles(instrument, interval, start, now)
        return rows

    def ping(self) -> dict[str, Any]:
        started = time.perf_counter()
        payload = self.get_json("/spot/time")
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return {"latency_ms": latency_ms, "payload": payload}
