import { useEffect, useRef, useState } from "react";
import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, type UTCTimestamp } from "lightweight-charts";
import { api } from "./api";
import { Dialog, PageState, Pagination } from "./ui";
import { attachPatternOverlays } from "./patternOverlays";
import { chartPriceFormat } from "./chartModel";
import { patternTime as date, phaseLabel, type PatternEvent, type PatternMeta, type Phase } from "./patternModel";
import type { Bar } from "./types";
import { StructureForecast } from "./StructureForecast";
import { StructureConfidence } from "./StructureConfidence";
import type { CompletionProbability } from "./patternModel";

type ArchiveItem = { context_id: string; id: string; phase: Phase; pattern: string; name: string; first_observed_time: number; observed_time: number; deadline_time: number; reason: string; sequence: number };
type ArchiveData = { items: ArchiveItem[]; total: number; offset: number; limit: number; contexts?: {id:string;created_at:string|null;bar_count:string|null;version?:string}[] };
type Evidence = { original_through: number; source_mode: string; bars: { ts: number; open: number; high: number; low: number; close: number; volume: number }[];
  transitions: { recorded_at: string; record: { phase: Phase; first: PatternEvent; latest: PatternEvent; observed_time: number; sequence: number; first_observed_time: number; deadline_time: number; stages: Record<string,{prediction:CompletionProbability}> } }[] };

function EvidenceChart({ evidence, index }: { evidence: Evidence; index: number }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(()=>{
    if (!host.current || !evidence.bars.length) return;
    const record = evidence.transitions[index].record;
    const event = record.latest;
    const chart = createChart(host.current,{ autoSize:true, layout:{background:{type:ColorType.Solid,color:"#101722"},textColor:"#99abc5"},
      grid:{vertLines:{color:"#1b2532"},horzLines:{color:"#1b2532"}}, timeScale:{timeVisible:true}, handleScroll:{vertTouchDrag:false} });
    const candles = chart.addSeries(CandlestickSeries,{upColor:"#2db69c",downColor:"#e47985",borderVisible:false,wickUpColor:"#2db69c",wickDownColor:"#e47985",priceFormat:chartPriceFormat(evidence.bars.map(b=>b.low))});
    candles.setData(evidence.bars.map(b=>({...b,time:b.ts as UTCTimestamp})));
    const bars = evidence.bars.map(b=>({...b,open_time:new Date(b.ts*1000).toISOString(),is_final:true})) as unknown as Bar[];
    const cleanup = attachPatternOverlays(chart,[event],bars);
    const step=evidence.bars.length>1 ? evidence.bars[1].ts-evidence.bars[0].ts : 1;
    const markTime=record.observed_time-step;
    const markers=createSeriesMarkers(candles,evidence.bars.some(b=>b.ts===markTime) ? [{time:markTime as UTCTimestamp,position:"aboveBar",shape:"arrowDown",color:"#91b9fa",text:`#${record.sequence} ${phaseLabel[record.phase]}`}] : []);
    const left=Math.max(0,evidence.bars.findIndex(b=>b.ts>=event.start_time)-8);
    chart.timeScale().setVisibleLogicalRange({from:left,to:evidence.bars.length+4});
    return ()=>{markers.detach();cleanup();chart.remove();};
  },[evidence,index]);
  return <div className="pattern-evidence-chart" ref={host} aria-label="候选原始证据与后续行情图"/>;
}

export function PatternEvidenceDialog({ context, candidate, onClose }: { context: string; candidate: string; onClose: () => void }) {
  const [data,setData]=useState<Evidence>(), [error,setError]=useState(""), [retry,setRetry]=useState(0), [index,setIndex]=useState(0);
  useEffect(()=>{const abort=new AbortController();setError("");
    api<Evidence>(`/api/v2/patterns/events/${encodeURIComponent(context)}/${encodeURIComponent(candidate)}`,{signal:abort.signal})
      .then(v=>{if(!abort.signal.aborted)setData(v)}).catch(e=>{if(!abort.signal.aborted)setError(e.message)});
    return ()=>abort.abort();},[context,candidate,retry]);
  const r=data?.transitions[index]?.record;
  return <Dialog title="候选档案 · 原始证据" onClose={onClose}><PageState loading={!data&&!error} error={error} onRetry={()=>setRetry(n=>n+1)}/>{data&&r&&<div className="pattern-detail">
    <div className="pattern-caption"><strong>{r.latest.name} · {phaseLabel[data.transitions[data.transitions.length-1].record.phase]}</strong><span>{data.source_mode==="REPLAY"?"历史逐根回放档案":"持续观测档案"}</span></div>
    <label className="pattern-evidence-select">事件时间线<select aria-label="候选事件时间线" value={index} onChange={e=>setIndex(+e.target.value)}>{data.transitions.map((t,i)=><option key={t.record.sequence} value={i}>#{t.record.sequence} {date(t.record.observed_time)} · {phaseLabel[t.record.phase]}</option>)}</select></label>
    <EvidenceChart evidence={data} index={index}/>
    <div className="pattern-caption">原始行情截止 {date(data.original_through)}；之后为后续走势，未替换原始 K 线版本。</div>
    <p>{r.latest.stage}</p><p>首次识别 {date(r.first_observed_time)} · 期限 {date(r.deadline_time)}</p>
    <StructureConfidence event={{...r.latest,initial_prediction:r.stages[r.first.phase]?.prediction}} frozen/>
    <StructureForecast event={r.latest}/>
    {!!r.latest.recognition&&<details><summary>几何规则检查</summary><p>{r.latest.recognition.checks.join(" · ")}</p>{r.latest.recognition.warnings.map(w=><p key={w}>{w}</p>)}</details>}
    <p>此节点实际落库 {new Date(data.transitions[index].recorded_at).toLocaleString()}</p>
    {Object.entries(r.stages).map(([stage,s])=><p key={stage}>{phaseLabel[stage as Phase]}首次判断时的突破确认估计：{s.prediction.probability==null ? "当时样本不足" : `${(s.prediction.probability*100).toFixed(1)}%`} · n={s.prediction.sample_count}</p>)}
    <p>实线：已知结构 · 虚线：暂定结构或确认参考 · 红点线：结构失效参考（收盘规则另加 ATR 缓冲）。</p>
    <small>快照摘要 {r.latest.evidence_id} · 初始判断与后续变化逐条保留</small>
  </div>}</Dialog>;
}

export function PatternArchive({ params, metas }: { params: string; metas: PatternMeta[] }) {
  const [source,setSource]=useState("RECORDED"), [phase,setPhase]=useState(""), [pattern,setPattern]=useState(""), [page,setPage]=useState(0);
  const [data,setData]=useState<ArchiveData>(), [loading,setLoading]=useState(true), [error,setError]=useState(""), [retry,setRetry]=useState(0);
  const [selected,setSelected]=useState<ArchiveItem>();
  const [context,setContext]=useState("");
  const request=`${params}&source=${source}&offset=${page*12}&limit=12${phase?`&phase=${phase}`:""}${pattern?`&pattern=${pattern}`:""}${context?`&context=${context}`:""}`;
  useEffect(()=>{const abort=new AbortController();setLoading(true);setError("");
    api<ArchiveData>(`/api/v2/patterns/events?${request}`,{signal:abort.signal}).then(v=>{if(!abort.signal.aborted)setData(v)})
      .catch(e=>{if(!abort.signal.aborted)setError(e.message)}).finally(()=>{if(!abort.signal.aborted)setLoading(false)});
    return ()=>abort.abort();},[request,retry]);
  return <section aria-label="候选档案"><div className="pattern-filters">
    <label>来源<select aria-label="档案来源" value={source} onChange={e=>{setSource(e.target.value);setPage(0);setContext("")}}><option value="RECORDED">持续观测</option><option value="REPLAY">历史回放</option></select></label>
    {(source==="REPLAY"||(data?.contexts?.length??0)>1)&&<label>{source==="REPLAY"?"回放批次":"模型档案"}<select aria-label="回放批次" value={context} onChange={e=>{setContext(e.target.value);setPage(0)}}><option value="">最新一批</option>{data?.contexts?.slice(1).map(c=><option key={c.id} value={c.id}>{c.version??(c.created_at?new Date(c.created_at).toLocaleString():"旧批次")}{c.bar_count?` · ${c.bar_count} 根`:""}</option>)}</select></label>}
    <label>状态<select aria-label="档案状态" value={phase} onChange={e=>{setPhase(e.target.value);setPage(0)}}><option value="">全部状态</option>{Object.entries(phaseLabel).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
    <label>形态<select aria-label="档案形态" value={pattern} onChange={e=>{setPattern(e.target.value);setPage(0)}}><option value="">全部形态</option>{metas.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
    <button disabled={loading} onClick={()=>setRetry(n=>n+1)}>{loading?"读取中…":"刷新档案"}</button></div>
    <PageState loading={loading} error={error} onRetry={()=>setRetry(n=>n+1)}/>
    {!loading&&!error&&<><div className="pattern-archive-list">{data?.items.map(item=><article key={item.id}><div><strong>{item.name}</strong><span className={`pattern-phase ${item.phase.toLowerCase()}`}>{phaseLabel[item.phase]}</span></div><p>{item.reason}</p><small>首次 {date(item.first_observed_time)} · 更新 {date(item.observed_time)}</small><button onClick={()=>setSelected(item)} aria-label={`查看 ${item.name} 候选档案`}>查看原始图与时间线</button></article>)}</div>{!data?.items.length&&<div className="empty">暂无该条件的档案。持续观测从功能启用后积累；历史候选在“历史表现”中启动回放。</div>}
    {!!data?.total&&<Pagination label="候选档案" page={page} pages={Math.ceil(data.total/12)} total={data.total} onChange={setPage}/>}</>}
    {selected&&<PatternEvidenceDialog context={selected.context_id} candidate={selected.id} onClose={()=>setSelected(undefined)}/>}
  </section>;
}
