"""Bounded, lease-resumable structural replay and owner-scoped evidence pages.

Each transaction commits a short OHLC prefix, immutable inputs/transitions and
trade-state projections together. Full-year arrays never enter checkpoint JSON.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.quant.artifacts import DIGEST_METHOD, report_digest
from gatescope.quant.backtest import StrategySpec
from gatescope.quant.pattern_study import WINDOW
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import observe
from gatescope.quant.structure_research import ENGINE_VERSION, case_context, dependence_horizon, policy_from_spec, selection, walk_forward_windows

COMPLETED = ("TARGET", "STOP", "TIMEOUT")
PENDING = ("WAITING_CONFIRMATION", "WAITING_ENTRY", "OPEN")


class StructureResearchRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    @staticmethod
    def terminal_candidates(conn: Any, run: UUID, identifiers: list[str]) -> list[str]:
        """Hot-window pruning must never erase a run's terminal identities."""
        if not identifiers:
            return []
        return [r[0] for r in conn.execute("""SELECT DISTINCT candidate_id FROM research_structure_events
            WHERE run_id=%s AND candidate_id=ANY(%s)
              AND phase IN ('CONFIRMED','INVALIDATED','EXPIRED','CENSORED')""", (run,identifiers)).fetchall()]

    def cases(self, tenant: UUID, user: UUID, run: UUID, *, limit: int = 25, offset: int = 0, state: str = "") -> dict[str, Any]:
        with self.pool.transaction(tenant, user) as conn:
            if not conn.execute("SELECT 1 FROM research_backtests WHERE id=%s", (run,)).fetchone():
                raise LookupError("回测不存在")
            tally = conn.execute("SELECT count(*) FROM research_structure_cases WHERE run_id=%s AND (%s='' OR state=%s)", (run, state, state)).fetchone()
            assert tally is not None
            total = tally[0]
            rows = conn.execute("""SELECT candidate_id,pattern,observed_time,selected,baseline,selection_reasons,environment,case_json
                FROM research_structure_cases WHERE run_id=%s AND (%s='' OR state=%s)
                ORDER BY observed_time DESC,candidate_id LIMIT %s OFFSET %s""", (run, state, state, limit+1, offset)).fetchall()
        items = [dict(zip(("candidate_id", "pattern", "observed_time", "selected", "baseline", "selection_reasons", "environment", "case"), row, strict=True)) for row in rows[:limit]]
        return {"items": items, "total": total, "has_more": len(rows)>limit,
            "next_offset": offset+limit if len(rows)>limit else None}

    def evidence(self, tenant: UUID, user: UUID, run: UUID, *, kind: str, limit: int, offset: int) -> dict[str, Any]:
        table, order, field = ("research_structure_inputs", "bar_index", "bar_json") if kind == "inputs" else (
            "research_structure_events", "observed_time,candidate_id,sequence", "event_json")
        with self.pool.transaction(tenant, user) as conn:
            if not conn.execute("SELECT 1 FROM research_backtests WHERE id=%s", (run,)).fetchone():
                raise LookupError("回测不存在")
            tally = conn.execute(f"SELECT count(*) FROM {table} WHERE run_id=%s", (run,)).fetchone()
            assert tally is not None
            total = tally[0]
            rows = conn.execute(f"SELECT {field} FROM {table} WHERE run_id=%s ORDER BY {order} LIMIT %s OFFSET %s", (run, limit+1, offset)).fetchall()
        return {"items": [row[0] for row in rows[:limit]], "total": total, "has_more": len(rows)>limit,
            "next_offset": offset+limit if len(rows)>limit else None}

    @staticmethod
    def _initialize(conn: Any, request: dict[str, Any]) -> dict[str, Any]:
        instrument, timeframe, as_of = request["instrument_id"], request["timeframe"], request["created_at"]
        step = TIMEFRAME_SECONDS[timeframe]
        latest = conn.execute("""SELECT open_time FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
            AND available_at<=%s AND created_at<=%s AND close_time<=%s
            AND (%s::timestamptz IS NULL OR close_time<=%s) ORDER BY open_time DESC LIMIT 1""",
            (instrument,timeframe,as_of,as_of,as_of,request["to_time"],request["to_time"])).fetchone()
        if latest is None:
            raise ValueError("STRUCTURE_RESEARCH_HISTORY_SHORT")
        end = int(latest[0].timestamp())
        start = int(request["from_time"].timestamp()) if request["from_time"] else end-(request["bar_limit"]-1)*step
        end = min(end, start+(request["bar_limit"]-1)*step)
        count = conn.execute("""SELECT count(DISTINCT open_time) FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
            AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
            AND available_at<=%s AND created_at<=%s AND close_time<=%s""", (instrument,timeframe,start,end,as_of,as_of,as_of)).fetchone()[0]
        if count < 300:
            raise ValueError("STRUCTURE_RESEARCH_HISTORY_SHORT")
        market = conn.execute("SELECT market FROM instruments WHERE id=%s", (instrument,)).fetchone()[0]
        expected = max(0,(end-start)//step+1)
        flags = ["INCOMPLETE_REQUESTED_HISTORY"] if count < expected else []
        if request["spec"].get("availability_mode") == "HISTORICAL_CLOSE":
            flags.append("HISTORICAL_AVAILABILITY_ASSUMED")
        return {"version": ENGINE_VERSION, "detector_version": VERSION, "processed": 0, "total": count,
            "expected":expected,
            "start": start, "end": end, "after": start-1, "bars": [], "lifecycle": {"step_seconds": step},
            "input_digest": "", "quality_flags": flags, "delayed_bars": 0, "gaps": 0, "market": market,
            "latest_available": 0, "failures": 0}

    @staticmethod
    def _summary(conn: Any, run: UUID, *, selected: bool = True, baseline: bool = False,
                 start: int | None = None, end: int | None = None, environment: tuple[str,str] | None = None,
                 stress: float = 1, slippage_bps: float = 0) -> dict[str, Any]:
        predicates = ["run_id=%s"]
        params: list[Any] = [run]
        if selected:
            predicates.append("selected")
        elif baseline:
            predicates.append("baseline")
        if start is not None:
            predicates.append("observed_time>=%s")
            params.append(start)
        if end is not None:
            predicates.append("observed_time<%s")
            params.append(end)
        if environment:
            predicates.append("environment->>%s=%s")
            params.extend(environment)
        # Prefix training/test outcomes are resolved strictly before their cut.
        cutoff = end if end is not None else 2**62
        stress_key = str(stress)
        rows = conn.execute(f"""WITH chosen AS (
            SELECT *, CASE WHEN %s=1 THEN case_json ELSE case_json->'cost_stress'->%s END AS priced,
                state=ANY(%s) AND exit_time<%s AS complete
            FROM research_structure_cases WHERE {' AND '.join(predicates)}),
            cohort AS (SELECT *, (priced->>'net_return')::double precision AS adjusted FROM chosen)
            SELECT count(*),count(*) FILTER(WHERE complete),count(*) FILTER(WHERE complete AND adjusted>0),
              count(*) FILTER(WHERE complete AND state='TARGET'),count(*) FILTER(WHERE complete AND state='STOP'),
              count(*) FILTER(WHERE complete AND state='TIMEOUT'),avg(adjusted) FILTER(WHERE complete),
              avg((priced->>'net_r')::double precision) FILTER(WHERE complete),
              avg((priced->>'fees')::double precision) FILTER(WHERE complete),
              avg((priced->>'funding')::double precision) FILTER(WHERE complete),
              sum(greatest(adjusted,0)) FILTER(WHERE complete),-sum(least(adjusted,0)) FILTER(WHERE complete),
              count(*) FILTER(WHERE state='CENSORED'),count(*) FILTER(WHERE state=ANY(%s) OR (state=ANY(%s) AND NOT complete)),
              count(*) FILTER(WHERE state='UNAVAILABLE'),count(*) FILTER(WHERE state='MISSED'),
              count(*) FILTER(WHERE complete AND coalesce((case_json->>'ambiguous')::boolean,false)),
              avg(adjusted) FILTER(WHERE complete AND adjusted>0),-avg(adjusted) FILTER(WHERE complete AND adjusted<=0),
              avg((priced->>'raw_gross_return')::double precision) FILTER(WHERE complete),
              avg((priced->>'slippage_return')::double precision) FILTER(WHERE complete),
              avg((priced->>'costs_return')::double precision) FILTER(WHERE complete)
            FROM cohort""", [stress,stress_key,list(COMPLETED),cutoff,*params,list(PENDING),list(COMPLETED)]).fetchone()
        (total,n,w,target,stop,timeout,mean,mean_r,fees,funding,gain,loss,censored,pending,unavailable,missed,ambiguous,mean_win,mean_loss,raw_gross,slippage,costs)=rows
        return {"case_count": total, "sample_count": n, "wins": w, "losses": n-w,
            "net_win_rate": w/n if n else None, "target_first_rate": target/n if n else None,
            "stop_first_rate": stop/n if n else None, "timeout_rate": timeout/n if n else None,
            "mean_net_return": mean, "mean_net_r": mean_r,
            "profit_factor": gain/loss if loss else None, "mean_fees": fees, "mean_funding": funding,
            "mean_raw_gross_return":raw_gross,"mean_slippage":slippage,"mean_costs":costs,
            "mean_win": mean_win, "mean_loss": mean_loss, "interval": None,
            "interval_reason": "OVERLAPPING_CASES_NOT_IID", "minimum_sample": 20,
            "sample_sufficient": n>=20, "ambiguous_count": ambiguous, "censored_count": censored,
            "pending_count": pending, "states": {"TARGET":target,"STOP":stop,"TIMEOUT":timeout,
                "CENSORED":censored,"PENDING":pending,"UNAVAILABLE":unavailable,"MISSED":missed}}

    def _report(self, conn: Any, run: UUID, request: dict[str,Any], checkpoint: dict[str,Any], spec: StrategySpec) -> dict[str, Any]:
        summary = self._summary(conn, run)
        baseline = self._summary(conn, run, selected=False, baseline=True)
        groups = []
        for dimension in ("trend", "volatility", "volume", "higher_alignment", "score_bucket"):
            values = conn.execute("SELECT DISTINCT coalesce(environment->>%s,'UNKNOWN') FROM research_structure_cases WHERE run_id=%s ORDER BY 1", (dimension,run)).fetchall()
            for (value,) in values:
                groups.append({"dimension": dimension, "value": value,
                    "summary": self._summary(conn,run,environment=(dimension,value)),
                    "baseline": self._summary(conn,run,selected=False,baseline=True,environment=(dimension,value))})
        step = TIMEFRAME_SECONDS[request["timeframe"]]
        from gatescope.quant.structure_validation import time_block_comparison
        observed_span = conn.execute("""SELECT coalesce(max(exit_time-observed_time),0) FROM research_structure_cases
            WHERE run_id=%s AND baseline AND state=ANY(%s) AND exit_time<=%s""",
            (run,list(COMPLETED),checkpoint["after"]+step)).fetchone()[0]
        horizon = dependence_horizon(spec,step,int(observed_span))
        daily_rows = conn.execute("""SELECT observed_time/86400 AS day,
            count(*) FILTER(WHERE selected),coalesce(sum(net_return) FILTER(WHERE selected),0),
            count(*) FILTER(WHERE selected AND net_return>0),count(*),coalesce(sum(net_return),0),
            count(*) FILTER(WHERE net_return>0) FROM research_structure_cases
            WHERE run_id=%s AND baseline AND state=ANY(%s) AND exit_time<=%s
            GROUP BY observed_time/86400 ORDER BY day""", (run,list(COMPLETED),checkpoint["after"]+step)).fetchall()
        comparison = time_block_comparison([
            dict(zip(("day","selected_count","selected_sum","selected_wins","baseline_count","baseline_sum","baseline_wins"),row,strict=True))
            for row in daily_rows],block_days=int(horizon["block_days"]))
        comparison["dependence_horizon"] = horizon
        comparison["note"] += "；时间块包含确认等待、入场延迟与持仓预算，并以实际观察到的更长决策至退出跨度加宽，不据此缩短区间"
        if int(horizon["required_block_days"])>1000:
            comparison.update(status="DEPENDENCE_HORIZON_BUDGET_EXCEEDED",mean_delta_interval=None,
                selected_mean_interval=None,selected_win_rate_interval=None,valid_resamples=0)
            comparison["note"] += "；所需依赖时间块超过计算预算，不输出区间，也不把时间块压短后伪称有效"
        embargo = int(horizon["embargo_bars"])
        folds = []
        for window in walk_forward_windows(checkpoint["first_ts"],checkpoint["after"]+step,step,spec.walk_forward_folds,embargo):
            train = self._summary(conn,run,start=window["train_from"],end=window["train_to"])
            test = self._summary(conn,run,start=window["test_from"],end=window["test_to"])
            base = self._summary(conn,run,selected=False,baseline=True,start=window["test_from"],end=window["test_to"])
            folds.append({**window,"training":train,"validation":test,"baseline":base,
                "parameters_frozen":True,"training_outcomes_before":window["train_to"],
                "retention_ratio": test["case_count"]/base["case_count"] if base["case_count"] else None})
        candidate_rows = conn.execute("""SELECT phase,count(*) FROM (
            SELECT DISTINCT ON(candidate_id) candidate_id,phase FROM research_structure_events WHERE run_id=%s
            ORDER BY candidate_id,sequence DESC) latest GROUP BY phase""", (run,)).fetchall()
        candidate_counts = dict(candidate_rows)
        case_count = conn.execute("SELECT count(*) FROM research_structure_cases WHERE run_id=%s", (run,)).fetchone()[0]
        result = {"engine_version":ENGINE_VERSION,"detector_version":VERSION,"spec":spec.model_dump(),
            "bar_count":checkpoint["processed"],"range":{"from":datetime.fromtimestamp(checkpoint["first_ts"],timezone.utc).isoformat(),
                "to":datetime.fromtimestamp(checkpoint["after"]+step,timezone.utc).isoformat()},
            "structure_research":{"summary":summary,"baseline":baseline,"environment_groups":groups,
                "time_block_comparison":comparison,
                "case_count":case_count,"candidate_counts":candidate_counts,
                "retention_ratio":summary["case_count"]/baseline["case_count"] if baseline["case_count"] else None,
                "walk_forward":{"method":"EXPANDING_PREFIX_FIXED_RULES_PURGED","sealed_holdout":False,
                    "parameters_retrained":False,"folds":folds,"embargo_bars":embargo,
                    "status":"READY" if folds else "INSUFFICIENT_HISTORY",
                    "note":"固定规则滚动评估；训练前缀只使用截点前已结束案例，未在验证段调参，非封存样本外认证"},
                "cost_stress":[{"multiplier":multiple,"summary":self._summary(conn,run,stress=multiple,slippage_bps=spec.slippage_bps)} for multiple in (1,1.5,2)],
                "cost_stress_basis":"冻结信号及进出场时刻，重新计算倍数滑点下的成交价、手续费和资金费；不重新选择交易或改变出场路径",
                "portfolio_metrics_available":False,"portfolio_note":"逐信号独立名义本金观察，允许重叠；不是共享资金组合，不能把收益相加当账户回报",
                "stop_mode":"PRICE_TOUCH","stop_note":"冻结止损价触及模拟；原结构失效条件另外保留；同根止盈止损按止损优先",
                "cases_url":f"/api/v2/research/backtests/{run}/cases","events_url":f"/api/v2/research/backtests/{run}/events",
                "inputs_url":f"/api/v2/research/backtests/{run}/inputs"},
            "full":{"metrics":{"trade_count":summary["sample_count"],"win_rate":summary["net_win_rate"],
                "mean_trade_return":summary["mean_net_return"],"profit_factor":summary["profit_factor"],
                "net_pnl":None,"total_return":None,"max_drawdown":None,"sharpe":None}},
            "validation":folds[-1]["validation"] if folds else None,
            "screening":{"status":"OBSERVE_ONLY","thresholds_enforced":False,"execution_permissions_changed":False},
            "availability":{"mode":spec.availability_mode,"assumed":spec.availability_mode=="HISTORICAL_CLOSE","delayed_bars":checkpoint["delayed_bars"]},
            "quality_flags":checkpoint["quality_flags"],"data_gaps":checkpoint["gaps"],
            "cache_coverage":{"requested_bars":checkpoint["expected"],"available_bars":checkpoint["total"],
                "missing_bars":max(0,checkpoint["expected"]-checkpoint["total"]),
                "ratio":checkpoint["total"]/checkpoint["expected"] if checkpoint["expected"] else None},
            "input_digest":checkpoint["input_digest"],"input_digest_method":"CHAIN_SHA256_CANONICAL_BAR_V1",
            "evidence":{"decision":"RESEARCH_ONLY","sealed_holdout":False,"reasons":[
                "所有形态和失败记录保留；筛选只影响对照分组，不修改识别数量",
                "形态形成、突破确认与扣成本交易盈利是不同终点",
                "重叠信号并非独立交易，不输出虚假的二项置信区间或账户夏普",
                "历史数据可用时间与价格修订按提交时快照冻结；历史收盘模式为重建假设"]}}
        result["artifact_digest_method"] = DIGEST_METHOD
        result["artifact_digest"] = report_digest(result)
        return result

    @staticmethod
    def _save_case(conn: Any, run: UUID, tenant: UUID, user: UUID, row: dict[str,Any]) -> None:
        case = row["case"]
        if case["state"] in COMPLETED:
            from gatescope.quant.structure_trade import reprice_case
            case = {**case,"cost_stress":{str(multiplier):{
                key: repriced.get(key) for key in ("net_return","net_r","fees","funding","raw_gross_return","slippage_return","costs_return")}
                for multiplier in (1.5,2) for repriced in [reprice_case(case,multiplier)]}}
        conn.execute("""INSERT INTO research_structure_cases
            (run_id,tenant_id,owner_user_id,candidate_id,pattern,observed_time,selected,baseline,selection_reasons,environment,
             state,entry_time,exit_time,net_return,net_r,fees,funding,case_json)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(run_id,candidate_id) DO UPDATE SET state=EXCLUDED.state,entry_time=EXCLUDED.entry_time,
              exit_time=EXCLUDED.exit_time,net_return=EXCLUDED.net_return,net_r=EXCLUDED.net_r,fees=EXCLUDED.fees,
              funding=EXCLUDED.funding,case_json=EXCLUDED.case_json""",
            (run,tenant,user,row["candidate_id"],row["pattern"],row["observed_time"],row["selected"],row["baseline"],
             Jsonb(row["selection_reasons"]),Jsonb(row["environment"]),case["state"],case.get("entry_time"),case.get("exit_time"),
             case.get("net_return"),case.get("net_r"),case.get("fees"),case.get("funding"),Jsonb(case)))

    def run_slice(self, *, max_bars: int = 32, budget_seconds: float = 1.5) -> bool:
        from gatescope.quant.structure_trade import advance_case, make_plan, market_context, start_case
        with self.pool.public_transaction() as conn:
            claimed = conn.execute("SELECT * FROM gatescope_security.claim_structure_research()").fetchone()
        if not claimed:
            return False
        run, tenant, user = claimed
        try:
            with self.pool.transaction(tenant,user) as conn:
                conn.execute("SET LOCAL statement_timeout='15s'")
                row = conn.execute("SELECT instrument_id,timeframe,spec_json,bar_limit,from_time,to_time,created_at FROM research_backtests WHERE id=%s FOR UPDATE", (run,)).fetchone()
                assert row is not None
                request = dict(zip(("instrument_id","timeframe","spec","bar_limit","from_time","to_time","created_at"),row,strict=True))
                spec = StrategySpec.model_validate(request["spec"])
                stored = conn.execute("SELECT checkpoint_json FROM research_structure_checkpoints WHERE run_id=%s FOR UPDATE", (run,)).fetchone()
                checkpoint = stored[0] if stored else self._initialize(conn,request)
                if checkpoint["version"] != ENGINE_VERSION or checkpoint["detector_version"] != VERSION:
                    raise ValueError("STRUCTURE_RESEARCH_VERSION_CHANGED")
                if checkpoint["market"] == "SPOT" and spec.direction != "LONG":
                    raise ValueError("STRUCTURE_RESEARCH_SPOT_DIRECTION")
                candles = annual.page(conn,request["instrument_id"],request["timeframe"],request["created_at"],
                    checkpoint["start"],checkpoint["end"],checkpoint["after"],max(1,min(64,max_bars)))
                step = TIMEFRAME_SECONDS[request["timeframe"]]
                if candles:
                    availability = conn.execute("""SELECT DISTINCT ON(open_time) open_time,available_at FROM bars
                        WHERE instrument_id=%s AND timeframe=%s AND is_final
                        AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
                        AND available_at<=%s AND created_at<=%s AND close_time<=%s ORDER BY open_time,revision DESC""",
                        (request["instrument_id"],request["timeframe"],candles[0]["ts"],candles[-1]["ts"],
                         request["created_at"],request["created_at"],request["created_at"])).fetchall()
                    available_by_time = {int(r[0].timestamp()):int(r[1].timestamp()) for r in availability}
                else:
                    available_by_time = {}
                pending = conn.execute("""SELECT candidate_id,pattern,observed_time,selected,baseline,selection_reasons,environment,case_json
                    FROM research_structure_cases WHERE run_id=%s AND state=ANY(%s)""", (run,list(PENDING))).fetchall()
                active = {r[0]:dict(zip(("candidate_id","pattern","observed_time","selected","baseline","selection_reasons","environment","case"),r,strict=True)) for r in pending}
                dirty: dict[str,dict[str,Any]] = {}
                started = time.monotonic()
                for candle in candles:
                    candle = {**candle,"step_seconds":step,"available_at":available_by_time[candle["ts"]]}
                    index = checkpoint["processed"]
                    if index and candle["ts"]-checkpoint["after"] != step:
                        checkpoint["gaps"] += 1
                    checkpoint.setdefault("first_ts",candle["ts"])
                    checkpoint["bars"] = [*checkpoint["bars"],candle][-WINDOW:]
                    checkpoint["latest_available"] = max(checkpoint["latest_available"],
                        candle["ts"]+step if spec.availability_mode == "HISTORICAL_CLOSE" else max(candle["ts"]+step,candle["available_at"]))
                    checkpoint["delayed_bars"] += int(candle["available_at"]>=candle["ts"]+2*step)
                    checkpoint["quality_flags"] = sorted(set(checkpoint["quality_flags"])|set(candle["quality_flags"]))
                    encoded = json.dumps(candle,sort_keys=True,separators=(",",":"),allow_nan=False)
                    checkpoint["input_digest"] = hashlib.sha256((checkpoint["input_digest"]+encoded).encode()).hexdigest()
                    conn.execute("INSERT INTO research_structure_inputs(run_id,tenant_id,owner_user_id,bar_index,open_time,bar_json) VALUES(%s,%s,%s,%s,%s,%s)",
                        (run,tenant,user,index,candle["ts"],Jsonb(candle)))
                    for identifier,item in list(active.items()):
                        item["case"] = advance_case(item["case"],candle)
                        dirty[identifier] = item
                        if item["case"]["state"] not in PENDING:
                            active.pop(identifier)
                    def terminal_guard(identifiers: list[str]) -> list[str]:
                        return self.terminal_candidates(conn,run,identifiers)
                    journal = observe(checkpoint["bars"],len(checkpoint["bars"])-1,checkpoint["lifecycle"],
                        snapshot_at=request["created_at"].isoformat(),source_mode="RESEARCH",index_offset=index-len(checkpoint["bars"])+1,
                        terminal_guard=terminal_guard) if index>=19 else []
                    environment = None
                    for transition in journal:
                        record = transition["record"]
                        conn.execute("""INSERT INTO research_structure_events(run_id,tenant_id,owner_user_id,candidate_id,sequence,observed_time,phase,pattern,event_json)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (run,tenant,user,record["id"],transition["sequence"],transition["observed_time"],transition["phase"],record["pattern"],Jsonb(transition)))
                        if transition["phase"] != spec.structure_entry_phase or transition["from_phase"] == transition["phase"]:
                            continue
                        if conn.execute("SELECT 1 FROM research_structure_cases WHERE run_id=%s AND candidate_id=%s", (run,record["id"])).fetchone() or record["id"] in dirty:
                            continue
                        if environment is None:
                            environment = market_context(checkpoint["bars"])
                        pattern_event = record["latest"]
                        context = case_context(pattern_event,environment)
                        plan = make_plan({**pattern_event,"market_context":context},candle,policy_from_spec(spec,step,spot=checkpoint["market"]=="SPOT"))
                        case = start_case(plan,available_at=checkpoint["latest_available"],source="REPLAY")
                        item = {"candidate_id":record["id"],"pattern":record["pattern"],"observed_time":transition["observed_time"],
                            "environment":context,**selection(pattern_event,context,spec),"case":case}
                        dirty[record["id"]] = item
                        if case["state"] in PENDING:
                            active[record["id"]] = item
                    if index>=19:
                        checkpoint["lifecycle"]["facts"] = []
                        annual.compact(checkpoint["lifecycle"])
                    checkpoint["after"] = candle["ts"]
                    checkpoint["processed"] += 1
                    if time.monotonic()-started >= budget_seconds:
                        break
                complete = not candles or checkpoint["processed"] >= checkpoint["total"] or checkpoint["after"] >= checkpoint["end"]
                if complete:
                    # Pending observations are right-censored, never fabricated wins/losses.
                    for identifier,item in active.items():
                        item["case"] = {**item["case"],"state":"CENSORED","reason":"RESEARCH_RANGE_ENDED"}
                        dirty[identifier] = item
                for item in dirty.values():
                    self._save_case(conn,run,tenant,user,item)
                progress = {"processed_bars":checkpoint["processed"],"total_bars":checkpoint["total"],
                    "phase":"COMPLETE" if complete else "REPLAY","through_time":checkpoint["after"]+step}
                conn.execute("""INSERT INTO research_structure_checkpoints(run_id,tenant_id,owner_user_id,checkpoint_json)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(run_id) DO UPDATE SET checkpoint_json=EXCLUDED.checkpoint_json,updated_at=now()""", (run,tenant,user,Jsonb(checkpoint)))
                if complete:
                    report = self._report(conn,run,request,checkpoint,spec)
                    conn.execute("INSERT INTO research_backtest_results(run_id,tenant_id,owner_user_id,report_json) VALUES(%s,%s,%s,%s)", (run,tenant,user,Jsonb(report)))
                    conn.execute("UPDATE research_backtests SET state='SUCCEEDED',completed_at=now(),lease_until=NULL,progress_json=%s WHERE id=%s", (Jsonb(progress),run))
                else:
                    conn.execute("UPDATE research_backtests SET lease_until=now(),progress_json=%s,safe_error=NULL WHERE id=%s", (Jsonb(progress),run))
        except Exception as exc:
            safe = {"STRUCTURE_RESEARCH_HISTORY_SHORT":"同源研究至少需要 300 根已缓存收盘 K 线",
                "STRUCTURE_RESEARCH_VERSION_CHANGED":"识别引擎版本已变更，旧进度保留，请创建新版研究",
                "STRUCTURE_RESEARCH_SPOT_DIRECTION":"现货回测只支持做多"}.get(str(exc) if isinstance(exc,ValueError) else "")
            from gatescope.observability import event
            event("structure_research.slice_failed",level=40,error_type=type(exc).__name__)
            with self.pool.transaction(tenant,user) as conn:
                failed_row = conn.execute("SELECT progress_json FROM research_backtests WHERE id=%s FOR UPDATE", (run,)).fetchone()
                assert failed_row is not None
                current = failed_row[0]
                failures = int(current.get("retry_count",0))+1
                current["retry_count"] = failures
                failed = safe is not None or failures >= 3
                conn.execute("""UPDATE research_backtests SET state=%s,safe_error=%s,progress_json=%s,
                    lease_until=CASE WHEN %s THEN NULL ELSE now()+interval '30 seconds' END,
                    completed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s""",
                    ("FAILED" if failed else "RUNNING",safe or ("分片计算失败，进度及原始证据已保留" if failed else "分片重试中，已保存的进度不丢失"),Jsonb(current),failed,failed,run))
        return True
