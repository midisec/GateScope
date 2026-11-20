"""Pre-registered development grid, then ONE final evaluation per selected model.

Only reads the production DB. All trials and snapshots are kept in private study
files, not 2,000 failed configurations in the user's normal strategy list.
"""
import bisect
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from prepare_service_envs import SECRET_DIR, read_env
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.quant.mining import Candidate,candidates,mining_features,candidate_signals,evaluate,FAMILIES
from gatescope.quant.artifacts import report_digest


def write(path,value):
    with path.open("x",encoding="utf-8") as stream:
        json.dump(value,stream,ensure_ascii=False,default=str,allow_nan=False)


def main():
    os.umask(0o077)
    directory=Path("data/research-studies")/datetime.now(timezone.utc).strftime("mining-%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True)
    plan=dict(version="family-mining-v1",develop_from="2020-01-01",develop_to="2023-01-01",
        untouched_from="2023-01-01",untouched_to="2024-05-01",recent_diagnostic_from="2024-05-01",
        recent_diagnostic_to="2026-09-11",recent_previously_seen=True,candidates=[c.payload() for c in candidates()],
        symbols=["BTC_USDT","ETH_USDT"],timeframes=["8h","1d"],
        selection="Top 2 per family by minimum annual development Sharpe, ties full-development Sharpe; require 30 development trades and MDD<15%. No holdout-based replacement.",
        live=False)
    write(directory/"plan.json",plan)
    pool=PostgresPool(read_env(SECRET_DIR/"formal-runtime.env")["GATESCOPE_MIGRATION_DATABASE_URL"],max_size=1)
    pool.open()
    datasets,trial_rows={},[]
    started=time.monotonic()
    print("Mining artifact:",directory,flush=True)
    try:
        with pool.public_transaction() as conn:
            instruments=conn.execute("SELECT DISTINCT i.id,i.symbol FROM instruments i JOIN watchlist_items w ON w.instrument_id=i.id WHERE i.symbol IN ('BTC_USDT','ETH_USDT') ORDER BY i.symbol").fetchall()
        reader=BacktestRepository(pool)
        for instrument,symbol in instruments:
            for timeframe in plan["timeframes"]:
                bars=[]
                for lo,hi in (("2019-01-01","2023-01-01"),("2023-01-01","2026-09-11")):
                    bars.extend(reader.bars(instrument,timeframe,5000,datetime.now(timezone.utc),datetime.fromisoformat(lo).replace(tzinfo=timezone.utc),datetime.fromisoformat(hi).replace(tzinfo=timezone.utc)))
                step=28800 if timeframe=="8h" else 86400
                if any((b.open_time-a.open_time).total_seconds()!=step for a,b in zip(bars,bars[1:])):
                    raise RuntimeError("Dataset gap; do not compress missing time")
                times=[b.open_time.timestamp() for b in bars]
                boundary=lambda day:bisect.bisect_left(times,datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
                bounds={day:boundary(day) for day in ("2020-01-01","2021-01-01","2022-01-01","2023-01-01","2024-05-01","2026-09-11")}
                points=mining_features(bars)
                key=f"{symbol}-{timeframe}"
                datasets[key]=(bars,points,bounds)
                write(directory/f"{key}-input.json",[dict(venue=b.venue,market=b.market,instrument_id=b.instrument_id,timeframe=b.timeframe,
                    open_time=b.open_time,close_time=b.close_time,open=str(b.open),high=str(b.high),low=str(b.low),close=str(b.close),volume=str(b.volume),
                    available_at=b.available_at,revision=b.revision,quality_flags=sorted(b.quality_flags),source_sequence=b.source_sequence) for b in bars])
                # Cache signals per entry rule: risk settings never change entry features.
                cache={}
                for index,candidate in enumerate(candidates(),1):
                    signal_key=(candidate.family,candidate.threshold,candidate.regime,candidate.direction)
                    if signal_key not in cache:
                        cache[signal_key]=candidate_signals(bars,points,candidate)
                    signals=cache[signal_key]
                    lo=max(201,bounds["2020-01-01"])
                    full=evaluate(bars,signals,candidate,lo,bounds["2023-01-01"])
                    years=[]
                    for left,right in (("2020-01-01","2021-01-01"),("2021-01-01","2022-01-01"),("2022-01-01","2023-01-01")):
                        years.append(evaluate(bars,signals,candidate,max(201,bounds[left])+candidate.holding,bounds[right])["metrics"])
                    score=min(m["sharpe_ratio"] if m["sharpe_ratio"] is not None else -100. for m in years)
                    row=dict(dataset=key,candidate=candidate.payload(),metrics=full["metrics"],years=years,score=score)
                    trial_rows.append(row)
                    if index%96==0:
                        print(key,index,"/",len(candidates()),"elapsed",round(time.monotonic()-started),flush=True)
                cache.clear()
        write(directory/"development-trials.json",trial_rows)
        finalists=[]
        for family in FAMILIES:
            eligible=[r for r in trial_rows if r["candidate"]["family"]==family and r["metrics"]["trade_count"]>=30 and r["metrics"]["max_drawdown"]<.15]
            eligible.sort(key=lambda r:(r["score"],r["metrics"]["sharpe_ratio"] or -100),reverse=True)
            finalists.extend(eligible[:2])
        write(directory/"frozen-finalists.json",finalists)
        print("Development complete; frozen finalists",len(finalists),"before holdout metrics",flush=True)
        results=[]
        for idx,choice in enumerate(finalists,1):
            bars,points,bounds=datasets[choice["dataset"]]
            candidate=Candidate(**choice["candidate"])
            signals=candidate_signals(bars,points,candidate)
            result={**choice,"holdout":{},"recent_diagnostic":{}}
            for cost in (1,2):
                result["holdout"][str(cost)]=evaluate(bars,signals,candidate,bounds["2023-01-01"]+candidate.holding,bounds["2024-05-01"],cost)
                result["recent_diagnostic"][str(cost)]=evaluate(bars,signals,candidate,bounds["2024-05-01"]+candidate.holding,bounds["2026-09-11"],cost)
            result["artifact_digest"]=report_digest(result)
            write(directory/f"finalist-{idx:02d}.json",result)
            results.append(result)
            h=result["holdout"]["1"];r=result["recent_diagnostic"]["1"]
            print("FINAL",choice["dataset"],candidate.family,"holdout",h["metrics"],"recent",r["metrics"],flush=True)
        write(directory/"summary.json",dict(development_trials=len(trial_rows),finalists=[dict(dataset=r["dataset"],candidate=r["candidate"],
            holdout={k:dict(metrics=v["metrics"],screening=v["screening"]) for k,v in r["holdout"].items()},
            recent_diagnostic={k:dict(metrics=v["metrics"],screening=v["screening"]) for k,v in r["recent_diagnostic"].items()}) for r in results]))
        print("Finished mining:",directory,"elapsed",round(time.monotonic()-started),flush=True)
    finally:
        pool.close()


if __name__=="__main__":
    main()
