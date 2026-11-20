import { useEffect, useRef, useState } from "react";
import { api, jsonBody } from "./api";
import { Dialog, PageState } from "./ui";
import { patternTime, phaseLabel, type PatternEvent } from "./patternModel";
import {
  coinglassBundleStatus, coinglassElapsed, coinglassMetricLabels, coinglassMetricReadiness, coinglassMetricSummary,
  coinglassNumber, coinglassPollSeconds, coinglassQueueAge, coinglassReason, coinglassRefreshState, coinglassRetrySeconds, coinglassSeriesUsable,
  coinglassStatus, parseCoinglassScenario, usableCoinglassSeries,
  type CoinglassBundle, type CoinglassMetricId, type CoinglassReview, type CoinglassReviewResponse,
  type CoinglassScenario, type CoinglassScope,
} from "./coinglassModel";
import { useCoinglassClock } from "./useCoinglassClock";
import "./coinglassContext.css";

export type CoinglassControlsProps = { bundle?: CoinglassBundle; loading?: boolean; error?: string;
  metrics: CoinglassMetricId[]; onMetricsChange: (metrics: CoinglassMetricId[]) => void; onRefresh: () => void };
export type CoinglassPatternReviewProps = { event: PatternEvent; instrumentId: string; timeframe: string;
  onClose: () => void; onPreview?: (scenario: CoinglassScenario | null) => void };

function CoinglassScopeLabel({ scope }: { scope: CoinglassScope | null | undefined }) {
  return scope ? <span className="coinglass-scope">{scope.exchange} · {scope.symbol} · {scope.settlement_currency} 结算 · 永续</span>
    : <span className="coinglass-scope">等待精确合约匹配</span>;
}

export function CoinglassCacheStatus({ bundle, now = Date.now() / 1000, autoPolling = false }: { bundle: Partial<CoinglassBundle> & { status: string }; now?: number; autoPolling?: boolean }) {
  const remaining = coinglassRetrySeconds(bundle, now), status = coinglassBundleStatus(bundle, now);
  const refresh = coinglassRefreshState(bundle, now), summary = coinglassMetricSummary(bundle, now), queued = coinglassQueueAge(bundle, now);
  const active = ["QUEUED", "COLLECTING", "COOLDOWN", "SCHEDULED"].includes(refresh);
  const labels = { COLLECTING: "后台采集中", COOLDOWN: remaining ? "等待额度 · 数据源冷却中" : "冷却时间已到，等待后台检查", SCHEDULED: "已计划，等待调度", QUEUED: "已排队，等待采集", IDLE: "当前无采集任务", NOT_REQUESTED: "尚未请求更新" };
  const nextEligible = bundle.next_eligible_at ?? bundle.next_refresh_at;
  return <div className="coinglass-status" role="status"><span>数据：{coinglassStatus(status)}</span>
    <strong>已就绪 {summary.ready}/{summary.total}</strong>{summary.historical > 0 && <span>历史参考 {summary.historical}/{summary.total}</span>}
    <span>更新进度：{labels[refresh]}</span>
    {active && queued != null && <span aria-live="off">已等待 {coinglassElapsed(queued)}</span>}
    {refresh === "COLLECTING" && bundle.collection_started_at != null && Number.isFinite(bundle.collection_started_at)
      && bundle.collection_started_at > 0 && bundle.collection_started_at <= now && <span aria-live="off">本次采集已进行 {coinglassElapsed(now - bundle.collection_started_at)}</span>}
    {remaining > 0 ? <span aria-live="off">{remaining} 秒后可重试，后台将自动续采</span>
      : ["QUEUED", "COLLECTING"].includes(refresh) ? <span>{autoPolling ? "每" : "建议"} {coinglassPollSeconds(bundle, now)} 秒{autoPolling ? "检查进度" : "后检查进度"}，不是完成时间</span>
      : refresh === "SCHEDULED" && nextEligible != null && nextEligible > now ? <time>最早采集 {patternTime(nextEligible)}，不是完成时间</time> : null}
    {bundle.as_of != null && bundle.as_of > 0 && <time>数据截至 {patternTime(bundle.as_of)}</time>}
    {bundle.fetched_at != null && bundle.fetched_at > 0 && <time>获取于 {patternTime(bundle.fetched_at)}</time>}
    {(bundle.cache_retained || status === "STALE") && <span>包含历史缓存 · 仅供参考，不代表最新数据</span>}
    {bundle.refresh_requested && <span>已请求后台更新，无需连续刷新</span>}
    {!!bundle.last_attempt_at && <time>上次尝试 {patternTime(bundle.last_attempt_at)}</time>}
  </div>;
}

export function CoinglassControls({ bundle, loading = false, error = "", metrics, onMetricsChange, onRefresh }: CoinglassControlsProps) {
  const refreshing = ["QUEUED", "COLLECTING"].includes(coinglassRefreshState(bundle));
  const now = useCoinglassClock(bundle?.retry_at, refreshing), remaining = coinglassRetrySeconds(bundle, now);
  const [mobile, setMobile] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(max-width: 640px)").matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 640px)"), changed = () => setMobile(media.matches);
    changed(); media.addEventListener("change", changed); return () => media.removeEventListener("change", changed);
  }, []);
  useEffect(() => { if (mobile && metrics.length > 1) onMetricsChange(metrics.slice(0, 1)); }, [mobile, metrics.join(":"), onMetricsChange]);
  const usable = usableCoinglassSeries(bundle), available = new Set(usable.map(series => series.id));
  const toggle = (id: CoinglassMetricId) => onMetricsChange(metrics.includes(id) ? metrics.filter(value => value !== id) : [...metrics, id]);
  return <section className="coinglass-controls" aria-label="Coinglass 衍生品数据">
    <header><div><strong>衍生品数据</strong><CoinglassScopeLabel scope={bundle?.scope} /></div>
      <button type="button" onClick={() => { if (!loading && !remaining) onRefresh(); }} disabled={loading || remaining > 0}>{loading ? "读取中…" : remaining > 0 ? "冷却中" : "刷新数据"}</button></header>
    <PageState loading={loading && !bundle} error={error} onRetry={remaining > 0 ? undefined : onRefresh} />
    {bundle ? <CoinglassCacheStatus bundle={bundle} now={now} autoPolling /> : <div className="coinglass-status" role="status">{loading ? "读取后台缓存…" : "等待后台数据"}</div>}
    {mobile ? <label className="coinglass-mobile-select">衍生品副图<select aria-label="手机衍生品副图" value={metrics.find(id => available.has(id)) ?? ""} onChange={event => onMetricsChange(event.target.value ? [event.target.value as CoinglassMetricId] : [])}>
      <option value="">隐藏副图</option>{(Object.keys(coinglassMetricLabels) as CoinglassMetricId[]).map(id => {
        const source = bundle?.series.find(series => series.id === id);
        const state = source?.unit_status === "UNKNOWN" ? "单位待核实" : source?.status === "RATE_LIMITED" && !remaining ? "等待重试" : coinglassStatus(source?.status ?? "EMPTY");
        return <option key={id} value={id} disabled={!available.has(id)}>{coinglassMetricLabels[id]} · {!available.has(id) ? state : bundle && coinglassMetricReadiness(bundle, id, now) === "HISTORICAL" ? "缓存参考" : "已就绪"}</option>;
      })}</select></label>
      : <div className="coinglass-metric-toggles" role="group" aria-label="Coinglass 副图选择">{(Object.keys(coinglassMetricLabels) as CoinglassMetricId[]).map(id => {
      const source = bundle?.series?.find(series => series.id === id), disabled = !available.has(id);
      const latest = source && coinglassSeriesUsable(source, bundle?.timeframe) ? source.points.at(-1)?.value : null;
      const historical = bundle && coinglassMetricReadiness(bundle, id, now) === "HISTORICAL";
      return <label key={id} className={`${metrics.includes(id) && !disabled ? "selected" : ""} ${disabled ? "unavailable" : ""}`}>
        <input type="checkbox" checked={metrics.includes(id) && !disabled} disabled={disabled} onChange={() => toggle(id)} aria-label={`显示 ${coinglassMetricLabels[id]} 副图`} />
        <span>{coinglassMetricLabels[id]}<small>{disabled ? source?.unit_status === "UNKNOWN" ? "单位待核实" : source?.status === "RATE_LIMITED" && !remaining ? "等待重试" : coinglassStatus(source?.status ?? "EMPTY") : <>{coinglassNumber(latest, source?.unit)} · {historical ? "缓存参考" : "已就绪"}</>}</small></span>
      </label>;
    })}</div>}
    <details className="coinglass-method"><summary>数据口径</summary><p>仅展示精确匹配的同交易所、同合约、同周期原生数据。资金费率单位未核实时不展示数值；全账户多空比不是持仓金额占比。数据窗口最多 256 期，不代表一年完整历史；缺口不补零。</p><p>当前数据可辅助观察，不改变形态的原始预测、几何评分或历史胜率。</p></details>
  </section>;
}

export function CoinglassReviewContent({ review }: { review: CoinglassReview }) {
  const original = review.original, forecast = original.forecast, initial = original.initial_forecast;
  const nextPivot = review.target_semantics === "NEXT_PIVOT_OBSERVATION_NOT_TAKE_PROFIT";
  return <div className="coinglass-review-content">
    <section className="coinglass-review-heading"><CoinglassScopeLabel scope={review.scope} /><div className="coinglass-status"><strong>{coinglassStatus(review.state)}</strong><time>研判于 {patternTime(review.assessed_at)}</time></div></section>
    {review.reason_codes.length > 0 && <div className="coinglass-warning" role="status">{review.reason_codes.map(code => <p key={code}>{coinglassReason(code)}</p>)}</div>}
    {review.bundle && <CoinglassCacheStatus bundle={review.bundle} />}
    <section className="coinglass-original" aria-label="原始结构预测"><h3>原始结构 · 保持不变</h3>
      <dl><div><dt>结构状态</dt><dd>{phaseLabel[original.phase as keyof typeof phaseLabel] ?? "—"}</dd></div>
        <div><dt>几何评分</dt><dd>{coinglassNumber(original.confidence == null ? null : original.confidence * 100)} 分</dd></div>
        <div><dt>{nextPivot ? "下一拐点观察区" : "原始条件目标区"}</dt><dd>{coinglassNumber(forecast?.target?.lower)}–{coinglassNumber(forecast?.target?.upper)}</dd></div>
        <div><dt>原结构失效位</dt><dd>{coinglassNumber(review.original_conditions.invalidation?.price)}</dd></div></dl>
      {initial?.id && initial.id !== forecast?.id && <small>首次预测 {coinglassNumber(initial.target?.price)} · 本次原预测 {coinglassNumber(forecast?.target?.price)}</small>}
      {nextPivot && <p className="coinglass-warning">等待结构形成；下一拐点不是已确认的交易方向或止盈区。</p>}
      <details><summary>确认与失效条件</summary>{forecast?.condition && <p>{forecast.condition}</p>}
        {review.original_conditions.confirmation.map((line, index) => <p key={index}>连续 {review.original_conditions.confirmation_closes ?? "—"} 根收盘{line.side > 0 ? "高于" : "低于"} {coinglassNumber(line.price)} · 投影时点 {patternTime(line.projected_at)}</p>)}
        <p>失效规则：{review.original_conditions.invalidation?.mode === "WICK_INVALIDATION" ? "影线触及结构边界" : review.original_conditions.invalidation ? "收盘越过结构边界" : "缺少有效失效线"}。没有下单或设置交易所止损。</p></details>
    </section>
    <section className="coinglass-evidence" aria-label="Coinglass 当前资金面证据"><h3>当前资金面证据</h3>
      {review.metric_evidence.length ? <div className="coinglass-evidence-grid">{review.metric_evidence.map(metric => <article key={metric.id}>
        <header><strong>{metric.label}</strong><span>{coinglassNumber(metric.latest.value, metric.unit)}</span></header>
        {metric.window_change_percent != null && <p>窗口美元估值变化 {coinglassNumber(metric.window_change_percent, "PERCENT")}</p>}
        <small>{metric.points_count} 个原生周期 · 截至 {patternTime(metric.window_end)}</small>
        <details><summary>统计含义</summary><p>{metric.note}</p><p>窗口开始 {patternTime(metric.window_start)}；平台获取 {patternTime(metric.latest.available_at)}。</p></details>
      </article>)}</div> : <p className="empty">暂无符合时间、单位与合约口径的数据</p>}
      <details className="coinglass-method"><summary>研判边界</summary><p>这是本次最新数据研判，不是原始识别时已保存的资金面。历史数据不能倒填成当时证据。当前四类数据不包含目标价格分布，不自动平移原目标，也未重新估计形成概率或胜率。</p></details>
    </section>
  </div>;
}

export function CoinglassPatternReview({ event, instrumentId, timeframe, onClose, onPreview }: CoinglassPatternReviewProps) {
  const key = `${instrumentId}:${timeframe}:${event.context_id ?? ""}:${event.id}:${event.evidence_id}`;
  const [load, setLoad] = useState<{ key: string; loading: boolean; error: string; response?: CoinglassReviewResponse }>({ key, loading: true, error: "" });
  const [retry, setRetry] = useState(0), [entry, setEntry] = useState(""), [lower, setLower] = useState(""), [upper, setUpper] = useState("");
  const [formError, setFormError] = useState(""), [saving, setSaving] = useState(false);
  const [contextChanged, setContextChanged] = useState(false);
  const now = useCoinglassClock(load.key === key ? load.response?.review.bundle?.retry_at : undefined);
  const request = useRef<AbortController | null>(null), generation = useRef(0), saveLock = useRef(false);
  const payload = (scenario?: { entry: number; target_lower: number; target_upper: number }) => ({
    instrument_id: instrumentId, timeframe, context_id: event.context_id,
    candidate_id: event.id, expected_evidence_id: scenario ? load.response?.review.evidence_id ?? event.evidence_id : event.evidence_id, ...(scenario ? { scenario } : {}),
  });
  useEffect(() => {
    const abort = new AbortController(), version = ++generation.current; request.current = abort;
    setLoad({ key, loading: true, error: "" }); setEntry(""); setLower(""); setUpper(""); setFormError(""); setSaving(false); setContextChanged(false); saveLock.current = false;
    void api<CoinglassReviewResponse>("/api/v2/coinglass/reviews", { method: "POST", body: jsonBody(payload()), signal: abort.signal }).then(response => {
      if (abort.signal.aborted || version !== generation.current) return;
      if (response.review.instrument_id !== instrumentId || response.review.timeframe !== timeframe || response.review.candidate_id !== event.id || (response.review.evidence_id !== event.evidence_id && !response.review.evidence_changed)) throw new Error("研判结果与当前形态不一致，请刷新后重试");
      setLoad({ key, loading: false, error: "", response });
      const target = response.review.original.forecast?.target;
      if (target) { setLower(String(target.lower)); setUpper(String(target.upper)); }
    }).catch(error => { if (!abort.signal.aborted && version === generation.current) setLoad({ key, loading: false, error: error instanceof Error ? error.message : "研判读取失败" }); });
    return () => { abort.abort(); generation.current++; request.current?.abort(); };
  }, [key, retry]);
  const current = load.key === key ? load : { key, loading: true, error: "", response: undefined };
  const review = current.response?.review;
  const remaining = coinglassRetrySeconds(review?.bundle, now);
  const close = () => { request.current?.abort(); generation.current++; onClose(); };
  const save = async (form: React.FormEvent) => {
    form.preventDefault(); if (saveLock.current || saving || current.loading || !review || review.state !== "READY") return;
    const parsed = parseCoinglassScenario(entry, lower, upper);
    if (parsed.error) { setFormError(parsed.error); return; }
    setFormError(""); setSaving(true); saveLock.current = true;
    const abort = new AbortController(), version = ++generation.current; request.current?.abort(); request.current = abort;
    try {
      const response = await api<CoinglassReviewResponse>("/api/v2/coinglass/reviews", { method: "POST", body: jsonBody(payload(parsed.scenario)), signal: abort.signal });
      if (!abort.signal.aborted && version === generation.current) {
        if (response.review.instrument_id !== instrumentId || response.review.timeframe !== timeframe || response.review.candidate_id !== event.id || response.review.evidence_id !== review.evidence_id) throw new Error("研判结果与当前形态不一致，请刷新后重试");
        setContextChanged(response.review.bundle?.snapshot_id !== review.bundle?.snapshot_id
          || JSON.stringify(response.review.metric_evidence) !== JSON.stringify(review.metric_evidence));
        setLoad({ key, loading: false, error: "", response });
      }
    } catch (error) { if (!abort.signal.aborted && version === generation.current) setFormError(error instanceof Error ? error.message : "情景保存失败"); }
    finally { if (!abort.signal.aborted && version === generation.current) { setSaving(false); saveLock.current = false; } }
  };
  return <Dialog title={`${event.name} · Coinglass 数据支撑`} onClose={close}>
    <div className="coinglass-review"><PageState loading={current.loading} error={current.error} onRetry={remaining ? undefined : () => setRetry(value => value + 1)} />
      {review && !current.loading && <>{review.evidence_changed&&<p className="coinglass-warning" role="status">结构已更新；本次研判采用最新快照，原始档案保持不变。</p>}<div className="coinglass-scenario-actions"><button type="button" disabled={saving || remaining > 0} onClick={() => { if (!remaining && !saving) setRetry(value => value + 1); }}>重新获取数据研判</button><small>{remaining ? `数据源冷却中 · ${remaining} 秒后可重试` : "生成新快照，已保存记录保持不变"}</small></div>
        {contextChanged && <p className="coinglass-warning" role="status">本次保存使用了更新的资金面快照，研判时点 {patternTime(review.assessed_at)}。请核对最新证据后再查看图表情景。</p>}
        <CoinglassReviewContent review={review} />
        <section className="coinglass-scenario" aria-label="独立观察情景"><h3>独立观察情景</h3>
          {review.state === "READY" ? <form onSubmit={save} aria-label="观察区间试算" aria-busy={saving}>
            <div className="coinglass-scenario-fields"><label>观察参考价<input type="number" inputMode="decimal" min="0" step="any" value={entry} onChange={event => setEntry(event.target.value)} disabled={saving} aria-label="观察参考价" /></label>
              <label>候选区下界<input type="number" inputMode="decimal" min="0" step="any" value={lower} onChange={event => setLower(event.target.value)} disabled={saving} aria-label="候选区下界" /></label>
              <label>候选区上界<input type="number" inputMode="decimal" min="0" step="any" value={upper} onChange={event => setUpper(event.target.value)} disabled={saving} aria-label="候选区上界" /></label></div>
            {formError && <p className="coinglass-warning" role="alert">{formError}</p>}
            <div className="coinglass-scenario-actions"><button type="submit" disabled={saving}>{saving ? "保存中…" : "保存独立观察情景"}</button><small>独立保存，不覆盖原始预测</small></div>
          </form> : <p>数据或结构条件未满足，暂不生成新情景。</p>}
          {review.scenario && <div className="coinglass-scenario-saved" role="status"><strong>已保存 · {review.scenario.target_label}</strong><span>{coinglassNumber(review.scenario.target_lower)}–{coinglassNumber(review.scenario.target_upper)}</span>
            <small>发布于 {patternTime(review.scenario.issued_at)}{review.scenario.kind === "NEXT_PIVOT_OBSERVATION" ? " · 不是入场指令或止盈区" : " · 仅价差试算，未扣成本"}</small>
            {onPreview && <div className="coinglass-scenario-actions"><button type="button" disabled={saving} onClick={() => { onPreview(review.scenario); close(); }}>在图表查看情景</button><button type="button" disabled={saving} onClick={() => onPreview(null)}>清除图表情景</button></div>}</div>}
        </section>
      </>}
      <footer className="coinglass-review-footer"><button type="button" onClick={close}>返回形态</button>{review && <span>研判快照已保存 · 不会自动修改</span>}</footer>
    </div>
  </Dialog>;
}
