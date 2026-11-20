export type ShadowState = "WAITING_DELIVERY" | "PENDING" | "OPEN" | "CLOSED" | "SKIPPED" | "EXPIRED";
export type ShadowCohort = "SIGNAL" | "DELIVERED";
export type ShadowTrade = {
  id: string; alert_event_id: string; instrument_id: string; symbol: string; timeframe: string;
  pattern_id: string; pattern_name: string; cohort: ShadowCohort; state: ShadowState; reason_code: string | null;
  signal_at: string; delivered_at: string | null; eligible_at: string | null; entry_at: string | null; exit_at: string | null;
  side: number | string | null; entry_price: number | null; exit_price: number | null;
  stop_price: number | null; target_price: number | null; net_return: number | null;
  gross_return: number | null; raw_gross_return: number | null; slippage_return: number | null; costs_return: number | null; created_at: string; updated_at: string;
  engine_version: string; environment: "SHADOW"; forward_only: boolean; execution_timeframe?: string;
};
export type ShadowSummary = {
  worker?: { state: string; last_cycle_at: string | null; safe_error_code: string | null };
  enabled_at: string | null; environment: string; forward_only: boolean;
  counts: { total: number; waiting_delivery: number; pending: number; open: number; closed: number; skipped: number; expired: number };
  cohorts: Array<{ cohort: ShadowCohort; total: number; closed: number; wins: number; win_rate: number | null; mean_net_return: number | null; mean_gross_return: number | null; mean_costs_return: number | null }>;
  reason_counts: Array<{ reason_code: string; count: number }>;
  by_context?: Array<{ dimension: string; bucket: string; cohort: ShadowCohort; total: number; closed: number; wins: number; win_rate: number | null; mean_net_return: number | null; descriptive_only: true }>;
};
export type ShadowDetail = ShadowTrade & {
  plan: Record<string, unknown>; source_event: Record<string, unknown>; result: Record<string, unknown> | null;
  policy: Record<string, unknown>; events: Array<{ sequence: number; event_type: string; observed_at: string; payload: Record<string, unknown> }>;
  events_total: number; events_has_more: boolean; events_next_offset: number | null;
};
export const shadowStates: Record<ShadowState, string> = { WAITING_DELIVERY: "等待通知送达", PENDING: "等待模拟入场", OPEN: "模拟持仓中", CLOSED: "模拟已结束", SKIPPED: "保留 · 未入场", EXPIRED: "入场已过期" };
export const cohortLabels: Record<ShadowCohort, string> = { SIGNAL: "全部信号观察组", DELIVERED: "通知送达观察组" };
export const shadowEventLabels: Record<string, string> = { CAPTURED: "记录信号", DELIVERED: "首次成功发送", STATE_CHANGED: "模拟状态变化", BAR_CHECKPOINT: "行情计算检查点", OBSERVATION_ENDED: "结束观察" };
export const researchDate = (value: unknown) => typeof value === "string" && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : typeof value === "number" && Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : "—";
export const researchNumber = (value: unknown, percent = false) => typeof value !== "number" || !Number.isFinite(value) ? "—" : percent ? `${(value * 100).toFixed(2)}%` : value.toLocaleString(undefined, { maximumFractionDigits: 8 });
export const directionLabel = (side: unknown) => side === 1 || side === "LONG" || side === "bullish" ? "多" : side === -1 || side === "SHORT" || side === "bearish" ? "空" : "待定";
export function shadowQuery(filters: { cohort: string; state?: string; instrument: string }, page?: number) {
  const params = new URLSearchParams();
  if (filters.cohort === "SIGNAL" || filters.cohort === "DELIVERED") params.set("cohort", filters.cohort);
  if (filters.state && filters.state in shadowStates) params.set("state", filters.state);
  if (filters.instrument) params.set("instrument_id", filters.instrument);
  if (page !== undefined) { params.set("limit", "20"); params.set("offset", String(Math.max(0, Math.floor(page)) * 20)); }
  return params.toString();
}
export const reasonLabel = (reason: string | null | undefined) => ({
  TARGET: "达到目标", STOP: "触及止损", STOP_GAP: "跳空止损", TIME_EXIT: "持有期限结束",
  DATA_GAP: "行情有缺口", SIGNAL_EXPIRED: "信号已过期", EXPIRED: "已过期", PENDING: "等待入场",
  NO_TARGET: "缺少有效目标", NO_STOP: "缺少失效线", UNAVAILABLE: "计划尚不可用",
  DIRECTION_UNAVAILABLE: "方向未明确", WAIT_CONFIRMATION: "等待突破确认",
  TARGET_TOUCH: "触及目标", STOP_TOUCH: "触及止损", TARGET_GAP_CONSERVATIVE: "跳空越过目标，按原目标结算",
  SAME_BAR_STOP_FIRST: "同根触及止盈止损，按止损优先", HOLDING_DEADLINE: "达到最长持有期限",
  STRUCTURE_CLOSE_INVALIDATION: "结构收盘失效", EXECUTION_DATA_GAP: "执行行情有缺口", EXECUTION_RESOLUTION_CHANGED: "执行周期发生变化",
  ENTRY_WINDOW_EXPIRED: "错过入场窗口", SIGNAL_AVAILABLE_AFTER_DEADLINE: "信号可用时已超过期限", GAP_BEYOND_FROZEN_PLAN: "开盘已越过冻结计划",
  CONFIRMATION_DEADLINE: "期限内未完成突破确认", ORIGINAL_CONFIRMATION_RULE_UNAVAILABLE: "原始确认规则不可用",
  WRONG_CONFIRMATION_TIMEFRAME: "确认周期不一致", ORIGINAL_CLOSE_TIMEFRAME_REQUIRED: "需要原周期收盘数据",
} as Record<string, string>)[reason ?? ""] ?? reason ?? "—";
