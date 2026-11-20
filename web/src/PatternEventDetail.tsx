import { useEffect, useState } from "react";
import { api } from "./api";
import { Dialog, PageState } from "./ui";
import { patternPercent as pct, patternTime as date, phaseLabel, type PatternEvent, type PatternStudy } from "./patternModel";
import { StructureConfidence } from "./StructureConfidence";
import { StructureForecast } from "./StructureForecast";
import { screeningInterval, screeningWinRate } from "./patternScreeningModel";

export function EventReturnReference({ event, study, version, horizon }: {
  event: PatternEvent; study?: PatternStudy; version: string; horizon: number;
}) {
  const report = study?.report;
  if (!report) return <p className="pattern-caption">历史回放尚未提供报告，暂无收益统计。</p>;
  if (report.version !== version) return <p className="pattern-caption">旧模型历史报告，不用于此结构的收益参考。</p>;
  const rows = (["FORMED", "CONFIRMED"] as const).map(phase => ({ phase, row: report.rows.find(row => row.pattern === event.pattern && row.phase === phase && row.horizon === horizon) }));
  return <div className="pattern-event-returns">
    <small>同类全部评分 · 不按当前评分区间或方向细分{report.partial ? " · 阶段报告" : ""}</small>
    {rows.map(({ phase, row }) => <article key={phase}><strong>{phaseLabel[phase]} · 后续 {horizon} 根</strong>
      <dl><div><dt>历史净胜率</dt><dd>{screeningWinRate(row, !!report.partial)}</dd></div>
        <div><dt>有效样本</dt><dd>{row?.sample_count?.toLocaleString() ?? "—"}</dd></div>
        <div><dt>平均净变动</dt><dd>{row?.sample_count ? pct(row.mean_net_return) : "—"}</dd></div>
        <div><dt>95% 校正区间</dt><dd>{screeningInterval(row)}</dd></div></dl>
    </article>)}
    <small>历史范围 {date(report.from_time)} — {date(report.to_time)} · 双边成本 {pct(report.round_trip_cost)}。下一根开盘至固定期限收盘的方向净收益，不是止盈止损成交胜率。</small>
  </div>;
}

export function PatternEventDetail({ event, params, version, canOverlay, overlayLabel, onOverlay, onEvidence, onCoinglass, onClose }: {
  event: PatternEvent; params: string; version: string; canOverlay: boolean; overlayLabel: string;
  onOverlay: () => void; onEvidence?: () => void; onCoinglass?:()=>void; onClose: () => void;
}) {
  const [study, setStudy] = useState<PatternStudy>(), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [retry, setRetry] = useState(0), [horizon, setHorizon] = useState(20);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError(""); setStudy(undefined);
    void api<PatternStudy>(`/api/v2/patterns/study?${params}`, { signal: abort.signal }).then(value => {
      if (!abort.signal.aborted) setStudy(value);
    }).catch(reason => { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "历史统计读取失败"); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [params, retry]);
  return <Dialog title={`${event.name} · 结构详情`} onClose={onClose}>
    <div className="pattern-event-detail">
      <div className="pattern-event-heading"><span className={`pattern-phase ${event.phase.toLowerCase()}`}>{phaseLabel[event.phase]}</span>
        <strong>{event.signal_direction === "bullish" ? "目标趋势：向上" : event.signal_direction === "bearish" ? "目标趋势：向下" : "目标趋势：方向待定"}</strong>
        <span>几何评分 {event.confidence == null ? "未评分" : `${(event.confidence * 100).toFixed(2)} 分`}</span></div>
      <small>首次观察 {date(event.first_observed_time ?? event.end_time)} · 此次打开保留候选快照</small>
      <h3>目标位置与形成条件</h3><StructureForecast event={event} />
      {onCoinglass && <button type="button" onClick={onCoinglass}>Coinglass 数据支撑</button>}
      <h3>历史形成与确认概率</h3><StructureConfidence event={event} />
      <div className="pattern-event-return-heading"><h3>同类历史净胜率</h3><label>观察期<select aria-label="结构详情收益观察期" value={horizon} onChange={e => setHorizon(Number(e.target.value))}>{[5, 10, 20, 50].map(value => <option key={value} value={value}>{value} 根 K 线</option>)}</select></label></div>
      <PageState loading={loading} error={error} onRetry={() => setRetry(value => value + 1)} />
      {!loading && !error && <EventReturnReference event={event} study={study} version={version} horizon={horizon} />}
      {event.initial_prediction && <details className="pattern-method"><summary>首次判断原始概率</summary><StructureConfidence event={event} frozen /></details>}
      {!!event.recognition?.checks?.length && <details className="pattern-method"><summary>形态校验依据</summary><ul>{event.recognition.checks.map((check, i) => <li key={i}>{check}</li>)}</ul></details>}
      <div className="pattern-event-actions"><button type="button" disabled={!canOverlay} onClick={onOverlay}>{overlayLabel}</button>{onEvidence && <button type="button" onClick={onEvidence}>查看原始档案图</button>}<button type="button" onClick={onClose}>返回列表</button></div>
    </div>
  </Dialog>;
}
