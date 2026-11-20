import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Dialog, PageState, Pagination } from "./ui";
import { historyState } from "./marketCache";
import { patternPercent as pct } from "./patternModel";

export type HistoryProgress = { instrument_id:string; symbol:string; timeframe:string; cached_bars:number; target_bars:number; missing_bars:number|null; coverage_ratio:number; history_status:string; history_error_code?:string|null; state:string; cursor:number|null; total:number|null; error?:string|null; replay_error?:string|null; replay_retry_exhausted?:boolean; listing_time?:number|null; range_policy?:string; target_from?:string|null; target_to?:string|null; live_error_code?:string|null; last_live_at?:string|null; coverage_asof?:string|null; range_refresh_pending?:boolean };
const states:Record<string,string>={WAITING_HISTORY:"等待可用历史样本",QUEUED:"已排队",RUNNING:"逐根回放中",READY:"已完成本地样本回放",FAILED:"回放异常"};
const errors:Record<string,string>={HISTORY_DISK_LOW:"可用磁盘不足 2 GB，历史下载已暂停",ARCHIVE_NOT_PUBLISHED:"该月份官方历史文件尚未发布，保留缺口并定时重试",ARCHIVE_RANGE_EMPTY:"官方文件未覆盖该区间，保留缺口",REST_RANGE_EMPTY:"上游暂未返回该区间",MARKET_HISTORY_FAILED:"历史来源暂不可用，自动重试",MARKET_RATE_LIMITED:"上游限流，稍后重试",UNSUPPORTED_ARCHIVE_SYMBOL:"历史下载不支持此币种代码，已延长重试间隔",UNSUPPORTED_ARCHIVE_MARKET:"历史下载不支持此市场，已延长重试间隔"};
const replayErrors:Record<string,string>={PATTERN_HISTORY_SHORT:"已收盘 K 线不足 60 根，等待更多样本",PATTERN_HISTORY_GAP:"历史有缺口，等待补齐",PATTERN_VERSION_CHANGED:"模型已更新，等待重新回放",PATTERN_STUDY_TOO_DENSE:"本次回放超出计算预算",PATTERN_CALCULATION_FAILED:"样本计算异常"};
export type ReplayControl={state:string;checked_at:string|null};
const pauseReasons:Record<string,string>={PAUSED_LOW_DISK:"磁盘空间不足",PAUSED_DISABLED:"维护暂停",PAUSED_STORAGE_UNAVAILABLE:"容量检查暂不可用"};

export function ReplayStorageNotice({control}:{control?:ReplayControl}) {
  const reason=control&&pauseReasons[control.state];
  if(!reason)return null;
  return <p role="status">历史回放已暂停：{reason}。已有结果和计算进度保留；实时行情状态见下方。</p>;
}

export function AnnualHistoryRow({row:r,control}:{row:HistoryProgress;control?:ReplayControl}) {
  const clipped=r.range_policy==="LISTING_CLIPPED";
  const replayError=r.replay_error??r.error;
  const liveAt=r.last_live_at?new Date(r.last_live_at):undefined;
  const liveKnown=liveAt!=null&&Number.isFinite(liveAt.getTime());
  const liveDelayed=liveKnown&&Date.now()-liveAt!.getTime()>60000;
  const pauseReason=control&&pauseReasons[control.state];
  const paused=!!pauseReason&&(r.state==="RUNNING"||r.state==="QUEUED");
  const uncertain=control?.state==="UNKNOWN"&&(r.state==="RUNNING"||r.state==="QUEUED");
  const coverage=r.coverage_ratio>0&&r.coverage_ratio<1?`${(Math.floor(r.coverage_ratio*10000)/100).toFixed(2)}%`:pct(r.coverage_ratio);
  return <article aria-label={`${r.symbol} ${r.timeframe} 同步状态`}>
    <header><strong>{r.symbol} · {r.timeframe}</strong><span>{coverage}</span></header>
    <small>{clipped?`自上市起 · ${r.listing_time?new Date(r.listing_time*1000).toLocaleDateString("zh-CN"):"有效历史"}`:"过去一年"}{clipped?" · 上市前不计缺失":""}</small>
    <progress aria-label={`${r.symbol} ${r.timeframe} 有效历史覆盖率`} value={Math.min(r.cached_bars,r.target_bars)} max={Math.max(1,r.target_bars)}/>
    <div><span>{r.cached_bars.toLocaleString()} / {r.target_bars.toLocaleString()} 根</span><small>{historyState[r.history_status]??"等待同步"}</small></div>
    {r.coverage_asof&&<small>覆盖统计截至 {new Date(r.coverage_asof).toLocaleString("zh-CN")}</small>}
    {r.range_refresh_pending&&<small>历史范围更新中，暂保留上次完整统计。</small>}
    <div><span>实时行情 · {r.live_error_code||liveDelayed?"更新延迟":liveKnown?"更新正常":"等待采集"}</span>{liveKnown&&<small>更新于 {liveAt!.toLocaleTimeString("zh-CN")}</small>}</div>
    <div><span>样本回放 · {paused?`已暂停（${pauseReason}）`:uncertain?"当前状态待确认，保留上次进度":states[r.state]??r.state}</span>{r.total!=null&&r.total>0&&<small>{(r.cursor??0).toLocaleString()} / {r.total.toLocaleString()}</small>}</div>
    {r.state==="RUNNING"&&<progress aria-label={`${r.symbol} ${r.timeframe} ${paused||uncertain?"已保留回放进度":"回放进度"}`} value={r.cursor??0} max={r.total||1}/>}
    {r.history_error_code&&<small>{errors[r.history_error_code]??"部分历史尚待补齐"}</small>}
    {replayError&&<small>{replayErrors[replayError]??"样本回放暂不可用"}{r.replay_retry_exhausted?" · 自动重试已达上限，可在历史表现中重新提交":""}</small>}
    {r.coverage_ratio<1&&r.state==="READY"&&<small>已回放现有缓存；新增历史将在后台补充回放。</small>}
  </article>;
}

export function AnnualHistory({onClose}:{onClose:()=>void}) {
  const [page,setPage]=useState(0),[data,setData]=useState<{items:HistoryProgress[];total:number;replay_control?:ReplayControl}>(),[error,setError]=useState(""),[retry,setRetry]=useState(0),[loading,setLoading]=useState(true);
  const list=useRef<HTMLDivElement>(null);
  useEffect(()=>{list.current?.closest("dialog")?.scrollTo({top:0});},[page]);
  useEffect(()=>{const abort=new AbortController();let busy=false;setLoading(true);
    async function load(){if(busy||document.hidden)return;busy=true;
      try{const value=await api<{items:HistoryProgress[];total:number;replay_control?:ReplayControl}>(`/api/v2/patterns/watchlist-progress?offset=${page*24}&limit=24`,{signal:abort.signal});
        if(!abort.signal.aborted){setData(value);setError("");if(page>0&&!value.items.length)setPage(p=>p-1);}}
      catch(e){if(!abort.signal.aborted)setError(e instanceof Error?e.message:"同步进度读取失败");}
      finally{busy=false;if(!abort.signal.aborted)setLoading(false);}}
    void load();const timer=window.setInterval(()=>void load(),10000);
    window.addEventListener("online",load);document.addEventListener("visibilitychange",load);
    return()=>{abort.abort();window.clearInterval(timer);window.removeEventListener("online",load);document.removeEventListener("visibilitychange",load);};
  },[page,retry]);
  return <Dialog title="自选历史 · 同步与回放" onClose={onClose}>
    <p>自选自动同步 8 个周期，默认过去一年；新合约按上市后的有效范围计算。关闭页面不影响下载和回放。</p>
    <PageState loading={loading} error={error} onRetry={()=>setRetry(n=>n+1)}/>
    {!loading&&!error&&<ReplayStorageNotice control={data?.replay_control}/>}
    <div className="annual-history-list" ref={list}>{data?.items.map(r=><AnnualHistoryRow key={`${r.instrument_id}:${r.timeframe}`} row={r} control={!loading&&!error?data?.replay_control:{state:"UNKNOWN",checked_at:null}}/>)}</div>
    {!loading&&!data?.items.length&&!error&&<div className="empty">添加自选后，这里会显示各周期的同步和回放进度。</div>}
    <Pagination label="自选历史同步" page={page} pages={Math.max(1,Math.ceil((data?.total??0)/24))} total={data?.total} busy={loading} onChange={setPage}/>
    <small>已有足够缓存会先回放，补齐更早历史后重新验证；1 分钟周期首次同步耗时较长。缺口单独保留，不生成虚假 K 线。</small>
  </Dialog>;
}
