"""Bounded, public-only directional research cache, with no account credentials."""
import hashlib
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "LTCUSDT", "DOGEUSDT")
START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp() * 1000)
DIRECTORY = Path("data/research-market/binance-directional-20210101-20260911")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--symbols",nargs="+",choices=(*SYMBOLS,"LINKUSDT","ETCUSDT","AVAXUSDT","DOTUSDT"),default=SYMBOLS)
    parser.add_argument("--directory",type=Path,default=DIRECTORY)
    args=parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.trust_env = False
    for symbol in args.symbols:
        for kind in ("1h", "funding"):
            target = args.directory / f"{symbol}-{kind}.json"
            if target.exists():
                print("Cached", target.name, flush=True)
                continue
            endpoint = "/fapi/v1/klines" if kind == "1h" else "/fapi/v1/fundingRate"
            rows, cursor = [], START
            for page in range(60):
                if cursor >= END:
                    break
                params = dict(symbol=symbol, startTime=cursor, endTime=END-1, limit=1000)
                if kind == "1h":
                    params["interval"] = "1h"
                for attempt in range(3):
                    response = session.get("https://fapi.binance.com" + endpoint, params=params,
                                           timeout=(5, 20), allow_redirects=False)
                    if response.status_code == 429:
                        delay = min(60, max(1, float(response.headers.get("Retry-After", "60"))))
                        raise RuntimeError(f"Rate limited; retry manually after Retry-After (at least {delay}s)")
                    if response.status_code >= 500 and attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                    response.raise_for_status()
                    break
                batch = response.json()
                if not isinstance(batch, list) or not batch:
                    raise ValueError(f"Missing public history: {symbol}/{kind}")
                stamps = [int(r[0] if kind == "1h" else r["fundingTime"]) for r in batch]
                if stamps != sorted(set(stamps)) or stamps[0] < cursor or stamps[-1] >= END:
                    raise ValueError("Unordered public history")
                rows.extend(batch)
                cursor = stamps[-1] + (3600000 if kind == "1h" else 1)
                if kind == "funding" and len(batch) < 1000:
                    break
                time.sleep(.15)
            else:
                raise ValueError("Public request page budget exceeded")
            stamps = [int(r[0] if kind == "1h" else r["fundingTime"]) for r in rows]
            if kind == "1h":
                if len(rows) != (END-START)//3600000 or any(b-a != 3600000 for a,b in zip(stamps,stamps[1:])):
                    raise ValueError("Missing hourly candle; do not interpolate")
            elif stamps[0]-START > 9*3600000 or END-stamps[-1] > 9*3600000 or max(b-a for a,b in zip(stamps,stamps[1:])) > 9*3600000:
                raise ValueError("Funding history gap exceeds 9 hours")
            payload = dict(symbol=symbol, kind=kind, rows=rows, source="https://fapi.binance.com"+endpoint,
                           fetched_at=datetime.now(timezone.utc).isoformat(), from_time=START, to_time=END,
                           sha256=hashlib.sha256(json.dumps(rows,separators=(",", ":")).encode()).hexdigest())
            with target.open("x") as stream:
                json.dump(payload, stream, allow_nan=False)
            print("Fetched", target.name, "rows", len(rows), flush=True)


if __name__ == "__main__":
    main()
