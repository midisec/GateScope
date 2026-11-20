"""Bounded, pre-registered owner research. No exchange/notification ports.

Freezes all datasets and 24 trials BEFORE examining any performance. Writes
immutable reports through existing research table roles; leaves LIVE disabled.
Run on deployment host with --execute; never prints environment or secrets.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from prepare_service_envs import SECRET_DIR, read_env
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_backtests import BacktestRepository
from gatescope.quant.backtest import ENGINE_VERSION, StrategySpec, run_backtest
from gatescope.quant.performance import POLICY_VERSION
from gatescope.quant.artifacts import report_digest


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()


def save_new(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, default=str, allow_nan=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--username", default="owner")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required to save 24 research reports; no trading is performed")
    os.umask(0o077)
    env = read_env(SECRET_DIR / "formal-runtime.env")
    dsn = env["GATESCOPE_MIGRATION_DATABASE_URL"]
    as_of = datetime.now(timezone.utc)
    end = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=365)
    directory = Path("data/research-studies") / (as_of.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    directory.mkdir(parents=True, mode=0o700)
    with psycopg.connect(dsn) as conn:
        identity = conn.execute("""SELECT t.id,u.id FROM tenants t JOIN memberships m ON m.tenant_id=t.id
            JOIN users u ON u.id=m.user_id WHERE t.slug='default' AND u.normalized_username=%s
            AND u.status='ACTIVE' AND m.status='ACTIVE' AND m.role='OWNER'""", (args.username.lower(),)).fetchone()
        if identity is None:
            raise RuntimeError("Active owner not found")
        tenant, user = identity
        watched = conn.execute("""SELECT DISTINCT i.id,i.symbol FROM watchlist_items wi
            JOIN watchlists w ON w.id=wi.watchlist_id JOIN instruments i ON i.id=wi.instrument_id
            WHERE w.tenant_id=%s AND w.owner_user_id=%s AND i.symbol IN ('BTC_USDT','ETH_USDT')
            ORDER BY i.symbol""", (tenant, user)).fetchall()
        if len(watched) != 2:
            raise RuntimeError("This fixed study requires both watched BTC_USDT and ETH_USDT")
        if conn.execute("SELECT count(*) FROM research_backtests WHERE tenant_id=%s AND owner_user_id=%s AND state IN ('QUEUED','RUNNING')", (tenant,user)).fetchone()[0]:
            raise RuntimeError("Owner has an active research job; do not interrupt it")
        previous_trials = conn.execute("SELECT count(*) FROM research_backtests WHERE tenant_id=%s AND owner_user_id=%s", (tenant,user)).fetchone()[0]
    pool = PostgresPool(dsn, max_size=1)
    pool.open()
    datasets, trials = {}, []
    try:
        repository = BacktestRepository(pool)
        for instrument, symbol in watched:
            for timeframe, expected in (("4h",2190),("8h",1095)):
                manifest = []
                bars = repository.bars(instrument, timeframe, expected, as_of, start, end, source_manifest=manifest)
                step = 14400 if timeframe == "4h" else 28800
                if len(bars) != expected or bars[0].open_time != start or bars[-1].close_time != end or any(
                    (b.open_time-a.open_time).total_seconds()!=step for a,b in zip(bars,bars[1:], strict=False)):
                    raise RuntimeError(f"Incomplete one-year dataset: {symbol} {timeframe}, bars={len(bars)}")
                key = f"{symbol}-{timeframe}"
                datasets[key] = (bars, manifest)
                for strategy in ("pullback", "volume_breakout", "momentum"):
                    for costs in (1,2):
                        spec = StrategySpec(strategy=strategy,direction="BOTH",availability_mode="HISTORICAL_CLOSE",
                            fast_period=20,slow_period=50,lookback=20,adx_threshold=20,
                            stop_atr=2,reward_risk=1.5,max_holding_bars=24,
                            fee_bps=5*costs,slippage_bps=5*costs,funding_bps_per_8h=costs)
                        trials.append(dict(id=str(uuid4()),dataset=key,symbol=symbol,instrument_id=str(instrument),
                            timeframe=timeframe,strategy=strategy,cost_multiplier=costs,spec=spec.model_dump()))
        plan = dict(engine_version=ENGINE_VERSION,policy_version=POLICY_VERSION,created_at=as_of,
            from_time=start,to_time=end,trial_count=len(trials),previous_trials=previous_trials,
            tested_hypotheses=previous_trials+len(trials),trials=trials,
            datasets={k:dict(bar_count=len(b),manifest_hash=digest(m),source_manifest=m) for k,(b,m) in datasets.items()},
            selection="No optimization; all 24 trials registered before outcomes. Candidate requires base and double-cost screening; no live certification.")
        save_new(directory/"plan.json",plan)
        plan_hash = digest(plan)
        print(f"Frozen study: {directory}; trials={len(trials)}; plan_digest={plan_hash}",flush=True)
        results = []
        for index, trial in enumerate(trials,1):
            identifier = trial["id"]
            # Atomic reservation: regular API INSERT and strategy UPDATE are not
            # visible to the background worker between statements.
            with pool.transaction(tenant,user) as conn:
                conn.execute("SET LOCAL ROLE gatescope_api")
                conn.execute("""INSERT INTO research_backtests(id,tenant_id,owner_user_id,instrument_id,timeframe,
                    spec_json,bar_limit,from_time,to_time,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED')""",
                    (identifier,tenant,user,trial["instrument_id"],trial["timeframe"],Jsonb(trial["spec"]),
                     len(datasets[trial["dataset"]][0]),start,end))
                conn.execute("SET LOCAL ROLE gatescope_strategy")
                conn.execute("UPDATE research_backtests SET state='RUNNING',started_at=now() WHERE id=%s",(identifier,))
            try:
                bars, manifest = datasets[trial["dataset"]]
                report = run_backtest(bars,StrategySpec.model_validate(trial["spec"]),
                    tested_hypotheses=plan["tested_hypotheses"],source_manifest=manifest)
                report["study"] = dict(plan_digest=plan_hash,trial_index=index,trial_count=len(trials),cost_multiplier=trial["cost_multiplier"])
                report.pop("artifact_digest")
                report["artifact_digest"] = report_digest(report)
                with pool.transaction(tenant,user) as conn:
                    conn.execute("SET LOCAL ROLE gatescope_strategy")
                    conn.execute("INSERT INTO research_backtest_results(run_id,tenant_id,owner_user_id,report_json) VALUES (%s,%s,%s,%s)",(identifier,tenant,user,Jsonb(report)))
                    conn.execute("UPDATE research_backtests SET state='SUCCEEDED',completed_at=now() WHERE id=%s",(identifier,))
                row = {**trial,"validation":report["validation"],"screening":report["screening"],
                    "full":report["full"]["metrics"],"range":report["range"],"artifact_digest":report["artifact_digest"]}
                results.append(row)
                save_new(directory/f"trial-{index:02d}.json",row)
                m = report["validation"]
                print(f"{index}/{len(trials)} {trial['dataset']} {trial['strategy']} cost={trial['cost_multiplier']} sharpe={m['sharpe_ratio']} t={m['t_statistic']} trades={m['trade_count']} status={report['screening']['status']}",flush=True)
            except Exception:
                with pool.transaction(tenant,user) as conn:
                    conn.execute("SET LOCAL ROLE gatescope_strategy")
                    conn.execute("UPDATE research_backtests SET state='FAILED',completed_at=now(),safe_error='固定研究批次计算失败，保留记录' WHERE id=%s",(identifier,))
                raise
        shortlist = [r["id"] for r in results if r["cost_multiplier"]==1 and r["screening"]["shortlisted"] and any(
            s["dataset"]==r["dataset"] and s["strategy"]==r["strategy"] and s["cost_multiplier"]==2 and s["screening"]["shortlisted"] for s in results)]
        save_new(directory/"summary.json",dict(plan_digest=plan_hash,trials=results,shortlist=shortlist,deployable=[]))
        print(f"Study completed: reports={len(results)}, shortlist={len(shortlist)}, deployed=0; artifact={directory/'summary.json'}",flush=True)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
