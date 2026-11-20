export type CoinglassMetricId = "oi" | "funding" | "lsr" | "liquidation_long" | "liquidation_short";
export type CoinglassRefreshState = "COLLECTING" | "COOLDOWN" | "SCHEDULED" | "QUEUED" | "IDLE" | "NOT_REQUESTED";
export type CoinglassPoint = { time: number; close_time: number; available_at: number; value: number };
export type CoinglassSeries = {
  id: CoinglassMetricId; label: string; unit: "USD" | "PERCENT" | "RATIO" | string;
  unit_status: "VERIFIED" | "UNKNOWN"; population: string; native_timeframe: string;
  points: CoinglassPoint[]; status: string; quality_flags: string[];
  coverage?: number; freshness_seconds?: number | null; funding_interval_seconds?: number | null;
  fetched_at?: number; expires_at?: number; as_of?: number | null; cache_retained?: boolean; refresh_status?: string;
};
export type CoinglassScope = { exchange: string; symbol: string; base_asset: string; quote_asset: string;
  settlement_currency: string; market: string; product_type?: string; funding_interval_seconds?: number | null };
export type CoinglassBundle = {
  version?: string; snapshot_id?: string; status: string; instrument_id: string; timeframe: string;
  scope: CoinglassScope | null; match: { status: string; reason?: string };
  as_of: number | null; fetched_at: number; expires_at: number; series: CoinglassSeries[];
  history_scope?: string; retry_after_seconds?: number | null; safe_errors?: { group: string; code: string }[];
  retry_at?: number | null; last_attempt_at?: number | null; refresh_status?: string;
  cache_retained?: boolean; next_refresh_at?: number | null; refresh_requested?: boolean;
  last_refresh_status?: string; last_safe_errors?: { group: string; code: string }[]; last_reason?: string;
  refresh_state?: CoinglassRefreshState; queued_at?: number | null; queue_age_seconds?: number | null;
  collection_started_at?: number | null; next_poll_after_seconds?: number | null; next_eligible_at?: number | null;
  cache_available?: boolean; cached_points_count?: number; last_success_at?: number | null;
};
export type CoinglassScenario = {
  kind: "NEXT_PIVOT_OBSERVATION" | "CONDITIONAL_MOVE_SCENARIO"; entry: number;
  target_lower: number; target_upper: number; issued_at: number; direction: string;
  target_label: string; status: string; source?: string; parent_forecast_id?: string | null;
  risk_reward: { lower: number; upper: number } | null; auto_order: false; costs_included: false;
  invalidation?: { price: number; side: number; projected_at: number; mode: string };
};
export type CoinglassMetricEvidence = {
  id: CoinglassMetricId; label: string; unit: string; population: string; latest: CoinglassPoint;
  window_start: number; window_end: number; points_count: number; window_change_percent: number | null;
  quality_flags: string[]; note: string;
};
export type CoinglassReview = {
  evidence_changed?: boolean; requested_evidence_id?: string;
  assessment_id: string; state: string; assessed_at: number; decision_bar_time: number;
  instrument_id: string; timeframe: string; candidate_id: string; evidence_id: string;
  basis: string; scope: CoinglassScope | null; match: { status: string; reason?: string };
  reason_codes: string[]; metric_evidence: CoinglassMetricEvidence[]; bundle?: CoinglassBundle;
  original: { phase?: string; signal_direction?: string; confidence?: number | null;
    forecast?: { id?: string; kind?: string; target?: { price: number; lower: number; upper: number; label?: string } | null;
      condition?: string; issued_at?: number; deadline_time?: number };
    initial_forecast?: { id?: string; target?: { price: number; lower: number; upper: number; label?: string } | null } | null };
  original_conditions: { confirmation: { side: number; price: number; projected_at: number; comparison: string }[];
    confirmation_closes?: number | null; projected_at: number;
    invalidation?: { price: number; side: number; projected_at: number; mode: string } | null };
  target_semantics: string; adjusted_formation_probability: null; adjusted_win_rate: null;
  automatic_price_adjustment: false; auto_order: false; scenario: CoinglassScenario | null;
};
export type CoinglassReviewResponse = { review_id: string; review: CoinglassReview };

const metricUnits: Record<CoinglassMetricId, string> = { oi: "USD", funding: "PERCENT", lsr: "RATIO", liquidation_long: "USD", liquidation_short: "USD" };
export const coinglassMetricLabels: Record<CoinglassMetricId, string> = { oi: "OI 持仓量", funding: "资金费率", lsr: "全账户多空比", liquidation_long: "多单实际爆仓", liquidation_short: "空单实际爆仓" };
const statusLabels: Record<string, string> = { READY: "已更新", PARTIAL: "部分数据可用", UNSUPPORTED: "暂不支持此合约", AMBIGUOUS: "合约匹配有歧义", ERROR: "数据读取异常", EMPTY: "尚无已收盘数据", STALE: "数据已过期", RATE_LIMITED: "数据源限频中", DEFERRED: "等待下次采集", MISMATCH: "维度或单位未核实", NOT_CONFIGURED: "尚未配置 Coinglass", NO_CONFIG: "请先在用户设置配置 Coinglass", NOT_CACHED: "等待后台采集", DISABLED: "Coinglass 已停用", PENDING: "后台采集中", WARMING: "后台采集中", QUEUED: "等待后台采集", UNAVAILABLE: "本次研判不可用", NO_DATA: "等待后台采集" };
export function coinglassStatus(status: string) { return statusLabels[status] ?? "数据暂不可用"; }
export function coinglassRetrySeconds(bundle?: Pick<CoinglassBundle, "retry_at">, now = Date.now() / 1000) {
  // A cached relative duration must never restart the cooldown on every read.
  return bundle?.retry_at != null && Number.isFinite(bundle.retry_at) ? Math.max(0, Math.ceil(bundle.retry_at - now)) : 0;
}
export function coinglassRefreshState(bundle?: Partial<CoinglassBundle>, now = Date.now() / 1000): CoinglassRefreshState {
  if (coinglassRetrySeconds(bundle, now)) return "COOLDOWN";
  if (bundle?.refresh_state && ["COLLECTING", "COOLDOWN", "SCHEDULED", "QUEUED", "IDLE", "NOT_REQUESTED"].includes(bundle.refresh_state)) return bundle.refresh_state;
  // Older responses cannot prove that a worker has claimed a lease.
  if (bundle?.refresh_requested || ["QUEUED", "PENDING", "WARMING"].includes(bundle?.status ?? "")) return "QUEUED";
  if (bundle?.next_refresh_at != null && bundle.next_refresh_at > now) return "SCHEDULED";
  return !bundle || ["NO_CONFIG", "NOT_CONFIGURED", "DISABLED"].includes(bundle.status ?? "") ? "NOT_REQUESTED" : "IDLE";
}
export function coinglassPollSeconds(bundle?: Partial<CoinglassBundle>, now = Date.now() / 1000) {
  const remaining = coinglassRetrySeconds(bundle, now);
  if (remaining) return remaining;
  const state = coinglassRefreshState(bundle, now), hint = bundle?.next_poll_after_seconds;
  if (["IDLE", "NOT_REQUESTED"].includes(state) && hint === 0) return 0;
  if (["QUEUED", "COLLECTING"].includes(state)) {
    const fallback = state === "COLLECTING" ? 3 : 5;
    return hint != null && Number.isFinite(hint) && hint > 0 ? Math.min(30, Math.max(2, Math.ceil(hint))) : fallback;
  }
  return 30; // A passive cache check, not an upstream collection request.
}
export function coinglassElapsed(seconds: number) {
  const whole = Math.max(0, Math.floor(seconds));
  return whole < 60 ? `${whole} 秒` : whole < 3600 ? `${Math.floor(whole / 60)} 分 ${whole % 60} 秒` : `${Math.floor(whole / 3600)} 小时 ${Math.floor(whole % 3600 / 60)} 分`;
}
export function coinglassQueueAge(bundle: Partial<CoinglassBundle>, now = Date.now() / 1000) {
  if (bundle.queued_at != null && Number.isFinite(bundle.queued_at) && bundle.queued_at > 0 && bundle.queued_at <= now) return Math.floor(now - bundle.queued_at);
  return bundle.queue_age_seconds != null && Number.isFinite(bundle.queue_age_seconds) && bundle.queue_age_seconds >= 0 ? Math.floor(bundle.queue_age_seconds) : null;
}
export function coinglassBundleStatus(bundle: Pick<CoinglassBundle, "status"> & Partial<CoinglassBundle>, now = Date.now() / 1000) {
  if (bundle.match && bundle.match.status !== "EXACT" && ["READY", "PARTIAL", "STALE"].includes(bundle.status)) return "MISMATCH";
  if (bundle.status === "RATE_LIMITED" && bundle.retry_at != null && Number.isFinite(bundle.retry_at)
      && !coinglassRetrySeconds(bundle, now)) return bundle.cache_retained ? "STALE" : bundle.refresh_requested ? "QUEUED" : "DEFERRED";
  return bundle.expires_at != null && bundle.expires_at > 0 && bundle.expires_at <= now && ["READY", "PARTIAL"].includes(bundle.status) ? "STALE" : bundle.status;
}
export function coinglassNumber(value: number | null | undefined, unit?: string) {
  if (value == null || !Number.isFinite(value)) return "—";
  const formatted = value.toLocaleString(undefined, { maximumSignificantDigits: 8 });
  return unit === "PERCENT" ? `${formatted}%` : unit === "USD" ? `$${formatted}` : formatted;
}
export function coinglassSeriesUsable(series: CoinglassSeries, timeframe?: string) {
  return series.unit_status === "VERIFIED" && series.unit === metricUnits[series.id]
    && (!timeframe || series.native_timeframe === timeframe) && ["READY", "PARTIAL", "STALE"].includes(series.status)
    && Array.isArray(series.points) && series.points.some(point => Number.isFinite(point.time)
      && Number.isFinite(point.close_time) && point.close_time > point.time
      && Number.isFinite(point.available_at) && Number.isFinite(point.value));
}
export function usableCoinglassSeries(bundle?: CoinglassBundle) {
  return bundle?.match?.status === "EXACT" && Array.isArray(bundle.series)
    ? bundle.series.filter(series => coinglassSeriesUsable(series, bundle.timeframe)) : [];
}
export function coinglassMetricReadiness(bundle: Partial<CoinglassBundle>, id: CoinglassMetricId, now = Date.now() / 1000) {
  const series = bundle.series?.find(item => item.id === id);
  if (!series || bundle.match?.status !== "EXACT" || !coinglassSeriesUsable(series, bundle.timeframe)) return "UNAVAILABLE";
  const expires = series.expires_at ?? bundle.expires_at;
  return series.status === "STALE" || bundle.status === "STALE" || series.cache_retained
    || (expires != null && Number.isFinite(expires) && expires > 0 && expires <= now) ? "HISTORICAL" : "READY";
}
export function coinglassMetricSummary(bundle: Partial<CoinglassBundle>, now = Date.now() / 1000) {
  const states = (Object.keys(coinglassMetricLabels) as CoinglassMetricId[]).map(id => coinglassMetricReadiness(bundle, id, now));
  return { ready: states.filter(state => state === "READY").length, historical: states.filter(state => state === "HISTORICAL").length, total: states.length };
}
export function parseCoinglassScenario(entry: string, lower: string, upper: string) {
  const raw = [entry, lower, upper];
  if (raw.some(value => !value.trim())) return { error: "请填写观察参考价和完整区间" } as const;
  const values = raw.map(Number);
  if (values.some(value => !Number.isFinite(value) || value <= 0)) return { error: "价格必须是大于 0 的有效数字" } as const;
  if (values[1] > values[2]) return { error: "区间下界不能高于上界" } as const;
  return { scenario: { entry: values[0], target_lower: values[1], target_upper: values[2] } } as const;
}

const reasonLabels: Record<string, string> = {
  CONTRACT_NOT_EXACT: "合约维度未精确匹配", EVENT_INSTRUMENT_ID_MISMATCH: "形态与数据不属于同一币种",
  EVENT_TIMEFRAME_MISMATCH: "形态与数据周期不一致", SNAPSHOT_TIME_INVALID: "数据获取时间不符合本次研判",
  SNAPSHOT_STALE: "数据快照已过期，请更新后重试", SNAPSHOT_NOT_USABLE: "数据快照尚不可用",
  STRUCTURE_TERMINATED_OR_EXPIRED: "结构已终止或过期", FORECAST_NOT_YET_KNOWN: "原预测时间尚未到达",
  NO_USABLE_METRICS: "暂无同口径可用数据", UNIT_UNKNOWN_OR_MISMATCH: "数据单位尚未核实",
};
export function coinglassReason(code: string) { return reasonLabels[code] ?? "数据质量条件未满足"; }
