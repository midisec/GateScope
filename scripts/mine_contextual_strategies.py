"""One bounded transfer study; data inspected after the first batch is NOT reused as holdout."""
from collections import defaultdict
import gc
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime,timezone

import mine_directional_strategies as base
from gatescope.quant.contextual_directional import CONTEXTS,ContextRule,VERSION,evaluate,filtered,rules,signal_context
from gatescope.quant.directional import FAMILIES,funding_series
from gatescope.quant.artifacts import report_digest
from gatescope.quant.performance import performance_screen


def main():
    os.umask(0o077)
    base.SOURCE=Path("data/research-market/binance-contextual-20210101-20260911")
    symbols=("LINKUSDT","ETCUSDT","AVAXUSDT","DOTUSDT")
    target=Path("data/research-studies")/datetime.now(timezone.utc).strftime("contextual-%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True)
    hashes={}
    for path in base.SOURCE.glob("*.json"):
        r=json.loads(path.read_text())
        assert hashlib.sha256(json.dumps(r["rows"],separators=(",",":")).encode()).hexdigest()==r["sha256"]
        hashes[path.name]=r["sha256"]
    assert len(hashes)==8
    plan=dict(engine_version=VERSION,symbols=symbols,timeframes=["1h","4h"],rules=[r.payload() for r in rules()],contexts=CONTEXTS,
        develop_from=base.DEVELOP_START,develop_to=base.DEVELOP_END,validate_to=base.VALIDATE_END,
        selection="Top 3 distinct datasets per family by weaker development-half HAC Sharpe; >=30 development trades, positive return, DD<15%; no post-validation replacements",
        caveats=["Filters motivated by first failed study; these four assets were not used in the first study", "No portfolio or hedge; one directional position per strategy", "Same original structural rules/costs/0.5% risk; no lower threshold", "Universe has survivorship bias; validation is not LIVE certification"],
        source_hashes=hashes,module_sha256=hashlib.sha256(Path("src/gatescope/quant/contextual_directional.py").read_bytes()).hexdigest(),
        original_module_sha256=hashlib.sha256(Path("src/gatescope/quant/directional.py").read_bytes()).hexdigest(),hedged=False,live=False)
    plan["artifact_digest"]=report_digest(plan)
    base.write(target/"plan.json",plan)
    print("FROZEN contextual plan",target,plan["artifact_digest"],flush=True)
    development=[]
    def load(symbol,tf):
        bars=base.load_bars(symbol,tf)
        points,book=signal_context(bars)
        contexts={(family,regime):filtered(points,book[family],regime) for family in FAMILIES for regime in CONTEXTS}
        funding=funding_series(bars,json.loads((base.SOURCE/f"{symbol}-funding.json").read_text())["rows"])
        return bars,points,contexts,funding
    for symbol in symbols:
        for tf in plan["timeframes"]:
            bars,points,book,funding=load(symbol,tf)
            lo,hi=base.boundary(bars,base.DEVELOP_START),base.boundary(bars,base.DEVELOP_END)
            for index,rule in enumerate(rules()):
                r=evaluate(bars,points,book[(rule.family,rule.regime)],rule,lo,hi,funding)
                score,halves=base.scored(r)
                development.append(dict(dataset=f"{symbol}-{tf}",symbol=symbol,timeframe=tf,rule=rule.payload(),metrics=r["metrics"],score=score,halves=halves))
                if (index+1)%96==0:print("Development",symbol,tf,index+1,"/288",flush=True)
            del bars,points,book,funding
            gc.collect()
    base.write(target/"development.json",development)
    assert len(development)==2304
    finalists=[]
    for family in FAMILIES:
        eligible=[r for r in development if r["rule"]["family"]==family and r["metrics"]["trade_count"]>=30 and r["metrics"]["total_return"]>0 and r["metrics"]["max_drawdown"]<.15]
        eligible.sort(key=lambda r:(r["score"],r["metrics"]["sharpe_ratio"] or -100),reverse=True)
        selected=set()
        for r in eligible:
            if r["dataset"] in selected:continue
            finalists.append(r);selected.add(r["dataset"])
            if len(selected)==3:break
    base.write(target/"frozen-finalists.json",finalists)
    print("FROZEN finalists",len(finalists),"before evaluation",flush=True)
    groups=defaultdict(list)
    for index,r in enumerate(finalists,1):groups[(r["symbol"],r["timeframe"])].append((index,r))
    rows=[]
    for (symbol,tf),chosen in groups.items():
        bars,points,book,funding=load(symbol,tf)
        for index,choice in chosen:
            rule=ContextRule(**choice["rule"])
            report={**choice,"validation":{},"full":{},"plan_digest":plan["artifact_digest"]}
            for segment,left in (("validation",base.DEVELOP_END),("full",base.DEVELOP_START)):
                lo=base.boundary(bars,left)+(72 if segment=="validation" else 0)
                for cost in (1,2):
                    report[segment][str(cost)]=evaluate(bars,points,book[(rule.family,rule.regime)],rule,lo,len(bars),funding,cost=cost,detailed=True,daily_curve=True)
            for cost in ("1","2"):
                report["validation"][cost]["screening"]=performance_screen(report["validation"][cost]["metrics"],full_drawdown=report["full"][cost]["metrics"]["max_drawdown"])
                report["validation"][cost]["screening"]["validation_scope"]="FROZEN_LATER_PERIOD_TRANSFER_RESEARCH"
            report["artifact_digest"]=report_digest(report)
            base.write(target/f"finalist-{index:02d}.json",report)
            row=dict(id=index,dataset=choice["dataset"],rule=choice["rule"],development=choice["metrics"],validation={c:r["metrics"] for c,r in report["validation"].items()},screening={c:r["screening"] for c,r in report["validation"].items()},artifact_digest=report["artifact_digest"])
            rows.append(row)
            m=row["validation"]["1"]
            print("RESULT",index,choice["dataset"],choice["rule"],"S",round(m["sharpe_ratio"] or 0,3),"DD",round(m["max_drawdown"],4),"win",round(m["win_rate"] or 0,3),"t",round(m["t_statistic"] or 0,3),"n",m["trade_count"],row["screening"]["1"]["status"],flush=True)
        del bars,points,book,funding
        gc.collect()
    base.write(target/"summary.json",dict(plan=plan,development_trials=len(development),finalists=rows,total_trial_count=len(development)+4*len(rows),qualified=[r["id"] for r in rows if r["screening"]["1"]["shortlisted"]],stress_qualified=[r["id"] for r in rows if r["screening"]["1"]["shortlisted"] and r["screening"]["2"]["shortlisted"]]))
    print("FINISHED",target,flush=True)


if __name__=="__main__":main()
