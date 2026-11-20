"""Bounded real-data lifecycle regression; no API, SQL, or trading writes."""
import argparse
import json
import time
from collections import Counter
from pathlib import Path

from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import observe, completion_summary
from gatescope.quant.pattern_study import digest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data-root",type=Path,required=True)
    args=parser.parse_args()
    results=[]
    for symbol in ("BTCUSDT","ETHUSDT"):
        data=json.loads((args.data_root/f"{symbol}-1h.json").read_text())
        bars=[dict(zip(("ts","open","high","low","close","volume"),[r[0]//1000,*map(float,r[1:6])])) for r in data["rows"][-600:]]
        state={"step_seconds":3600}; first={}; journal_hash=""; started=time.monotonic(); peak_active=0
        for i in range(19,len(bars)):
            for t in observe(bars,i,state,snapshot_at=data["fetched_at"]):
                record=t["record"]
                initial=digest(record["first"])
                assert first.setdefault(record["id"],initial)==initial
                assert all(p["time"]<=bars[i]["ts"] for p in record["latest"]["points"])
                journal_hash=digest([journal_hash,t])
            peak_active=max(peak_active,len(state["active"]))
        summary,_=completion_summary(state)
        for r in summary:
            assert r["total"]==sum(r[k] for k in ("FORMING","FORMED","CONFIRMED","INVALIDATED","EXPIRED","CENSORED"))
            assert r["sample_count"]<=r["CONFIRMED"]+r["INVALIDATED"]+r["EXPIRED"]
        results.append({"symbol":symbol,"version":VERSION,"bars":len(bars),"input_digest":digest(bars),
            "candidates":len(state["records"]),"peak_active":peak_active,"phases":dict(Counter(r["phase"] for r in state["records"].values())),
            "journal_digest":journal_hash,"seconds":round(time.monotonic()-started,3)})
        print(json.dumps(results[-1],sort_keys=True),flush=True)


if __name__=="__main__":main()
