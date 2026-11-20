"""Shared-capital portfolio jobs over owner-scoped immutable structure studies."""
from __future__ import annotations

import heapq
import time
from datetime import datetime
from typing import Any,Callable,Iterator
from uuid import UUID,uuid4

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.observability import event
from gatescope.quant.artifacts import report_digest
from gatescope.quant.portfolio_research import MAX_CASES,MAX_INPUTS,VERSION,PortfolioPolicy,run_portfolio
from gatescope.quant.research_sequences import CHUNK_ROWS,ResearchSpool,decode_chunk


class PortfolioResearchRepository:
    def __init__(self,pool: PostgresPool) -> None:
        self.pool=pool

    @staticmethod
    def _row(row: Any) -> dict[str,Any]:
        return dict(zip(("id","source_run_ids","policy","state","created_at","completed_at","error","progress","report","from_time","to_time","source_manifest"),row,strict=True))

    @staticmethod
    def _select(*,include_report: bool=True) -> str:
        report="report_json" if include_report else "NULL"
        return f"SELECT id,source_run_ids,policy_json,state,created_at,completed_at,safe_error,progress_json,{report},from_time,to_time,source_manifest FROM research_portfolios"

    def create(self,tenant: UUID,user: UUID,sources: list[UUID],policy: PortfolioPolicy,
               start: datetime|None=None,end: datetime|None=None) -> dict[str,Any]:
        if not 1<=len(sources)<=8 or len(set(sources))!=len(sources):
            raise ValueError("请选择 1–8 个不重复的已完成形态研究")
        identifier=uuid4()
        with self.pool.transaction(tenant,user) as conn:
            conn.execute("SET LOCAL statement_timeout='8s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            rows=conn.execute("""SELECT r.id,r.instrument_id,i.symbol,r.timeframe,r.spec_json,
                j.report_json->'range',j.report_json->>'artifact_digest',j.report_json->>'input_digest',
                j.report_json->>'engine_version',i.venue,i.market,i.quote_asset,i.settle_asset FROM research_backtests r
                JOIN research_backtest_results j ON j.run_id=r.id JOIN instruments i ON i.id=r.instrument_id
                WHERE r.id=ANY(%s) AND r.state='SUCCEEDED' AND r.spec_json->>'engine_mode'='STRUCTURE'""",(sources,)).fetchall()
            if len(rows)!=len(sources):
                raise LookupError("来源必须是当前用户已完成的同源形态研究")
            if len({row[9] for row in rows})!=1:
                raise ValueError("共享资金组合要求来源属于同一交易所")
            if len({row[10] for row in rows})!=1:
                raise ValueError("共享资金组合不能混用现货与合约账户资金")
            if any(row[11]!="USDT" or row[10] not in {"SPOT","FUTURES_USDT"} or (row[10]!="SPOT" and row[12]!="USDT") for row in rows):
                raise ValueError("组合只支持 USDT 计价与结算的来源，不能混合计价维度")
            indexed={row[0]:row for row in rows}
            manifest=[]
            for source in sources:
                row=indexed[source]
                manifest.append({"run_id":str(row[0]),"instrument_id":str(row[1]),"symbol":row[2],"timeframe":row[3],
                    "spec":row[4],"range":row[5],"artifact_digest":row[6],"input_digest":row[7],"engine_version":row[8]})
                manifest[-1].update(venue=row[9],market=row[10],quote_asset=row[11],settle_asset=row[12])
            left=max(datetime.fromisoformat(source["range"]["from"]) for source in manifest)
            right=min(datetime.fromisoformat(source["range"]["to"]) for source in manifest)
            left=max(left,start) if start else left
            right=min(right,end) if end else right
            if left>=right:
                raise ValueError("来源历史没有共同覆盖区间，请调整研究或日期")
            case_count=conn.execute("SELECT count(*) FROM (SELECT 1 FROM research_structure_cases WHERE run_id=ANY(%s) LIMIT %s) bounded",
                (sources,MAX_CASES+1)).fetchone()[0]
            if case_count>MAX_CASES:
                raise ValueError("来源案例超过 20000 条，请选择更短的来源研究或减少来源")
            input_count=conn.execute("SELECT count(*) FROM (SELECT 1 FROM research_structure_inputs WHERE run_id=ANY(%s) AND open_time>=%s AND open_time<%s LIMIT %s) bounded",
                (sources,int(left.timestamp()),int(right.timestamp()),MAX_INPUTS+1)).fetchone()[0]
            if input_count>MAX_INPUTS:
                raise ValueError("组合行情超过 1000000 根预算，请缩短区间或减少来源")
            conn.execute("""INSERT INTO research_portfolios(id,tenant_id,owner_user_id,source_run_ids,policy_json,
                from_time,to_time,source_manifest,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED')""",
                (identifier,tenant,user,sources,Jsonb(policy.model_dump()),left,right,Jsonb(manifest)))
        return self.get(tenant,user,identifier)

    def get(self,tenant: UUID,user: UUID,run: UUID) -> dict[str,Any]:
        with self.pool.transaction(tenant,user) as conn:
            row=conn.execute(self._select()+" WHERE id=%s",(run,)).fetchone()
        if row is None:
            raise LookupError("组合回测不存在")
        return self._row(row)

    def list(self,tenant: UUID,user: UUID,*,limit: int=20,offset: int=0) -> dict[str,Any]:
        with self.pool.transaction(tenant,user) as conn:
            count=conn.execute("SELECT count(*) FROM research_portfolios").fetchone()[0]
            rows=conn.execute(self._select(include_report=False)+" ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",(limit+1,offset)).fetchall()
        return {"items":[{**self._row(row),"report":None} for row in rows[:limit]],"total":count,
            "has_more":len(rows)>limit,"next_offset":offset+limit if len(rows)>limit else None}

    def _cases(self,tenant: UUID,user: UUID,manifest: list[dict]) -> list[dict]:
        result=[]
        prefix_inputs=0
        for source in manifest:
            source_cases=[]
            after=""
            while True:
                with self.pool.transaction(tenant,user) as conn:
                    rows=conn.execute("""SELECT candidate_id,pattern,observed_time,selected,state,
                        case_json-'plan'-'cost_stress',case_json->'plan'->'policy',
                        case_json->'plan'->'side',case_json->'plan'->'stop_price'
                        FROM research_structure_cases WHERE run_id=%s AND candidate_id>%s
                        ORDER BY candidate_id LIMIT 256""",(source["run_id"],after)).fetchall()
                if not rows:
                    break
                for candidate,pattern,observed,selected,state,case,policy,side,stop in rows:
                    item={"source_run_id":source["run_id"],"instrument_id":source["instrument_id"],
                        "symbol":source["symbol"],"timeframe":source["timeframe"],"step_seconds":TIMEFRAME_SECONDS[source["timeframe"]],
                        "candidate_id":candidate,"pattern":pattern,"observed_time":observed,"selected":selected,"state":state,
                        "side":side,"stop_price":stop,"fee_bps":policy["fee_bps"],"funding_bps_per_8h":policy["funding_bps_per_8h"],
                        "slippage_bps":policy.get("slippage_bps",source["spec"].get("slippage_bps",5.)),
                        "max_holding_bars":policy.get("max_holding_bars",source["spec"].get("max_holding_bars",48)),
                        **{key:case.get(key) for key in ("entry_time","exit_time","entry_price","raw_entry_price","exit_price",
                            "net_return","available_at","eligible_at","outcome_time","ambiguous")}}
                    # Keep execution and knowledge clocks distinct. Source cases
                    # freeze execution timestamps for costs, not the later time
                    # at which a delayed CLOSED candle revealed that execution.
                    item["exit_available_at"]=item["exit_time"]
                    item["outcome_available_at"]=item["outcome_time"]
                    if source["spec"].get("availability_mode")!="HISTORICAL_CLOSE":
                        item["exit_available_at"]=None
                        outcome=item["outcome_time"]
                        item["outcome_available_at"]=None
                        last=case.get("last_bar_time")
                        if outcome is not None:
                            if item["exit_time"] is not None:
                                item["_resolving_bar"]=last
                            elif last is None and outcome==item["available_at"]:
                                # Plan rejected immediately on issue, without a
                                # future execution candle (e.g. invalid levels).
                                item["outcome_available_at"]=outcome
                            else:
                                # Non-trade endings are observed at candle close;
                                # gap censoring does not update last_bar_time.
                                item["_resolving_bar"]=outcome-item["step_seconds"]
                    result.append(item)
                    source_cases.append(item)
                after=rows[-1][0]
                if len(result)>MAX_CASES:
                    raise ValueError("来源案例超过 20000 条，请选择更短的来源研究或减少来源")
            needed=sorted({row["_resolving_bar"] for row in source_cases if isinstance(row.get("_resolving_bar"),int)})
            if needed:
                with self.pool.transaction(tenant,user) as conn:
                    conn.execute("SET LOCAL statement_timeout='8s'")
                    # At most one bounded prefix aggregation per source. An old
                    # late-arriving candle can delay the complete source prefix,
                    # so looking up just the final bar is not sufficient.
                    count=conn.execute("""SELECT count(*) FROM (SELECT 1 FROM research_structure_inputs
                        WHERE run_id=%s AND open_time<=%s LIMIT %s) bounded""",
                        (source["run_id"],needed[-1],MAX_INPUTS-prefix_inputs+1)).fetchone()[0]
                    prefix_inputs+=count
                    if prefix_inputs>MAX_INPUTS:
                        raise ValueError("组合行情超过 1000000 根预算，请缩短区间或减少来源")
                    clocks=conn.execute("""WITH timeline AS (
                        SELECT open_time,max(greatest(open_time+%s,
                            coalesce((bar_json->>'available_at')::bigint,open_time+%s))) OVER(
                                ORDER BY bar_index ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS known_at
                        FROM research_structure_inputs WHERE run_id=%s AND open_time<=%s)
                        SELECT open_time,known_at FROM timeline WHERE open_time=ANY(%s)""",
                        (TIMEFRAME_SECONDS[source["timeframe"]],TIMEFRAME_SECONDS[source["timeframe"]],source["run_id"],needed[-1],needed)).fetchall()
                by_open=dict(clocks)
                for item in source_cases:
                    known=by_open.get(item.pop("_resolving_bar",None))
                    if known is not None:
                        item["outcome_available_at"]=max(known,item["outcome_time"],item.get("available_at") or 0)
                        if item["exit_time"] is not None:
                            item["exit_available_at"]=max(item["exit_time"],item["outcome_available_at"])
            for item in source_cases:
                item.pop("_resolving_bar",None)
        return result

    def _bar_stream(self,tenant: UUID,user: UUID,source: dict,*,start: int,end: int) -> Iterator[dict]:
        after=-1
        watermark=0
        while True:
            with self.pool.transaction(tenant,user) as conn:
                rows=conn.execute("""SELECT bar_index,bar_json FROM research_structure_inputs
                    WHERE run_id=%s AND bar_index>%s AND open_time>=%s AND open_time<%s
                    ORDER BY bar_index LIMIT 512""",(source["run_id"],after,start,end)).fetchall()
            if not rows:
                return
            for _index,bar in rows:
                timestamp=bar["ts"]+TIMEFRAME_SECONDS[source["timeframe"]]
                if source["spec"].get("availability_mode")!="HISTORICAL_CLOSE":
                    timestamp=max(timestamp,bar.get("available_at",timestamp))
                watermark=max(watermark,timestamp)
                yield {"source_run_id":source["run_id"],"mark_time":watermark,
                    "close_time":bar["ts"]+TIMEFRAME_SECONDS[source["timeframe"]],"close":bar["close"]}
            after=rows[-1][0]

    def _progress(self,tenant: UUID,user: UUID,run: UUID,phase: str,done: int,total: int) -> None:
        with self.pool.transaction(tenant,user) as conn:
            conn.execute("UPDATE research_portfolios SET progress_json=%s,lease_until=now()+interval '5 minutes' WHERE id=%s AND state='RUNNING'",
                (Jsonb({"phase":phase,"processed":done,"total":total,"engine_version":VERSION}),run))

    def run_one(self,*,progress: Callable[[int,int],None]|None=None) -> bool:
        with self.pool.public_transaction() as conn:
            claimed=conn.execute("SELECT * FROM gatescope_security.claim_research_portfolio()").fetchone()
        if not claimed:
            return False
        run,tenant,user=claimed
        try:
            request=self.get(tenant,user,run)
            if request["progress"].get("engine_version") not in (None,VERSION):
                raise ValueError("组合引擎已更新，请保留原记录并新建回测")
            self._progress(tenant,user,run,"READING_SOURCES",0,MAX_INPUTS)
            cases=self._cases(tenant,user,request["source_manifest"])
            start,end=int(request["from_time"].timestamp()),int(request["to_time"].timestamp())
            streams=[self._bar_stream(tenant,user,source,start=start,end=end) for source in request["source_manifest"]]
            bars=heapq.merge(*streams,key=lambda bar:bar["mark_time"])
            last_heartbeat=0.0
            phase="ALLOCATING"

            def notify(done: int,total: int) -> None:
                nonlocal last_heartbeat
                now=time.monotonic()
                if now-last_heartbeat>=5:
                    self._progress(tenant,user,run,phase,done,total)
                    if progress:
                        progress(done,total)
                    last_heartbeat=now

            with ResearchSpool() as spool:
                report=run_portfolio(cases,bars,PortfolioPolicy.model_validate(request["policy"]),start=start,end=end,
                    sequence_factory=spool.sequence,progress=notify)
                phase="SAVING_EVIDENCE"
                self._progress(tenant,user,run,phase,report["input_bar_count"],report["input_bar_count"])
                for kind in ("trades","decisions","curve"):
                    for index,count,payload,digest in spool.sequences[kind].chunks():
                        with self.pool.transaction(tenant,user) as conn:
                            conn.execute("""INSERT INTO research_portfolio_chunks(run_id,tenant_id,owner_user_id,kind,chunk_index,row_count,payload,digest)
                                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",(run,tenant,user,kind,index,count,payload,digest))
                            stored=conn.execute("SELECT row_count,digest FROM research_portfolio_chunks WHERE run_id=%s AND kind=%s AND chunk_index=%s",(run,kind,index)).fetchone()
                            if stored!=(count,digest):
                                raise ValueError("组合冻结证据不一致，请保留原记录并新建回测")
                        notify(report["input_bar_count"],report["input_bar_count"])
                with self.pool.transaction(tenant,user) as conn:
                    chunks=conn.execute("SELECT kind,chunk_index,row_count,digest FROM research_portfolio_chunks WHERE run_id=%s ORDER BY kind,chunk_index",(run,)).fetchall()
                report.update(source_manifest=request["source_manifest"],details_url=f"/api/v2/research/portfolios/{run}/details",
                    chunk_manifest=[dict(zip(("kind","chunk_index","row_count","digest"),row,strict=True)) for row in chunks])
                report["artifact_digest"]=report_digest(report)
                with self.pool.transaction(tenant,user) as conn:
                    conn.execute("""UPDATE research_portfolios SET state='SUCCEEDED',report_json=%s,completed_at=now(),
                        lease_until=NULL,progress_json=%s WHERE id=%s AND state='RUNNING'""",
                        (Jsonb(report),Jsonb({"phase":"COMPLETE","processed":report["input_bar_count"],"total":report["input_bar_count"]}),run))
        except Exception as exc:
            safe=str(exc) if isinstance(exc,ValueError) and str(exc) in {
                "组合行情超过 1000000 根预算，请缩短区间或减少来源","组合冻结行情可用时间不连续",
                "来源案例超过 20000 条，请选择更短的来源研究或减少来源","组合引擎已更新，请保留原记录并新建回测",
                "组合冻结证据不一致，请保留原记录并新建回测","组合退出价格证据无效",
            } else "组合回测失败，已保存的来源与证据保持不变，请新建回测"
            event("portfolio_research.failed",level=40,error_type=type(exc).__name__)
            with self.pool.transaction(tenant,user) as conn:
                conn.execute("UPDATE research_portfolios SET state='FAILED',safe_error=%s,completed_at=now(),lease_until=NULL WHERE id=%s AND state='RUNNING'",(safe,run))
        return True

    def page(self,tenant: UUID,user: UUID,run: UUID,*,kind: str,limit: int,offset: int) -> dict[str,Any]:
        if kind not in {"trades","decisions","curve"}:
            raise ValueError("未知组合证据类型")
        with self.pool.transaction(tenant,user) as conn:
            found=conn.execute("SELECT report_json->'detail_counts' FROM research_portfolios WHERE id=%s AND state='SUCCEEDED'",(run,)).fetchone()
            if not found:
                raise LookupError("组合报告不存在或尚未完成")
            total=int(found[0][kind])
            end=min(total,offset+limit)
            rows=conn.execute("SELECT chunk_index,payload,digest FROM research_portfolio_chunks WHERE run_id=%s AND kind=%s AND chunk_index>=%s AND chunk_index<=%s ORDER BY chunk_index",
                (run,kind,offset//CHUNK_ROWS,max(offset,end-1)//CHUNK_ROWS)).fetchall() if offset<total else []
        items=[]
        for index,payload,digest in rows:
            decoded=decode_chunk(payload,digest)
            items.extend(decoded[max(0,offset-index*CHUNK_ROWS):min(len(decoded),end-index*CHUNK_ROWS)])
        if len(items)!=max(0,end-offset):
            raise ValueError("组合证据分片缺失")
        return {"items":items,"offset":offset,"total":total,"has_more":end<total,"next_offset":end if end<total else None}
