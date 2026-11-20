import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Dialog, PageState, Pagination, pageSlice } from "./ui";
import { patternPercent, patternTime, phaseLabel, sampleCountLabel, type PatternStudy } from "./patternModel";
import {
  screeningHistory, screeningInterval, screeningPatternHistory, screeningProbability, screeningShare, screeningWinRate,
  type PatternScreeningGroup, type PatternScreeningSummaryData, type ScreeningDirections as DirectionCounts, type ScreeningHistory,
  type ScreeningHorizon, type ScreeningReturnPhase,
} from "./patternScreeningModel";
import "./patternScreening.css";

export type PatternScreeningSummaryProps = {
  summary: PatternScreeningSummaryData; params: string; asOf: number | null;
  filterLabel: string; version: string; onClose: () => void; onPattern: (pattern: string) => void;
};

const directionLabels = { bullish: "向上", bearish: "向下", neutral: "方向待定" } as const;

export function ScreeningDirections({ directions, total }: { directions: DirectionCounts; total: number }) {
  return <dl className="screening-directions" aria-label="方向占比（非涨跌概率）">
    {(Object.keys(directionLabels) as (keyof DirectionCounts)[]).map(key => <div key={key} className={`screening-direction-${key}`}>
      <dt>{directionLabels[key]}</dt><dd>{sampleCountLabel(directions[key])}<small>{patternPercent(screeningShare(directions[key], total))}</small></dd>
    </div>)}
  </dl>;
}

export function ScreeningOverview({ summary, asOf, filterLabel }: Pick<PatternScreeningSummaryProps, "summary" | "asOf" | "filterLabel">) {
  return <section className="screening-overview" aria-label="筛选概览">
    <div className="screening-snapshot"><strong>{filterLabel}</strong><span>当前跟踪集合 · 全部匹配，非当前页</span><time>识别截至 {patternTime(asOf)}</time></div>
    <dl className="screening-totals"><div><dt>全部匹配</dt><dd aria-label="全部匹配数量">{sampleCountLabel(summary.total)}</dd></div>
      <div><dt>形态类型</dt><dd>{summary.patterns.length}</dd></div><div><dt>已评分 / 未评分</dt><dd>{sampleCountLabel(summary.scored_count)} / {sampleCountLabel(summary.unscored_count)}</dd></div></dl>
    <ScreeningDirections directions={summary.directions} total={summary.total} />
    <div className="screening-phase-chips" aria-label="匹配结构状态分布">{Object.entries(summary.phase_counts).filter(([, count]) => count && count > 0).map(([phase, count]) => <span key={phase}>{phaseLabel[phase as keyof typeof phaseLabel] ?? phase} {sampleCountLabel(count)}</span>)}</div>
    <small className="screening-note">方向占比只按匹配数量统计，不是行情上涨或下跌概率。</small>
  </section>;
}

export function ScreeningHistoryStatus({ history, study, version }: { history: ScreeningHistory; study?: PatternStudy; version: string }) {
  const report = history.report;
  return <div className={`screening-history-status ${history.state === "VERSION_MISMATCH" || history.state === "FAILED" ? "screening-warning" : ""}`} role="status">
    <strong>{history.label}</strong>
    <span>当前模型 {version || "—"}{study?.report && study.report.version !== version ? ` · 历史模型 ${study.report.version}` : ""}</span>
    {report && <><time>历史报告截至 {patternTime(report.to_time)}</time><span>{sampleCountLabel(report.bar_count)} 根 K 线{report.metadata?.coverage_ratio != null ? ` · 目标历史覆盖 ${patternPercent(report.metadata.coverage_ratio)}` : ""}</span></>}
  </div>;
}

export function ScreeningPatternRow({ item, history, phase, horizon, onPattern }: {
  item: PatternScreeningGroup; history: ScreeningHistory; phase: ScreeningReturnPhase; horizon: ScreeningHorizon;
  onPattern: (pattern: string) => void;
}) {
  const values = screeningPatternHistory(history, item.pattern, phase, horizon);
  const unavailable = history.state === "VERSION_MISMATCH" ? "模型不一致" : !history.report ? "等待历史统计" : null;
  const formation = unavailable ?? (!values.supportsForming ? "不支持形成中预判" : screeningProbability(values.formation, history.partial));
  const confirmation = unavailable ?? screeningProbability(values.confirmation, history.partial);
  const winRate = unavailable ?? screeningWinRate(values.returns, history.partial);
  const scores = item.score_min == null || item.score_max == null ? "未评分" : `${(item.score_min * 100).toFixed(2)}–${(item.score_max * 100).toFixed(2)} 分`;
  return <article className="screening-pattern-row" aria-label={`${item.name} 匹配与历史参考`}>
    <header><div><h3>{item.name}</h3><small>{item.family === "wave" ? "波浪理论" : "图表形态"} · {item.group}</small></div>
      <div className="screening-type-share"><strong aria-label="类型匹配数量">{sampleCountLabel(item.count)} 个</strong><span aria-label="类型占比">占匹配 {patternPercent(item.share)}</span></div>
      <button type="button" onClick={() => onPattern(item.pattern)} aria-label={`查看 ${item.name} 匹配结构`}>查看匹配</button></header>
    <div className="screening-pattern-description"><span>当前几何评分 {scores}</span><span>向上 {item.directions.bullish} · 向下 {item.directions.bearish} · 待定 {item.directions.neutral}</span></div>
    <dl className="screening-history-metrics"><div><dt>历史形成概率</dt><dd>{formation}</dd></div><div><dt>历史确认概率</dt><dd>{confirmation}</dd></div><div><dt>历史净胜率 · {horizon} 根</dt><dd>{winRate}</dd></div></dl>
    <details className="screening-sample-detail"><summary>{item.name} · 样本与区间</summary>
      <dl><div><dt>形成有效样本 / 95% 校正区间</dt><dd>n={sampleCountLabel(values.formation?.sample_count)} · {screeningInterval(values.formation)}</dd></div>
        <div><dt>确认有效样本 / 95% 校正区间</dt><dd>n={sampleCountLabel(values.confirmation?.sample_count)} · {screeningInterval(values.confirmation)}</dd></div>
        <div><dt>收益有效样本 / 95% 校正区间</dt><dd>n={sampleCountLabel(values.returns?.sample_count)} · {screeningInterval(values.returns)}</dd></div>
        <div><dt>收益阶段 / 观察期</dt><dd>{phaseLabel[phase]} · 后续 {horizon} 根</dd></div>
        <div><dt>收益等待走完 / 重叠排除</dt><dd>{sampleCountLabel(values.returns?.pending)} / {sampleCountLabel(values.returns?.overlap_excluded)}</dd></div></dl>
      <small className="screening-note">历史参考采用同类全部评分与方向，未按当前评分区间重新统计。</small>
    </details>
  </article>;
}

export function PatternScreeningView({ summary, asOf, filterLabel, version, study, loading = false, error = "", onRetry, onPattern }: Omit<PatternScreeningSummaryProps, "params" | "onClose"> & {
  study?: PatternStudy; loading?: boolean; error?: string; onRetry: () => void;
}) {
  const [page, setPage] = useState(0), [phase, setPhase] = useState<ScreeningReturnPhase>("CONFIRMED"), [horizon, setHorizon] = useState<ScreeningHorizon>(20);
  useEffect(() => setPage(0), [filterLabel, summary.patterns.map(item => item.pattern).join(":"), asOf]);
  const history = screeningHistory(error || loading ? undefined : study, version);
  const paged = pageSlice(summary.patterns, page, 8);
  return <div className="pattern-screening">
    <ScreeningOverview summary={summary} asOf={asOf} filterLabel={filterLabel} />
    <section className="screening-history-section" aria-label="历史参考 · 同类全部评分">
      <div className="screening-history-heading"><h3>历史参考 · 同类全部评分</h3><button type="button" disabled={loading} onClick={onRetry}>{loading ? "读取中…" : "刷新历史参考"}</button></div>
      <PageState loading={loading} error={error} onRetry={onRetry} />
      {!loading && !error && <ScreeningHistoryStatus history={history} study={study} version={version} />}
      <div className="screening-history-controls"><label>收益样本阶段<select aria-label="收益样本阶段" value={phase} onChange={event => setPhase(event.target.value as ScreeningReturnPhase)}><option value="CONFIRMED">已确认</option><option value="FORMED">已形成（含后续确认）</option></select></label>
        <label>收益观察期<select aria-label="收益观察期" value={horizon} onChange={event => setHorizon(Number(event.target.value) as ScreeningHorizon)}>{[5, 10, 20, 50].map(value => <option key={value} value={value}>后续 {value} 根 K 线</option>)}</select></label></div>
      <details className="screening-method"><summary>历史统计口径</summary><p>当前数量与占比覆盖筛选后的全部匹配项。历史形成概率取“形成中”阶段全部评分样本；确认概率取“已形成”阶段全部评分样本。两者不是当前精确评分区间、当前方向或当前目标点位的成功率，几何评分也不是概率。</p><p>历史净胜率为所选阶段下一根开盘进入、固定观察期收盘退出后的正净变动比例，双边成本假设 {history.report ? patternPercent(history.report.round_trip_cost) : "以报告为准"}。它不是当前目标的止盈率。待定、中断与重叠样本按原报告口径处理；不足 20 个有效样本不展示概率。</p><p>各类型历史样本可能重叠，不能相加样本数或平均概率合成为市场上涨概率。旧模型报告不混入当前参考，部分回放只展示已计算部分。</p></details>
    </section>
    <section className="screening-pattern-list" aria-label="匹配类型列表">{paged.items.length ? paged.items.map(item => <ScreeningPatternRow key={item.pattern} item={item} history={history} phase={phase} horizon={horizon} onPattern={onPattern} />) : <div className="empty">当前筛选没有匹配结构</div>}</section>
    {paged.pages > 1 && <Pagination label="匹配形态类型" page={paged.page} pages={paged.pages} total={summary.patterns.length} onChange={setPage} />}
  </div>;
}

export function PatternScreeningSummary({ params, onClose, ...props }: PatternScreeningSummaryProps) {
  const [load, setLoad] = useState<{ key: string; study?: PatternStudy; loading: boolean; error: string }>({ key: params, loading: true, error: "" });
  const [retry, setRetry] = useState(0);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const abort = new AbortController(); request.current = abort;
    setLoad({ key: params, loading: true, error: "" });
    void api<PatternStudy>(`/api/v2/patterns/study?${params}`, { signal: abort.signal }).then(study => {
      if (!abort.signal.aborted) setLoad({ key: params, study, loading: false, error: "" });
    }).catch(error => {
      if (!abort.signal.aborted) setLoad({ key: params, loading: false, error: error instanceof Error ? error.message : "历史参考读取失败，请重试" });
    });
    return () => { abort.abort(); if (request.current === abort) request.current = null; };
  }, [params, retry]);
  const current = load.key === params ? load : { loading: true, error: "", study: undefined };
  return <Dialog title="筛选结果详情" onClose={() => { request.current?.abort(); onClose(); }}>
    <PatternScreeningView {...props} study={current.study} loading={current.loading} error={current.error} onRetry={() => setRetry(value => value + 1)} />
  </Dialog>;
}
