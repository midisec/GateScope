"""Causal shared-cash allocation over immutable historical OHLC fill events.

Source strategy rules and fill paths are frozen before this allocator runs. It
does not select winners, change a stop, or pretend candle data is an order book.
Every source case receives an allocation decision, including unfilled cases.
"""
from __future__ import annotations

import heapq
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gatescope.quant.artifacts import report_digest
from gatescope.quant.backtest import metrics
from gatescope.quant.performance import daily_performance
from gatescope.quant.reporting import drawdown_analysis

VERSION = "shared-cash-ohlc-portfolio-1"
MAX_CASES, MAX_INPUTS = 20000, 1000000


class PortfolioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    initial_equity: float = Field(default=10000,ge=100,le=1e8)
    risk_fraction: float = Field(default=.01,ge=.001,le=.03)
    max_positions: int = Field(default=5,ge=1,le=20)
    max_exposure_fraction: float = Field(default=1,ge=.05,le=1)
    per_position_fraction: float = Field(default=.2,ge=.01,le=1)

    @model_validator(mode="after")
    def exposure(self) -> "PortfolioPolicy":
        if self.per_position_fraction > self.max_exposure_fraction:
            raise ValueError("单笔名义资金比例不能超过组合总比例")
        return self


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp,timezone.utc).isoformat()


def run_portfolio(cases: list[dict[str,Any]], bars: Iterable[dict[str,Any]], policy: PortfolioPolicy,
                  *, start: int, end: int, sequence_factory: Callable[[str],Any],
                  progress: Callable[[int,int],None] | None = None) -> dict[str,Any]:
    if not start < end or len(cases)>MAX_CASES:
        raise ValueError("组合范围或案例数量超过预算")
    decisions, trades, curve = (sequence_factory(name) for name in ("decisions","trades","curve"))
    active: dict[str,dict[str,Any]] = {}
    marks: dict[str,tuple[int,float]] = {}
    events: list[tuple[int,int,int,str,dict[str,Any]]] = []
    reasons: Counter[str] = Counter()
    seen: set[tuple[str,str,str]] = set()
    cash, reserved, paid_fees, paid_funding = policy.initial_equity, 0.0, 0.0, 0.0
    peak=policy.initial_equity
    accepted, unknown_marks, processed, bankrupt = 0, 0, 0, False

    def decision(row: dict, status: str, reason: str, timestamp: int, **extra: Any) -> None:
        decisions.append({key:row[key] for key in ("source_run_id","candidate_id","symbol","pattern")} |
            {"observed_time":iso(row["observed_time"]),"decision_time":iso(timestamp),"status":status,"reason":reason,**extra})
        if status=="REJECTED":
            reasons[reason]+=1

    for rank,row in enumerate(cases):
        identity=f"{row['source_run_id']}:{row['candidate_id']}"
        row={**row,"identity":identity}
        timestamp=max(start,min(end,row["observed_time"]))
        entry,exit_at=row.get("entry_time"),row.get("exit_time")
        duplicate=(row["instrument_id"],row["timeframe"],row["candidate_id"])
        if not row["selected"]:
            decision(row,"REJECTED","SOURCE_RULE_EXCLUDED",timestamp)
        elif row["observed_time"]<start or row["observed_time"]>=end or (entry is not None and entry<start):
            decision(row,"REJECTED","OUTSIDE_COMMON_WINDOW",timestamp)
        elif duplicate in seen:
            decision(row,"REJECTED","DUPLICATE_SIGNAL",timestamp)
        elif entry is not None and entry>=end:
            decision(row,"REJECTED","WINDOW_END_CENSORED",end)
        elif entry is None:
            outcome=row.get("outcome_available_at",row.get("outcome_time"))
            if outcome is None or outcome>end:
                decision(row,"REJECTED","WINDOW_END_CENSORED",end)
            else:
                decision(row,"REJECTED",f"SOURCE_{row['state']}",max(timestamp,outcome))
        elif entry<max(row["observed_time"],row.get("available_at") or 0,row.get("eligible_at") or 0) or (exit_at is not None and exit_at<=entry):
            decision(row,"REJECTED","INVALID_SOURCE_TIME",timestamp)
        elif not all(isinstance(row.get(key),(float,int)) and not isinstance(row[key],bool) and math.isfinite(row[key]) and row[key]>0 for key in ("entry_price","stop_price","raw_entry_price")) or row.get("side") not in (-1,1) or isinstance(row.get("side"),bool):
            decision(row,"REJECTED","INVALID_SOURCE_PLAN",timestamp)
        elif any(not isinstance(value,(float,int)) or isinstance(value,bool) or not math.isfinite(value) or not 0<=value<=100
                 for value in (row.get("fee_bps"),row.get("slippage_bps",0),row.get("funding_bps_per_8h"))) or (
                 not isinstance(row.get("max_holding_bars",0),int) or isinstance(row.get("max_holding_bars",0),bool) or not 0<=row.get("max_holding_bars",0)<=10000):
            decision(row,"REJECTED","INVALID_SOURCE_POLICY",timestamp)
        else:
            seen.add(duplicate)
            heapq.heappush(events,(entry,2,rank,"ENTRY",row))
            received=row.get("exit_available_at",exit_at)
            exit_available=max(exit_at,received) if exit_at is not None and received is not None else None
            if exit_available is not None and exit_available<=end and row["state"] in {"TARGET","STOP","TIMEOUT"}:
                heapq.heappush(events,(exit_available,1,rank,"EXIT",row))

    # Complete UTC dates include flat days. Marks are only previously observed
    # source closes; there is no price interpolation or access to a later bar.
    for timestamp in range((start//86400+1)*86400,end+1,86400):
        heapq.heappush(events,(timestamp,3,0,"CLOCK",{}))
    heapq.heappush(events,(end,3,0,"CLOCK",{}))
    iterator=iter(bars)
    next_bar=next(iterator,None)

    def equity_at(timestamp: int) -> float | None:
        value=cash+reserved
        for position in active.values():
            recorded=marks.get(position["source_run_id"])
            if recorded is None or timestamp-recorded[0]>position["step_seconds"]*2:
                return None
            close=recorded[1]
            funding=position["notional"]*position["funding_rate"]*max(0,timestamp-position["entry_ts"])/28800
            value+=position["side"]*position["quantity"]*(close-position["entry_price"])-funding-position["quantity"]*close*position["fee_rate"]
        return value

    curve.append({"time":iso(start),"equity":cash,"cash":cash,"reserved":0.0,"positions":0,"drawdown":0.0})
    while events or next_bar is not None:
        bar_time=next_bar["mark_time"] if next_bar is not None else end+1
        event_time=events[0][0] if events else end+1
        timestamp=min(bar_time,event_time)
        if timestamp>end:
            break
        # Source stream must be monotonically available, not just open-sorted.
        while next_bar is not None and next_bar["mark_time"]==timestamp:
            if start<=timestamp<=end:
                closed_at=next_bar.get("close_time",timestamp)
                if closed_at>timestamp:
                    raise ValueError("组合冻结行情包含未来收盘")
                previous=marks.get(next_bar["source_run_id"])
                if previous is None or closed_at>=previous[0]:
                    marks[next_bar["source_run_id"]]=(closed_at,float(next_bar["close"]))
            next_bar=next(iterator,None)
            processed+=1
            if processed>MAX_INPUTS:
                raise ValueError("组合行情超过 1000000 根预算，请缩短区间或减少来源")
            if next_bar is not None and next_bar["mark_time"]<timestamp:
                raise ValueError("组合冻结行情可用时间不连续")
            if progress and processed%1024==0:
                progress(processed,MAX_INPUTS)
        if timestamp<start:
            continue
        while events and events[0][0]==timestamp:
            _,_,_,kind,row=heapq.heappop(events)
            if kind=="CLOCK":
                continue
            identity=row["identity"]
            if kind=="EXIT":
                position=active.pop(identity,None)
                if position is None:
                    continue
                price=float(row["exit_price"])
                if not math.isfinite(price) or price<=0:
                    raise ValueError("组合退出价格证据无效")
                gross=position["side"]*position["quantity"]*(price-position["entry_price"])
                fee=position["quantity"]*price*position["fee_rate"]
                executed_at=row["exit_time"]
                funding=position["notional"]*position["funding_rate"]*(executed_at-position["entry_ts"])/28800
                cash+=position["notional"]+gross-fee-funding
                reserved-=position["notional"]
                if abs(reserved)<1e-8:
                    reserved=0.0
                paid_fees+=fee
                paid_funding+=funding
                net=gross-position["entry_fee"]-fee-funding
                trades.append({key:position[key] for key in ("source_run_id","candidate_id","instrument_id","symbol","timeframe","pattern","side","entry_time","entry_price","notional","quantity")} |
                    {"state":row["state"],"exit_time":iso(executed_at),"settled_at":iso(timestamp),"exit_price":price,"net_pnl":net,
                     "net_return":net/position["notional"],"fees":position["entry_fee"]+fee,"funding":funding,
                     "source_case_net_return":row.get("net_return"),"ambiguous":bool(row.get("ambiguous"))})
                continue
            equity=equity_at(timestamp)
            reason=None
            if equity is None:
                reason="STALE_MARK_NO_NEW_RISK"
            elif cash<=0 or equity<=0:
                reason="CAPITAL_EXHAUSTED"
            elif len(active)>=policy.max_positions:
                reason="MAX_CONCURRENT_POSITIONS"
            elif any(position["instrument_id"]==row["instrument_id"] for position in active.values()):
                reason="SYMBOL_ALREADY_OPEN_NO_HEDGE"
            relative_risk=abs(row["entry_price"]-row["stop_price"])/row["entry_price"]
            if relative_risk<=0 or row["side"]*(row["entry_price"]-row["stop_price"])<=0:
                reason="INVALID_SOURCE_PLAN"
            fee_rate=row["fee_bps"]/10000
            stop_execution=row["stop_price"]*(1-row["side"]*row.get("slippage_bps",0)/10000)
            stop_cost_risk=max(0,row["side"]*(row["entry_price"]-stop_execution)/row["entry_price"])+fee_rate*(1+stop_execution/row["entry_price"])
            stop_cost_risk+=row["funding_bps_per_8h"]/10000*row.get("max_holding_bars",0)*row["step_seconds"]/28800
            if reason is None:
                immediate_cost=fee_rate+fee_rate*row["raw_entry_price"]/row["entry_price"]-row["side"]*(row["raw_entry_price"]-row["entry_price"])/row["entry_price"]
                exposure_room=max(0,equity*policy.max_exposure_fraction-reserved)/(1+policy.max_exposure_fraction*max(0,immediate_cost))
                notional=min(equity*policy.risk_fraction/stop_cost_risk,equity*policy.per_position_fraction,
                    exposure_room,cash/(1+fee_rate))
                if notional<=1e-8:
                    reason="NO_FREE_CAPITAL_OR_EXPOSURE"
            if reason:
                decision(row,"REJECTED",reason,timestamp)
                continue
            quantity=notional/row["entry_price"]
            entry_fee=notional*fee_rate
            cash-=notional+entry_fee
            if -1e-8<cash<0:
                cash=0.0
            reserved+=notional
            paid_fees+=entry_fee
            accepted+=1
            position={key:row[key] for key in ("source_run_id","candidate_id","instrument_id","symbol","timeframe","pattern","side","entry_price","step_seconds")}
            position.update(entry_time=iso(timestamp),entry_ts=timestamp,notional=notional,quantity=quantity,
                entry_fee=entry_fee,fee_rate=fee_rate,funding_rate=row["funding_bps_per_8h"]/10000)
            active[identity]=position
            # A recorded executable open is known at entry, unlike its future close.
            marks[row["source_run_id"]]=(timestamp,float(row["raw_entry_price"]))
            decision(row,"ACCEPTED","ALLOCATED_AT_RECORDED_ENTRY",timestamp,notional=notional,
                quantity=quantity,risk_budget=notional*stop_cost_risk,
                price_distance_risk=notional*relative_risk,modeled_stop_price=stop_execution)
        equity=equity_at(timestamp)
        unknown_marks+=int(equity is None)
        bankrupt|=equity is not None and equity<=0
        peak=max(peak,equity if equity is not None else peak)
        point={"time":iso(timestamp),"equity":equity,"cash":cash,"reserved":reserved,"positions":len(active),
            "drawdown":equity/peak-1 if equity is not None and unknown_marks==0 and peak>0 else None}
        if curve[-1]["time"]==point["time"]:
            # Initial curve row is before entries; duplicate-timestamp baseline
            # is omitted to avoid a chart shift or duplicate daily return.
            if timestamp!=start:
                raise ValueError("组合资金曲线时间重复")
        else:
            curve.append(point)

    open_positions=[]
    for position in active.values():
        open_positions.append({key:position[key] for key in ("source_run_id","candidate_id","symbol","pattern","side","entry_time","entry_price","notional","quantity")} |
            {"state":"OPEN_CENSORED","exit_time":None,"exit_price":None,"net_pnl":None,
             "fees":position["entry_fee"],"funding":position["notional"]*position["funding_rate"]*max(0,end-position["entry_ts"])/28800})
    complete_metrics=unknown_marks==0
    if progress:
        progress(processed,processed)
    if complete_metrics:
        summary={**metrics(trades,curve,policy.initial_equity),**daily_performance(curve)}
        if progress:
            progress(processed,processed)
        drawdown=drawdown_analysis(curve,output=sequence_factory("drawdown"))["summary"]
    else:
        summary={"trade_count":len(trades),"win_rate":sum(trade["net_pnl"]>0 for trade in trades)/len(trades) if trades else None,
            "net_pnl":None,"total_return":None,"max_drawdown":None,"sharpe_ratio":None,"t_statistic":None}
        drawdown=None
    summary.update(fees=paid_fees,funding=paid_funding+sum(position["funding"] for position in open_positions))
    result={"engine_version":VERSION,"range":{"from":iso(start),"to":iso(end)},"policy":policy.model_dump(),
        "metrics":summary,"metrics_available":complete_metrics,"initial_equity":policy.initial_equity,
        "final_cash":cash,"final_reserved":reserved,"open_position_count":len(active),"open_positions":open_positions,
        "source_case_count":len(cases),"accepted_count":accepted,"rejected_count":sum(reasons.values()),
        "rejection_reasons":dict(reasons),"unknown_mark_points":unknown_marks,"collateral_exhausted":bankrupt,
        "drawdown":{"summary":drawdown},"input_bar_count":processed,
        "detail_counts":{"decisions":len(decisions),"trades":len(trades),"curve":len(curve)},
        "assumptions":["共享现金、1 倍名义资金占用；入场时扣费并预留本金，平仓后才释放",
            "按冻结来源规则的历史 OHLC 入场与退出事件分配资金，不根据未来盈亏排序或重新挑选成功案例",
            "同一时间先处理已知平仓，再按所选来源顺序及案例编号分配入场；同币种不叠仓、不对冲",
            "风险预算包含止损价差、已知进出场费用、止损滑点和配置持仓期限内资金成本；跳空、额外延迟或更长持仓仍可能超出预算，不是保证亏损上限",
            "待触发信号不预占本金；数量为连续名义数量，未模拟历史交易所手数、盘口深度与排队成交",
            "原形态的同根止盈止损规则、滑点、手续费和资金费模型保持不变；没有伪造历史订单簿",
            "资金曲线用当时已知的冻结收盘价或实际模拟入场价逐时估值；数据缺口不插值",
            "未结束持仓保留为右删失，不虚构平仓；缺失估值时账户收益、回撤和夏普留空",
            "这些来源及参数由用户选择，不是封存样本外验证；不承诺未来收益、不设部署绩效门槛"]}
    result["artifact_digest"]=report_digest(result)
    return result
