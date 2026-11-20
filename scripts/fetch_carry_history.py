"""Bounded public-only Binance history cache. No credentials are accepted/sent."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp() * 1000)
DIRECTORY = Path("data/research-market/binance-carry-20210101-20260911")


def fetch(task):
    symbol, kind = task
    filename = DIRECTORY / f"{symbol}-{kind}.json"
    if filename.exists():
        print("Cached", filename.name, flush=True)
        return
    host = "https://api.binance.com" if kind == "spot" else "https://fapi.binance.com"
    endpoint = {
        "spot": "/api/v3/klines",
        "perp": "/fapi/v1/klines",
        "mark": "/fapi/v1/markPriceKlines",
        "funding": "/fapi/v1/fundingRate",
    }[kind]
    session = requests.Session()
    session.trust_env = False
    cursor, rows = START, []
    for _page in range(25):
        if cursor >= END:
            break
        params = dict(symbol=symbol, startTime=cursor, endTime=END - 1, limit=1000)
        if kind != "funding":
            params["interval"] = "8h"
        response = session.get(host + endpoint, params=params, timeout=(4, 15), allow_redirects=False)
        if response.status_code == 429:
            # Abort instead of ignoring Retry-After or silently retrying forever.
            raise RuntimeError(
                "Public history rate limited; wait Retry-After=" + response.headers.get("Retry-After", "60")
            )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list) or not batch:
            raise RuntimeError(f"Missing history {symbol} {kind} at {cursor}")
        times = [int(r["fundingTime"] if kind == "funding" else r[0]) for r in batch]
        if times != sorted(set(times)) or times[0] < cursor or times[-1] >= END:
            raise RuntimeError("Unexpected history ordering")
        rows.extend(batch)
        cursor = times[-1] + (1 if kind == "funding" else 28800000)
        if kind == "funding" and len(batch) < 1000:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("History page budget exceeded")
    if kind != "funding" and (
        len(rows) != (END - START) // 28800000
        or any(int(b[0]) - int(a[0]) != 28800000 for a, b in zip(rows, rows[1:], strict=False))
    ):
        raise RuntimeError(f"Incomplete regular history {symbol} {kind}, rows={len(rows)}")
    payload = dict(
        symbol=symbol,
        kind=kind,
        source=host + endpoint,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        from_time=START,
        to_time=END,
        rows=rows,
        sha256=hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
    )
    with filename.open("x") as stream:
        json.dump(payload, stream)
    print("Fetched", filename.name, "rows", len(rows), flush=True)


def main():
    os.umask(0o077)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                fetch, [(symbol, kind) for symbol in SYMBOLS for kind in ("spot", "perp", "mark", "funding")]
            )
        )


if __name__ == "__main__":
    main()
