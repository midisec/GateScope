export type PaperNumber = string | number | null;
export type PaperSnapshot = {
  environment: "PAPER";
  account: { id: string; name: string; base_currency: string; status: string };
  balance: {
    initial_equity: PaperNumber; realized_pnl: PaperNumber; cash_balance: PaperNumber;
    legacy_open_fees: PaperNumber; position_margin: PaperNumber; order_reserved_margin: PaperNumber;
    order_reserved_fees: PaperNumber; available_balance: PaperNumber;
    risk_used: PaperNumber; risk_limit: PaperNumber; notional_used: PaperNumber; notional_limit: PaperNumber;
    unrealized_pnl: PaperNumber; equity: PaperNumber; mark_status: string;
  };
  limitations?: string[];
  execution_health?: { state: string; heartbeat_at: string | null; last_success_at: string | null; age_seconds: number | null; safe_error_code?: string | null; freshness_limit_seconds: number; not_a_hard_realtime_guarantee: true };
  as_of?: string;
};
export type PaperOrder = {
  id: string; account_id: string; instrument_id: string; symbol: string; timeframe: string;
  position_id: string | null; effect: "OPEN" | "CLOSE"; side: "LONG" | "SHORT";
  execution_side: "BUY" | "SELL"; order_type: "MARKET" | "LIMIT"; time_in_force: "IOC" | "GTC" | "FOK";
  status: string; contracts: number; filled_contracts: number; remaining_contracts: number;
  average_fill_price: PaperNumber; limit_price: PaperNumber; stop_price: PaperNumber; take_profit_price: PaperNumber;
  reserved_notional: PaperNumber; reserved_risk: PaperNumber; reserved_fees: PaperNumber;
  reason: string | null; source: string; created_at: string; updated_at: string; expires_at: string;
  fills?: PaperFill[]; events?: PaperOrderEvent[]; fills_has_more?: boolean; events_has_more?: boolean; history_limit?: number;
  fills_next_cursor?: string | null; events_next_cursor?: string | null;
  decision_status?: "LINKED" | "UNASSOCIATED"; decision_snapshot_id?: string | null; decision_snapshot?: PaperDecisionSnapshot | null;
};
export type PaperDecisionSnapshot = { id: string; evidence_digest: string; evidence_json: {
  observed_at: string; captured_at: string; timeframe: string;
  server_evidence: { bar_count: number; last_closed_candle_at: string | null; data_status: string; window_limited: boolean;
    pattern_events: { phase: string; version: string; source_mode: string; record: { latest?: { name?: string; pattern?: string } } }[];
    indicators: Record<string, { open_time: string; value: PaperNumber }[]> };
  client_analysis: { rationale: string; analysis_model: string | null; verification: string;
    coinglass_claims: { metric: string; value: PaperNumber; unit: string; source_at: string; market_scope: string }[] };
} };
export type PaperOrderEvent = { id: string; event_type: string; created_at: string; payload_json: Record<string, unknown> };
export type PaperPreview = {
  environment: "PAPER"; preview_only: true; order_created: false; account: PaperSnapshot;
  market: { spec: { symbol: string; multiplier: string; price_tick: string; min_contracts: string; max_contracts: string; market_max_contracts: string | null };
    book: { source_at: string; captured_at: string; quote_id: string; best_bid: string; best_ask: string } };
  estimate: { requested_contracts: number; filled_contracts: number; remaining_contracts: number; vwap: PaperNumber; fee: PaperNumber; notional: PaperNumber; reason: string; resting: boolean; remainder_cancelled: boolean };
  sizing: { requested_base_quantity: PaperNumber; estimated_base_quantity: PaperNumber; reference_notional: PaperNumber;
    required_margin: PaperNumber; required_fees: PaperNumber; reserved_risk: PaperNumber; estimated_fill_fees: PaperNumber;
    estimated_stop_loss: PaperNumber; max_contracts_by_budget: number; stop_loss_is_guaranteed: false };
  checks: { budget_available: boolean; reasons: string[]; balance_reserved: false; depth_reserved: false };
};
export type PaperPosition = {
  id: string; account_id: string; instrument_id: string; symbol: string; timeframe: string;
  side: "LONG" | "SHORT"; opened_contracts: number; closed_contracts: number; remaining_contracts: number;
  reserved_close_contracts: number; multiplier: PaperNumber; average_entry_price: PaperNumber;
  average_exit_price: PaperNumber; stop_price: PaperNumber; take_profit_price: PaperNumber;
  realized_pnl: PaperNumber; gross_realized_pnl?: PaperNumber; net_realized_pnl?: PaperNumber; fees: PaperNumber; status: "OPEN" | "CLOSED";
  protection_status: string; trigger_reason: string | null; triggered_at: string | null;
  opened_at: string; closed_at: string | null; updated_at: string;
};
export type PaperFill = {
  id: string; order_id: string; position_id: string; symbol?: string; timeframe?: string; effect: "OPEN" | "CLOSE";
  side: "LONG" | "SHORT"; execution_side: "BUY" | "SELL"; contracts: number;
  price: PaperNumber; base_quantity: PaperNumber; notional: PaperNumber; fee: PaperNumber;
  realized_pnl: PaperNumber; reason: string; filled_at: string; quote_id: string;
};
export type PaperPage<T> = { items: T[]; limit: number; offset: number; has_more: boolean; next_offset: number | null; next_cursor?: string | null; snapshot_at?: string; pagination?: "CURSOR" | "OFFSET" };
export type PaperMutation = { environment: "PAPER"; accepted: boolean; replayed: boolean; order: PaperOrder; fills: PaperFill[]; limitations?: string[] };
export type PaperAction = { path: string; body: Record<string, unknown>; label: string };
export const paperIntentKey = (body: Record<string, unknown>) => JSON.stringify(Object.fromEntries(
  Object.entries(body).filter(([key]) => key !== "idempotency_key").sort(([left], [right]) => left.localeCompare(right))));
export function restorePaperAction(userId: string): PaperAction | undefined {
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(`gatescope:paper-pending:${userId}`) ?? "null");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;
    const value = saved as Partial<PaperAction>;
    if (typeof value.path !== "string" || !/^\/api\/v2\/paper\/(?:orders|orders\/[A-Za-z0-9_-]+:cancel|positions\/[A-Za-z0-9_-]+:close)$/.test(value.path)
      || !value.body || typeof value.body !== "object" || Array.isArray(value.body)
      || typeof value.body.idempotency_key !== "string" || !/^[A-Za-z0-9_-]{8,128}$/.test(value.body.idempotency_key)
      || typeof value.label !== "string" || value.label.length > 20) return;
    return value as PaperAction;
  } catch { return; }
}
export function persistPaperAction(userId: string, action: PaperAction | undefined) {
  try {
    const key = `gatescope:paper-pending:${userId}`;
    if (action) sessionStorage.setItem(key, JSON.stringify(action)); else sessionStorage.removeItem(key);
  } catch { /* Browser storage may be disabled; in-memory retry stays available. */ }
}
export const PAPER_PAGE_SIZE = 10;
export const paperEmptyPage = <T,>(): PaperPage<T> => ({ items: [], limit: PAPER_PAGE_SIZE, offset: 0, has_more: false, next_offset: null });
export const paperNumber = (value: PaperNumber | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  if (typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value)) {
    const [integer, fraction] = value.split(".");
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ","), trimmed = fraction?.replace(/0+$/, "");
    return trimmed ? `${grouped}.${trimmed}` : grouped;
  }
  const number = Number(value);
  return number !== 0 && Math.abs(number) < 1e-8 ? number.toPrecision(8) : number.toLocaleString("zh-CN", { maximumFractionDigits: 8 });
};
export const paperTime = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
export const paperStatus = (status: string) => ({
  NEW: "已受理", OPEN: "挂单中", PENDING: "待撮合", ACCEPTED: "已受理", PARTIALLY_FILLED: "部分成交",
  FILLED: "已成交", CANCELLED: "已撤单", CANCELED: "已撤单", EXPIRED: "已到期", REJECTED: "已拒绝", CLOSED: "已平仓",
} as Record<string, string>)[status] ?? status;
export const paperReason = (reason: string) => ({
  FOK_INSUFFICIENT_EXECUTABLE_DEPTH: "盘口不足以全部成交，FOK 已取消", EXECUTABLE_DEPTH_FILLED: "已按可用盘口深度成交",
  SLIPPAGE_CAP: "盘口价格超过滑点上限", LIMIT_NOT_CROSSED: "盘口尚未到达委托限价",
  INSUFFICIENT_EXECUTABLE_DEPTH: "可成交深度不足", INSUFFICIENT_DEPTH: "可成交深度不足",
  USER_CANCELED: "用户撤销剩余委托", OPENING_LOT_EXIT_STARTED: "仓位已开始退出，取消继续加仓",
  CONTRACT_MULTIPLIER_CHANGED: "合约乘数发生变化，已停止撮合", PROTECTION_INVALID_AT_EXECUTION: "当前成交价不再满足止盈止损条件",
  BUDGET_CHANGED_BEFORE_FILL: "成交前可用余额或风险额度发生变化", ACCOUNT_OR_OWNER_INACTIVE: "账户或用户已停用",
  PROTECTIVE_EXIT_PREEMPTED: "止盈止损保护已接管退出", STOP_LOSS: "触发止损", TAKE_PROFIT: "触发止盈", MANUAL: "主动平仓",
  ACTIVE: "监控中", TRIGGERED: "已触发，正在退出", WAITING_MARKET: "等待有效盘口，尚未保证成交", EXECUTION_RETRY: "退出尚未完成，等待重新撮合", COMPLETED: "已完成",
  FRESH: "实时估值", NOT_REQUESTED: "尚未请求估值", UNAVAILABLE_OR_STALE: "缺少有效标记价或报价过期",
  INSUFFICIENT_PAPER_BALANCE: "可用模拟余额不足", ACCOUNT_NOTIONAL_LIMIT: "超过账户名义额度", ACCOUNT_RISK_LIMIT: "超过账户风险额度",
  PAPER_ACCOUNT_UNAVAILABLE: "账户停用或不支持该结算币", UNSUPPORTED_MINIMUM_CONTRACTS_FOR_SAFE_PARTIAL_EXITS: "该合约最小张数暂不支持安全部分退出",
  HEALTHY: "最近周期正常", STALE: "后台心跳延迟", DEGRADED: "后台部分处理异常", STOPPED: "后台已停止", NO_RECENT_SUCCESS: "尚无近期成功周期", UNAVAILABLE: "暂未取得后台状态",
  UNSUPPORTED_FEE_RATE: "当前费用规格不支持", SUBMITTED: "已提交", MATCHED: "完成一次盘口撮合", CANCELED: "剩余委托已撤销",
  EXPIRED: "委托到期", REJECTED: "委托被拒绝", PROTECTION_TRIGGERED: "止盈止损触发", FILL: "产生模拟成交",
  PAPER_ONLY_NO_EXCHANGE_ORDER: "仅模拟资金，不向交易所提交订单", ONE_X_FULL_COLLATERAL_USDT: "1× 全额 USDT 抵押，不提供杠杆",
  TEN_PERCENT_VISIBLE_DEPTH_PARTICIPATION: "每次仅使用可见盘口深度的 10%，避免假设独占全部流动性",
  NO_QUEUE_PRIORITY_OR_OFFLINE_FILL_GUARANTEE: "不模拟真实排队优先级，离线期间不补造保证成交",
  FUNDING_NOT_SETTLED: "暂不结算资金费，盈亏不是实盘完整成本", ACCEPTED_IS_NOT_FILLED: "受理委托不等于已成交",
} as Record<string, string>)[reason] ?? reason;
export const cancellablePaperOrder = (order: PaperOrder) => !["FILLED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"].includes(order.status) && order.remaining_contracts > 0;
export function paperMutationNotice(result: PaperMutation) {
  const prefix = result.replayed ? "已核对原请求" : "模拟委托";
  return `${prefix}：${paperStatus(result.order.status)} · 已成交 ${result.order.filled_contracts}/${result.order.contracts} 张${result.order.reason ? ` · ${paperReason(result.order.reason)}` : ""}`;
}
export function paperListPath(account: string, kind: "orders" | "positions" | "fills", page: number, state?: string) {
  const query = new URLSearchParams({ limit: String(PAPER_PAGE_SIZE), offset: String(page * PAPER_PAGE_SIZE) });
  if (state && state !== "ALL") query.set("state", state);
  else if (kind === "positions") query.set("state", "ALL");
  return `/api/v2/paper/accounts/${encodeURIComponent(account)}/${kind}?${query}`;
}
export function paperCursorListPath(account: string, kind: "orders" | "positions" | "fills", state?: string, cursor?: string) {
  const query = new URLSearchParams({ limit: String(PAPER_PAGE_SIZE), pagination: "CURSOR" });
  if (cursor) query.set("cursor", cursor);
  if (state && state !== "ALL") query.set("state", state); else if (kind === "positions") query.set("state", "ALL");
  return `/api/v2/paper/accounts/${encodeURIComponent(account)}/${kind}?${query}`;
}
export function paperOrderBody(values: FormData, instrumentId: string, accountId: string): Record<string, unknown> {
  if (!instrumentId || !accountId) throw new Error("请先选择账户和合约币种");
  const orderType = String(values.get("order_type") ?? "MARKET");
  const contracts = Number(values.get("contracts")), slippage = Number(values.get("max_slippage_bps") ?? 50);
  const ttl = Number(values.get("ttl_seconds") ?? 3600);
  if (!Number.isSafeInteger(contracts) || contracts < 1 || contracts > 1_000_000_000) throw new Error("合约张数须为 1–1,000,000,000 的正整数，不是币的数量");
  if (!Number.isFinite(slippage) || slippage < 0 || slippage > 100) throw new Error("最大滑点须为 0–100 bps");
  if (!Number.isSafeInteger(ttl) || ttl < 1 || ttl > 86400) throw new Error("委托有效期须为 1–86400 秒");
  const stop = String(values.get("stop_price") ?? ""), target = String(values.get("take_profit_price") ?? "");
  for (const value of [stop, target, ...(orderType === "LIMIT" ? [String(values.get("limit_price") ?? "")] : [])])
    if (!value || !Number.isFinite(Number(value)) || Number(value) <= 0) throw new Error("止损、止盈和限价须为有效正价格");
  return { account_id: accountId, instrument_id: instrumentId, timeframe: String(values.get("timeframe") ?? "15m"),
    side: String(values.get("side") ?? "LONG"), order_type: orderType, contracts,
    time_in_force: String(values.get("time_in_force") ?? "IOC"),
    ...(orderType === "LIMIT" ? { limit_price: String(values.get("limit_price")) } : {}),
    stop_price: stop, take_profit_price: target, max_slippage_bps: String(slippage), ttl_seconds: ttl,
    idempotency_key: crypto.randomUUID() };
}
