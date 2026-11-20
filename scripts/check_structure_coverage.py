"""Deterministic closed-prefix coverage/anchor audit, not an accuracy benchmark.

Uses existing public research cache. No requests, orders, or database writes.
Prints JSON so before/after runs can be compared without mixing model IDs.
"""
import json
import time
from collections import Counter
from pathlib import Path

from gatescope.quant.pattern_study import digest
from gatescope.quant.structure_detection import VERSION, proposals


def main():
    root = Path("data/research-market/binance-directional-20210101-20260911")
    counts, keys, inputs = Counter(), [], []
    started = time.monotonic()
    windows, invalid = 0, 0
    for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "LTCUSDT", "DOGEUSDT"):
        data = json.loads((root / f"{symbol}-1h.json").read_text())
        rows = data["rows"]
        inputs.append([symbol, data["sha256"]])
        for end in range(len(rows)-24*14, len(rows)+1, 24):
            bars = [dict(zip(("ts", "open", "high", "low", "close", "volume"),
                    [r[0]//1000, *map(float, r[1:6])])) for r in rows[end-480:end]]
            by_time = {b["ts"]: b for b in bars}
            windows += 1
            for e in proposals(bars):
                counts[f"{e['pattern']}:{e['phase']}"] += 1
                keys.append(digest([symbol, bars[-1]["ts"], e["pattern"], e["phase"],
                    [[p["time"],p["price"]] for p in e["points"]]]))
                for p in e["points"]:
                    invalid += int(p["time"] not in by_time or abs(p["price"]-by_time[p["time"]][p["basis"].lower()])>max(1e-12,abs(p["price"])*1e-12))
    print(json.dumps({"version":VERSION,"windows":windows,"inputs":inputs,"counts":dict(sorted(counts.items())),
        "proposals":sum(counts.values()),"invalid_anchors":invalid,"keys":sorted(set(keys)),
        "elapsed_seconds":round(time.monotonic()-started,3)}, sort_keys=True))


if __name__ == "__main__":
    main()
