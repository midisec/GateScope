import { useEffect, useRef, useState } from "react";
import { api, jsonBody } from "./api";
import { Dialog, PageState, PagedList, Pagination, pageSlice } from "./ui";
import { overlayColors, overlayGeometry, patternPercent as pct, patternTime as date, sampleCountLabel as count, studySampleSummary, phaseLabel, type PatternCurrent, type PatternEvent, type PatternStudy, type Phase, type StudyRow } from "./patternModel";
import type { Bar, Instrument } from "./types";
import "./patterns.css";
import { CompletionStats } from "./CompletionStats";
import { PatternArchive, PatternEvidenceDialog } from "./PatternArchive";
import { AnnualHistory } from "./AnnualHistory";
import { StructureForecast } from "./StructureForecast";
import { StructureConfidence } from "./StructureConfidence";
import { PatternConfidenceFilter, confidenceLabel, confidenceParams, type ConfidenceFilter } from "./PatternConfidenceFilter";
import { PatternScreeningSummary } from "./PatternScreeningSummary";
import { PatternEventDetail } from "./PatternEventDetail";

const studyErrors: Record<string, string> = { PATTERN_HISTORY_GAP: "历史 K 线有缺口，请等待缓存补齐再更新", PATTERN_HISTORY_SHORT: "本地不足 60 根已收盘 K 线，请等待同步",
  PATTERN_STUDY_TOO_DENSE: "形态过密，超出本次研究预算", PATTERN_VERSION_CHANGED: "模型已升级，请重新更新统计", PATTERN_CALCULATION_FAILED: "计算失败，请稍后重新更新" };

export function PatternStudySummary({ study, cohort, horizon }: { study?: PatternStudy; cohort: Phase; horizon: number }) {
  const summary = studySampleSummary(study?.report, cohort, horizon);
  const label = study?.report?.partial ? "部分完成" : study?.state === "READY" && study.report ? "本地回放已完成" : study?.state === "RUNNING" ? "后台回放中" : study?.state === "FAILED" ? "回放待恢复" : study?.report ? "上次统计结果" : "等待历史回放";
  return <div className="pattern-sample-summary" aria-label="历史样本概览"><div className="pattern-sample-summary-heading"><strong>{label}</strong><small>{phaseLabel[cohort]} · {horizon} 根观察期</small></div><dl><div><dt>识别总量</dt><dd>{count(summary.detected)}</dd></div><div><dt>有效收益样本</dt><dd>{count(summary.effective)}</dd></div><div><dt>重叠排除</dt><dd>{count(summary.overlap)}</dd></div><div><dt>等待走完</dt><dd>{count(summary.pending)}</dd></div></dl></div>;
}

export function ReturnStudyRow({ row, name, group, partial = false, onDetail }: { row?: StudyRow; name: string; group: string; partial?: boolean; onDetail: (row: StudyRow) => void }) {
  if (!row) return <tr><th>{name}<small>{group}</small></th><td colSpan={7} className="pattern-sample-empty">— · {partial ? "回放进行中，尚未生成此组统计" : "此组暂无统计"}</td></tr>;
  return <tr><th>{name}<small>{group}</small></th><td>{count(row.sample_count)} / {count(row.detected)}</td><td>{count(row.overlap_excluded)} / {count(row.pending)}</td><td>{pct(row.win_rate)}{row.sample_count === 0 && <small>{partial ? "回放尚未完成" : "无有效收益样本"}</small>}</td><td>{row.interval ? `${pct(row.interval.lower)}–${pct(row.interval.upper)}` : row.sample_count > 0 && row.sample_count < 20 ? "样本不足 20" : "—"}</td><td className={row.mean_net_return == null ? undefined : row.mean_net_return >= 0 ? "positive" : "negative"}>{pct(row.mean_net_return)}</td><td>{pct(row.mean_mfe)} / {pct(row.mean_mae)}</td><td><button onClick={() => onDetail(row)} aria-label={`查看 ${name} 统计明细`}>查看</button></td></tr>;
}

export function PatternPanel({ instrument, timeframe, bars, onSelection, onCoinglass }: { instrument: Instrument; timeframe: string; bars: Bar[]; onSelection: (items: PatternEvent[]) => void; onCoinglass?: (event:PatternEvent)=>void }) {
  const [current, setCurrent] = useState<PatternCurrent>(), [currentError, setCurrentError] = useState(""), [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("current"), [family, setFamily] = useState("all"), [phase, setPhase] = useState("all"), [query, setQuery] = useState("");
  const [selected, setSelected] = useState<PatternEvent[]>([]), [message, setMessage] = useState("");
  const [study, setStudy] = useState<PatternStudy>(), [studyError, setStudyError] = useState(""), [studyLoading, setStudyLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false), [retry, setRetry] = useState(0), [cohort, setCohort] = useState<Phase>("CONFIRMED"), [horizon, setHorizon] = useState(20);
  const [detail, setDetail] = useState<StudyRow>();
  const [statsPage, setStatsPage] = useState(0);
  const [annualOpen,setAnnualOpen]=useState(false);
  const [currentPage,setCurrentPage]=useState(0), [search,setSearch]=useState("");
  const [evidence,setEvidence]=useState<PatternEvent>();
  const [confidence,setConfidence]=useState<ConfidenceFilter>({}), [pattern,setPattern]=useState("");
  const [screening,setScreening]=useState<{current:PatternCurrent;label:string}>(), [eventDetail,setEventDetail]=useState<PatternEvent>();
  useEffect(()=>{const timer=window.setTimeout(()=>setSearch(query.trim().slice(0,80)),250);return ()=>window.clearTimeout(timer)},[query]);
  useEffect(()=>setCurrentPage(0),[family,phase,search,confidence,pattern]);
  useEffect(() => setStatsPage(0), [cohort, horizon, family, query]);
  const submitLock = useRef(false), mounted = useRef(true), studyGeneration = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const params = `instrument_id=${encodeURIComponent(instrument.id)}&timeframe=${encodeURIComponent(timeframe)}`;
  const currentParams = `${params}&limit=12&offset=${currentPage*12}${family!=="all"?`&family=${family}`:""}${phase!=="all"?`&phase=${phase}`:""}&q=${encodeURIComponent(search)}&${confidenceParams(confidence)}${pattern?`&pattern=${encodeURIComponent(pattern)}`:""}`;
  useEffect(() => {
    const abort = new AbortController(); let busy = false;
    setLoading(true);
    async function load() {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const value = await api<PatternCurrent>(`/api/v2/patterns/current?${currentParams}`, { signal: abort.signal });
        if (!abort.signal.aborted) { setCurrent(value); setCurrentError(""); if(value.offset>=value.total)setCurrentPage(Math.max(0,Math.ceil(value.total/12)-1)); }
      } catch (e) { if (!abort.signal.aborted) setCurrentError(e instanceof Error ? e.message : "结构读取失败"); }
      finally { busy = false; if (!abort.signal.aborted) setLoading(false); }
    }
    void load(); const timer = window.setInterval(() => void load(), 30000);
    window.addEventListener("online", load); document.addEventListener("visibilitychange", load);
    return () => { abort.abort(); window.clearInterval(timer); window.removeEventListener("online", load); document.removeEventListener("visibilitychange", load); };
  }, [currentParams, retry]);
  useEffect(() => {
    if (tab !== "history") return;
    const abort = new AbortController(); let busy = false;
    setStudyLoading(true);
    async function load() {
      if (busy || document.hidden || submitLock.current) return;
      busy = true; const generation = studyGeneration.current;
      try {
        const value = await api<PatternStudy>(`/api/v2/patterns/study?${params}`, { signal: abort.signal });
        if (!abort.signal.aborted && generation === studyGeneration.current) { setStudy(value); setStudyError(""); }
      } catch (e) { if (!abort.signal.aborted && generation === studyGeneration.current) setStudyError(e instanceof Error ? e.message : "历史统计读取失败"); }
      finally { busy = false; if (!abort.signal.aborted) setStudyLoading(false); }
    }
    void load(); const timer = window.setInterval(() => void load(), 4000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, [params, tab, retry]);
  async function update() {
    if (submitLock.current) return;
    submitLock.current = true; studyGeneration.current += 1; setSubmitting(true); setStudyError("");
    try {
      const value = await api<PatternStudy>("/api/v2/patterns/study", { method: "POST", body: jsonBody({ instrument_id: instrument.id, timeframe }) });
      if (mounted.current) setStudy(value);
    } catch (e) { if (mounted.current) setStudyError(e instanceof Error ? e.message : "提交失败"); }
    finally { submitLock.current = false; if (mounted.current) setSubmitting(false); }
  }
  function choose(event: PatternEvent) {
    setMessage("");
    const exists = selected.some(e => e.id === event.id);
    if (!exists && selected.length >= 3) { setMessage("最多同时叠加 3 个结构，请先取消一个"); return; }
    const next = exists ? selected.filter(e => e.id !== event.id) : [...selected, event];
    setSelected(next); onSelection(next);
  }
  function applyConfidence(value: ConfidenceFilter) { setCurrentPage(0); setConfidence(value); setRetry(value=>value+1); }
  function resetFilters() { setCurrentPage(0); setConfidence({}); setFamily("all"); setPhase("all"); setPattern(""); setQuery(""); setSearch(""); setRetry(value=>value+1); }
  const filterLabel = [confidenceLabel(confidence),family==="chart"?"图表形态":family==="wave"?"波浪理论":"全部类别",phase==="all"?"全部状态":phaseLabel[phase as Phase],search?`搜索：${search}`:"",pattern?`类型：${current?.catalog.find(meta=>meta.id===pattern)?.name??pattern}`:""].filter(Boolean).join(" · ");
  const needle = query.trim().toLocaleLowerCase();
  const events = loading || currentError ? [] : current?.items ?? [];
  const metas = ((tab === "history" ? study?.report?.catalog : undefined) ?? current?.catalog ?? []).filter(m => (family === "all" || m.family === family) && `${m.name} ${m.group}`.toLocaleLowerCase().includes(needle));
  const stats = pageSlice(metas, statsPage, 8);
  const report = study?.report;
  const active = study?.state === "QUEUED" || study?.state === "RUNNING";
  const cooldown = study?.requested_at ? Math.max(0, Math.ceil((Date.parse(study.requested_at) + 300000 - Date.now()) / 1000)) : 0;
  return <section className="pattern-panel panel" aria-label="形态与波浪"><header><div><h2>形态与波浪</h2><span>{instrument.symbol} · {timeframe}</span></div><div className="pattern-tabs" role="tablist" aria-label="形态面板视图"><button role="tab" aria-selected={tab === "current"} onClick={() => setTab("current")}>当前结构</button><button role="tab" aria-selected={tab === "history"} onClick={() => setTab("history")}>历史表现</button><button role="tab" aria-selected={tab === "archive"} onClick={() => setTab("archive")}>候选档案</button></div></header>
    <div className="pattern-study-actions"><span>自选全部周期 · 默认过去一年</span><button onClick={()=>setAnnualOpen(true)}>自选历史同步进度</button></div>
    {tab === "current" && current?.history_status && <div className="pattern-caption"><span>{({ READY: "历史样本已完成回放", RUNNING: "历史样本回放中", QUEUED: "历史样本等待回放", NOT_REQUESTED: "历史样本等待回放", WAITING_HISTORY: "等待历史缓存", FAILED: "历史回放待恢复" }[current.history_status.state] ?? "历史样本同步中")}{current.history_status.total != null && current.history_status.total > 0 ? ` · ${count(current.history_status.cursor)} / ${count(current.history_status.total)} 根` : ""}</span>{current.history_status.through_time != null && <span>已回放至 {date(current.history_status.through_time)}</span>}</div>}
    {annualOpen&&<AnnualHistory onClose={()=>setAnnualOpen(false)}/>}
    {screening?.current.summary && <PatternScreeningSummary summary={screening.current.summary} params={params} asOf={screening.current.as_of} filterLabel={screening.label} version={screening.current.version} onClose={()=>setScreening(undefined)} onPattern={id=>{setCurrentPage(0);setPattern(id);setScreening(undefined);}} />}
    {eventDetail && <PatternEventDetail event={eventDetail} params={params} version={current?.version??""} canOverlay={selected.some(item=>item.id===eventDetail.id)||(selected.length<3&&overlayGeometry(eventDetail,bars).lines.length>0)} overlayLabel={selected.some(item=>item.id===eventDetail.id)?"取消图表叠加":selected.length>=3?"叠加已满 3 个":overlayGeometry(eventDetail,bars).lines.length>0?"在图表显示原理线":"原理线不在当前图表范围"} onOverlay={()=>{choose(eventDetail);setEventDetail(undefined);}} onEvidence={eventDetail.context_id?()=>{setEvidence(eventDetail);setEventDetail(undefined);}:undefined} onCoinglass={onCoinglass&&eventDetail.phase==="FORMING"?()=>{onCoinglass(eventDetail);setEventDetail(undefined);}:undefined} onClose={()=>setEventDetail(undefined)} />}
    {tab === "history" && !report && <PatternStudySummary study={study} cohort={cohort} horizon={horizon} />}
    {evidence?.context_id && <PatternEvidenceDialog context={evidence.context_id} candidate={evidence.id} onClose={()=>setEvidence(undefined)}/>}
    <div className="pattern-filters"><label className="pattern-search"><span>查找</span><input placeholder="搜索形态或波浪…" aria-label="搜索形态或波浪" value={query} onChange={e => setQuery(e.target.value)} /></label><label><span>类别</span><select aria-label="形态类别" value={family} onChange={e => setFamily(e.target.value)}><option value="all">全部类别</option><option value="chart">图表形态</option><option value="wave">波浪理论</option></select></label>{tab === "current" ? <label><span>状态</span><select aria-label="结构状态" value={phase} onChange={e => setPhase(e.target.value)}><option value="all">全部状态</option>{Object.entries(phaseLabel).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label> : tab === "history" ? <><label><span>收益样本组</span><select aria-label="统计样本组" value={cohort} onChange={e => setCohort(e.target.value as Phase)}><option value="CONFIRMED">已确认</option><option value="FORMED">已形成（含已确认）</option></select></label><label><span>观察期</span><select aria-label="统计观察期" value={horizon} onChange={e => setHorizon(+e.target.value)}>{[5, 10, 20, 50].map(h => <option key={h} value={h}>{h} 根 K 线</option>)}</select></label></> : null}</div>
    {tab === "current" && <><PatternConfidenceFilter value={confidence} busy={loading} onApply={applyConfidence} onReset={resetFilters} /><div className="pattern-screening-actions"><div><strong aria-live="polite">{loading?"正在筛选…":currentError?"筛选暂不可用":`匹配 ${count(current?.total)} 个结构`}</strong>{pattern&&<button type="button" aria-label="清除形态类型筛选" onClick={()=>{setCurrentPage(0);setPattern("");}}>{current?.catalog.find(meta=>meta.id===pattern)?.name??pattern} ×</button>}</div><button type="button" disabled={loading||!!currentError||!current?.summary} onClick={()=>current&&setScreening({current,label:filterLabel})}>筛选结果详情</button></div></>}
    {!!selected.length && <div className="pattern-selection" aria-label="已选结构"><small>叠加 {selected.length}/3 · 选择时快照</small>{selected.map((e, i) => <button key={e.id} style={{ borderColor: overlayColors[i] }} onClick={() => choose(e)} aria-label={`取消叠加 ${e.name}`}>{e.name} ×</button>)}<button onClick={() => document.querySelector(".market-workspace .chart-panel")?.scrollIntoView({ behavior: "smooth", block: "center" })}>查看图表 ↑</button><button onClick={() => { setSelected([]); onSelection([]); setMessage(""); }}>清空叠加</button></div>}
    {message && <div className="pattern-message" role="status">{message}</div>}
    {selected.map(e=><details className="pattern-method" key={e.id}><summary>{e.name} · 预测点位与形成条件</summary><StructureForecast event={e}/></details>)}
    {selected.filter(e => e.initial_prediction != null).map(e => <details className="pattern-method" key={`initial-${e.id}`}><summary>{e.name} · 首次判断原始样本</summary><StructureConfidence event={e} frozen /></details>)}
    {selected.filter(e=>e.historical_reference?.failure_examples?.length).map(e=><details className="pattern-method" key={`history-${e.id}`}><summary>{e.name} · 历史失败案例</summary><p>以下案例已发生在本次判断之前，已计入同类历史确认率。</p>{e.historical_reference!.failure_examples!.map(f=><button key={`${f.context_id}:${f.id}`} onClick={()=>setEvidence({...e,id:f.id,context_id:f.context_id})}>{f.source==="RECORDED"?"实测":"回放"} · {phaseLabel[f.phase]} · {date(f.observed_time)} · 查看原图</button>)}</details>)}
    {tab === "archive" ? <PatternArchive params={params} metas={metas} /> : tab === "current" ? <><PageState loading={loading} error={currentError} onRetry={() => setRetry(n => n + 1)} /><div className="pattern-caption"><span>最近 {current?.bars_scanned ?? "—"} 根已收盘 K 线 · 筛选 {current?.total ?? 0} / 跟踪 {current?.tracked_total ?? 0} 个结构</span><time>截至 {date(current?.as_of)}</time></div>{current?.tracking_status && current.tracking_status !== "READY" && <div className="pattern-caption" role="status">{current.tracking_status === "QUEUED" ? "候选档案同步中，后台将持续跟踪已收盘 K 线" : (studyErrors[current.tracking_status] ?? "结构跟踪暂有异常，请刷新或查看候选档案")}</div>}<div className="pattern-cards"><PagedList key={`${family}:${phase}:${query}`} label="结构列表" size={12} items={events} empty={loading ? "正在识别结构…" : currentError ? "结构暂不可用" : "当前条件下暂无匹配结构"} render={e => {
      const index = selected.findIndex(s => s.id === e.id), geometry = overlayGeometry(e, bars), available = geometry.lines.length > 0;
      return <article className={`pattern-card ${index >= 0 ? "is-selected" : ""}`} key={e.id} aria-label={`${e.name} ${phaseLabel[e.phase]}`}><div><strong>{e.name}</strong><span className={`pattern-phase ${e.phase.toLowerCase()}`}>{phaseLabel[e.phase]}</span></div><p>{e.stage}</p><div className="pattern-card-meta"><span>{e.signal_direction === "bullish" ? "观察向上" : e.signal_direction === "bearish" ? "观察向下" : "方向待定"}</span><span>{e.quality==="LOW_FIT"?"低匹配 · 保留候选":""}</span></div><StructureConfidence event={e}/><StructureForecast event={e} compact/><footer><time>{date(e.first_observed_time ?? e.end_time)}</time><div className="pattern-card-actions"><button type="button" aria-label={`查看 ${e.name} 结构详情`} onClick={()=>setEventDetail(e)}>查看详情</button><button type="button" aria-pressed={index >= 0} aria-label={`叠加 ${e.name} ${phaseLabel[e.phase]}`} disabled={!available&&!e.context_id&&index<0} onClick={()=>available||index>=0?choose(e):setEvidence(e)}>{index >= 0 ? "✓ 已叠加" : available ? (geometry.status==="CLIPPED" ? "+ 局部原理线" : "+ 显示原理线") : "查看原始图"}</button>{onCoinglass&&e.phase==="FORMING"&&<button type="button" onClick={()=>onCoinglass(e)}>Coinglass 数据支撑</button>}</div></footer></article>;
    }} /></div>{!!current?.total && <Pagination label="当前结构" page={currentPage} pages={Math.ceil(current.total/12)} total={current.total} busy={loading} onChange={setCurrentPage}/>}<div className="pattern-caption">跟踪数为当前窗口保留的候选，不是历史总量；全部记录见候选档案。</div></> : <><div className="pattern-study-actions"><span>{report ? `${report.bar_count.toLocaleString()} 根 · ${date(report.from_time)} — ${date(report.to_time)}` : "逐根回放过去一年的本地缓存，后台自动同步"}</span><button disabled={submitting || active || studyLoading || cooldown > 0} onClick={() => void update()}>{submitting ? "提交中…" : active ? "后台计算中…" : cooldown > 0 ? `${cooldown} 秒后可更新` : report ? "更新历史统计" : "计算历史表现"}</button></div><PageState loading={studyLoading && !study} error={studyError} onRetry={() => setRetry(n => n + 1)} />{active && <div className="pattern-progress" role="status"><span>{study.state === "QUEUED" ? "已排队 · 关闭页面不影响计算" : `逐根回放 ${study.cursor.toLocaleString()} / ${study.total.toLocaleString()} · 完成后自动展示`}</span><progress value={study.cursor} max={Math.max(1, study.total)} /></div>}{study?.state === "FAILED" && <PageState error={studyErrors[study.error ?? ""] ?? "历史计算失败，请重新更新"} />}{report && <>
      {(active || study?.state === "FAILED") && <div className="pattern-caption">{report.partial ? `阶段结果 · 已回放前 ${report.bar_count.toLocaleString()} 根，完整样本尚未完成` : "当前保留上次完成的统计结果"}</div>}
      <div className="pattern-caption"><span>报告模型 {report.version}{current?.version&&report.version!==current.version?" · 旧版报告，仅供查阅；新版正在重算，不用于当前概率":""}</span><span>{report.metadata.period_days===365?`${report.metadata.range_policy==="LISTING_CLIPPED"?"上市后有效范围覆盖":"一年覆盖"} ${pct(report.metadata.coverage_ratio)} · 缺 ${report.metadata.missing_bars?.toLocaleString()??"—"} 根 · ${report.metadata.mode==="INCREMENTAL"?"增量回放":"全量回放"}`:"此前版本的历史报告，等待一年回放更新"}</span></div>
      <PatternStudySummary study={study} cohort={cohort} horizon={horizon} />
      <CompletionStats rows={report.completion_rows} metas={metas} partial={!!report.partial} />
      {report.forecast_outcomes&&<div className="pattern-caption">首次目标验证（非重叠样本）：触及 {report.forecast_outcomes.TOUCHED??0} · 未触及 {report.forecast_outcomes.NOT_REACHED??0} · 待定 {report.forecast_outcomes.PENDING??0} · 同根先后不明 {report.forecast_outcomes.AMBIGUOUS??0} · 数据中断 {report.forecast_outcomes.CENSORED??0}。触价不是形态确认。</div>}
      <h3 className="pattern-subheading">方向收益 · 与形态确认率分开评估</h3><div className="pattern-stats" role="region" aria-label="形态历史表现表" tabIndex={0}><table><thead><tr><th>形态 / 波浪</th><th>有效收益 / 识别</th><th>重叠 / 等待走完</th><th>历史净胜率</th><th>校正区间</th><th>平均净变动</th><th>有利 / 不利幅度</th><th>明细</th></tr></thead><tbody>{stats.items.map(m => <ReturnStudyRow key={m.id} name={m.name} group={m.group} row={report.rows.find(r => r.pattern === m.id && r.phase === cohort && r.horizon === horizon)} partial={!!report.partial} onDetail={setDetail} />)}{!stats.items.length && <tr><td colSpan={8}>没有匹配的形态类型</td></tr>}</tbody></table></div>{stats.pages > 1 && <Pagination label="历史表现" page={stats.page} pages={stats.pages} total={metas.length} onChange={setStatsPage} />}
      <div className="pattern-caption">历史回放假设 · 每组剔除重叠持有区间 · 不足样本不输出概率区间</div></>}{!report && !active && !studyLoading && !studyError && <div className="empty">点击“计算历史表现”生成统计；没有报告不代表胜率为 0。</div>}</>}
    <details className="pattern-method"><summary>识别规则与统计口径</summary><p>当前模型 {current?.version??"—"}。枢轴须左右各 4 根已收盘 K 线确认；杯柄采用收盘价圆弧主体，突破回测采用已收盘突破和回测事件。几何幅度以 ATR 归一化。通过规则的低匹配候选保留，具体校验与前置趋势见候选详情。波浪遵守硬约束，斐波那契比例仅为参考；不声称已验证低一级子浪或唯一数浪。</p><p>首次识别固定锚点、失效线和观察期限；新增枢轴另存节点，不覆盖原图。已形成后连续两根收盘越过确认线及 0.15 ATR 缓冲才确认。失效优先；一般形态 48 根、突破回测 24 根、波浪 96 根、完整周期 144 根到期。数据中断单列。</p><p>形成概率与突破确认概率使用此前已结束的同类、同周期、同评分分组非重叠候选。曾形成后失效：计形成成功、确认失败；待定和中断不进分母。至少 20 个有效样本才输出概率；Wilson 95% 区间按当前模型的形态、阶段、分组和两个结局校正，区间宽代表不确定性大。历史频率不保证下一次结果，几何评分不等于胜率。</p><p>方向收益独立统计：下一根开盘进入，固定期限收盘退出，假设双边成本 0.10%，剔除重叠持有期并校正多重比较。回放不是当时实测。原理线按实际时间价格绘制，标签避让不移动锚点；旧版本档案保留，不混入新版概率。</p></details>
    {detail && <Dialog title={`${current?.catalog?.find(m => m.id === detail.pattern)?.name ?? detail.pattern} · 历史明细`} onClose={() => setDetail(undefined)}><div className="pattern-detail"><p>{phaseLabel[detail.phase]} · 后续 {detail.horizon} 根 · 有效样本 {detail.sample_count} / 识别 {detail.detected}</p><p>尚未走完 {detail.pending} · 重叠排除 {detail.overlap_excluded} · 无方向排除 {detail.neutral_excluded}</p><h3>最近 5 个非重叠样本</h3>{detail.recent_samples.length ? detail.recent_samples.map(s => <article key={s.evidence_id}><strong>{s.side > 0 ? "向上" : "向下"} · 净变动 {pct(s.net_return)}</strong><span>首次识别 {date(s.observed_time)}</span><span>研究入场 {date(s.entry_time)} → 退出 {date(s.exit_time)}</span><small>证据摘要 {s.evidence_id.slice(0, 16)}</small></article>) : <p>此组暂时没有可评估样本。</p>}<small>数据快照 {report?.metadata?.snapshot_at ? new Date(report.metadata.snapshot_at).toLocaleString() : "—"} · 模型 {report?.version}</small></div></Dialog>}
  </section>;
}
