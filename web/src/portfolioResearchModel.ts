export type PortfolioPolicy = { initial_equity: number; risk_fraction: number; max_positions: number; max_exposure_fraction: number; per_position_fraction: number; from_time?: string | null; to_time?: string | null };
export type PortfolioSource = { id: string; symbol: string; timeframe: string; state: string; created_at: string };
export type PortfolioRun = { id: string; state: string; source_run_ids: string[]; policy: PortfolioPolicy; created_at: string; completed_at: string | null; error: string | null;
  progress?: { phase: string; processed: number; total: number }; report: PortfolioReport | null };
export type PortfolioReport = { engine_version: string; range: { from: string; to: string }; metrics_available: boolean;
  metrics: { trade_count: number; win_rate: number | null; net_pnl: number | null; total_return: number | null; max_drawdown: number | null; sharpe_ratio: number | null; t_statistic: number | null; fees: number; funding: number };
  initial_equity: number; final_cash: number; final_reserved: number; open_position_count: number; open_positions?: PortfolioTrade[]; source_case_count: number;
  accepted_count: number; rejected_count: number; rejection_reasons: Record<string, number>; assumptions: string[]; artifact_digest: string; details_url: string };
export type PortfolioTrade = { source_run_id: string; candidate_id: string; symbol: string; pattern: string; side: number | string;
  entry_time: string; exit_time: string | null; settled_at?: string | null; state: string; notional: number; quantity: number; entry_price: number; exit_price: number | null; net_pnl: number | null; fees: number; funding: number };
export type PortfolioDecision = { source_run_id: string; candidate_id: string; symbol: string; pattern: string; observed_time: string; status: "ACCEPTED" | "REJECTED"; reason: string };
export type PortfolioCurve = { time: string; equity: number | null; cash: number; reserved: number; positions: number; drawdown: number | null };
export type PortfolioPage<T> = { items: T[]; total: number; has_more: boolean; next_offset: number | null };
export const portfolioActive = (state: string | undefined) => state === "QUEUED" || state === "RUNNING";
export const portfolioReason = (reason: string) => ({
  SOURCE_RULE_EXCLUDED: "来源规则未选中，保留候选", OUTSIDE_COMMON_WINDOW: "不在各来源共同研究窗口",
  WINDOW_END_CENSORED: "共同区间结束，尚无可用成交结果",
  SOURCE_MISSED: "来源候选错过入场", SOURCE_UNAVAILABLE: "来源执行数据不可用", SOURCE_CENSORED: "来源结果未能完整观察",
  SOURCE_WAITING_ENTRY: "来源仍等待入场", SOURCE_WAITING_CONFIRMATION: "来源仍等待确认",
  INVALID_SOURCE_TIME: "来源时间顺序无效", INVALID_SOURCE_PLAN: "来源做单计划无效",
  STALE_MARK_NO_NEW_RISK: "估值过期，停止新增风险", CAPITAL_EXHAUSTED: "账户资金耗尽",
  MAX_CONCURRENT_POSITIONS: "达到同时持仓上限", SYMBOL_ALREADY_OPEN_NO_HEDGE: "该币种已有持仓，不叠加或对冲",
  NO_FREE_CAPITAL_OR_EXPOSURE: "没有可用资金或敞口额度", ALLOCATED_AT_RECORDED_ENTRY: "按原始入场时刻分配资金",
  INSUFFICIENT_CASH: "共享资金不足", INSUFFICIENT_BALANCE: "共享资金不足", MAX_POSITIONS: "达到同时持仓上限",
  MAX_EXPOSURE: "达到总名义敞口上限", EXPOSURE_LIMIT: "达到总名义敞口上限", RISK_LIMIT: "风险预算不足",
  DUPLICATE_SIGNAL: "重复信号已保留但不重复建仓", DUPLICATE_CANDIDATE: "同一候选不重复建仓",
  NO_EXECUTION: "没有历史成交条件", SOURCE_NOT_FILLED: "来源候选未成交", OUTSIDE_RANGE: "不在指定区间",
  DATA_UNAVAILABLE: "历史数据不足", CAPITAL_RESERVED: "资金已被其他持仓占用", ACCEPTED: "纳入共享账户",
} as Record<string, string>)[reason] ?? reason;
export function portfolioRequest(values: FormData, sources: PortfolioSource[]) {
  if (!sources.length || sources.length > 8 || new Set(sources.map(source => source.id)).size !== sources.length || sources.some(source => source.state !== "SUCCEEDED"))
    throw new Error("请选择 1–8 个已完成的同源形态回测，不能重复选择");
  const number = (key: string, fallback: number, min: number, max: number, integer = false) => {
    const value = Number(values.get(key) ?? fallback);
    if (!Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error("请检查资金、风险比例和持仓上限");
    return value;
  };
  const from = String(values.get("from_time") ?? ""), to = String(values.get("to_time") ?? "");
  if ((from && !Number.isFinite(Date.parse(from))) || (to && !Number.isFinite(Date.parse(to))) || (from && to && Date.parse(from) >= Date.parse(to))) throw new Error("开始时间必须早于结束时间");
  const exposure = number("max_exposure_percent", 100, 5, 100), perPosition = number("per_position_percent", 20, 1, 100);
  if (perPosition > exposure) throw new Error("单仓名义上限不能超过总名义敞口上限");
  return { source_run_ids: sources.map(source => source.id), initial_equity: number("initial_equity", 10000, 100, 1e8),
    risk_fraction: number("risk_percent", 1, .1, 3) / 100, max_positions: number("max_positions", 5, 1, 20, true),
    max_exposure_fraction: exposure / 100,
    per_position_fraction: perPosition / 100,
    ...(from ? { from_time: new Date(from).toISOString() } : {}), ...(to ? { to_time: new Date(to).toISOString() } : {}) };
}
export function portfolioCurveSegments(rows: PortfolioCurve[], kind: "equity" | "drawdown") {
  const valid = rows.filter(row => Number.isFinite(Date.parse(row.time)) && typeof row[kind] === "number" && Number.isFinite(row[kind]));
  if (!valid.length) return [];
  const times = valid.map(row => Date.parse(row.time)), values = valid.map(row => row[kind] as number);
  const from = Math.min(...times), until = Math.max(...times), bottom = kind === "drawdown" ? Math.min(-.0001, ...values) : Math.min(...values);
  const top = kind === "drawdown" ? Math.max(0, ...values) : Math.max(...values), span = Math.max(1e-12, top-bottom);
  const segments: string[][] = [[]];
  for (const row of rows) {
    const value = row[kind], time = Date.parse(row.time);
    if (value === null || !Number.isFinite(value) || !Number.isFinite(time)) { if (segments.at(-1)!.length) segments.push([]); continue; }
    const x = 12 + (time-from)/Math.max(1, until-from)*776, y = top === bottom ? 90 : 12 + (top-value)/span*156;
    segments.at(-1)!.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return segments.filter(segment => segment.length > 1).map(segment => segment.join(" "));
}
