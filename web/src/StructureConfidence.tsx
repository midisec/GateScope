import { patternPercent as pct, patternTime as date, sampleCountLabel, type CompletionProbability, type PatternEvent } from "./patternModel";

export function probabilityLabel(value?: CompletionProbability | null) {
  if (value?.reference_state === "STAGE_UNAVAILABLE") return "该阶段暂无统计";
  if (value?.reference_state === "HISTORY_NOT_READY") return "历史回放未就绪";
  if (value?.reference_state === "NO_MATCHING_SAMPLES" || value?.sample_count === 0) return "无同类有效样本";
  if (value?.sample_count == null || !Number.isFinite(value.sample_count)) return "等待历史统计";
  return value.sample_count < 20 ? "样本不足" : pct(value.probability ?? value.rate);
}
export function probabilityInterval(value?: CompletionProbability | null) {
  if (value?.sample_count == null || value.sample_count < 20 || !value.interval) return "—";
  return `${pct(value.interval.lower)}–${pct(value.interval.upper)}`;
}
export function StructureConfidence({ event, frozen=false }: { event:PatternEvent; frozen?:boolean }) {
  // An archived initial judgment is never replaced with a newer reference.
  const history = frozen ? event.initial_prediction : event.historical_reference;
  const terminal = !["FORMING", "FORMED"].includes(event.phase);
  const formation = history?.formation;
  const forming = event.phase === "FORMING" || !!formation;
  const asOf = history?.reference_as_of ?? history?.as_of;
  const bucket = history?.reference_score_bucket ?? history?.score_bucket;
  const bucketLabel = bucket ? ({ UNSCORED: "未评分", LOW: "0–69 分", MID: "70–84 分", HIGH: "85–100 分", ALL: "全部评分" }[bucket] ?? bucket) : undefined;
  const count = history?.sample_count;
  const failures = count != null && history?.successes != null && Number.isFinite(history.successes) && history.successes >= 0 && history.successes <= count ? count - history.successes : null;
  const referenceUnavailable = history?.reference_state === "HISTORY_NOT_READY" || history?.reference_state === "STAGE_UNAVAILABLE";
  return <div className="structure-confidence" aria-label="形成概率与历史样本置信度">
    <div><span>几何匹配</span><strong>{event.confidence == null ? "未完成 · 不评分" : `${(event.confidence*100).toFixed(0)} 分`}</strong></div>
    {forming && <div><span>{terminal ? "形成阶段的历史形成率" : "形成概率"}</span><strong>{probabilityLabel(formation)}</strong></div>}
    <div><span>{terminal ? "形成阶段的历史确认率" : "突破确认概率"}</span><strong>{probabilityLabel(history)}</strong></div>
    <small>{frozen ? "首次判断时" : "此前同类"} · 有效样本 n={sampleCountLabel(count)}{bucketLabel ? ` · ${bucketLabel}` : ""}</small>
    {asOf != null && <small>样本截止 {date(asOf)}{history?.reference_phase ? ` · ${history.reference_phase === "FORMING" ? "形成中" : "已形成"}阶段` : ""}</small>}
    {!frozen && history?.reference_is_live_prediction === false && <small>回放补充对照 · 非当时预测</small>}
    {failures != null && !referenceUnavailable && <small>确认 {sampleCountLabel(history?.successes)} / 失败 {sampleCountLabel(failures)} · 均计入有效分母</small>}
    {forming && formation?.interval && <small>形成 · 95% 校正区间 {probabilityInterval(formation)}</small>}
    {history?.interval && <small>确认 · 95% 校正区间 {probabilityInterval(history)}</small>}
    {history?.recorded_count != null && <small>实测 {sampleCountLabel(history.recorded_count)} · 回放 {sampleCountLabel(history.replay_count)} · 失效 {sampleCountLabel(history.invalidated)} · 到期 {sampleCountLabel(history.expired)}</small>}
    <small title={history?.reference_reason ?? history?.reference_basis}>几何评分不是概率，确认不等于盈利{history?.partial ? " · 回放尚未完成" : ""}</small>
    {!!event.recognition?.warnings.length && <small title={event.recognition.warnings.join("；")}>{event.recognition.warnings[0]}</small>}
  </div>;
}
