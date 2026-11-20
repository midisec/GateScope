export type Spec = SelectionSpec & { strategy: string; direction: string; fast_period: number; slow_period: number; lookback: number; adx_threshold: number; stop_atr: number; reward_risk: number; max_holding_bars: number; initial_equity: number; risk_fraction: number; fee_bps: number; slippage_bps: number; funding_bps_per_8h: number };
export type Strategy = { id: string; name: string; rule: string; indicators: string[] };
export type Metrics = { sharpe_ratio?: number | null; t_statistic?: number | null; daily_observations?: number; trade_count: number; win_rate: number | null; net_pnl: number; total_return: number; max_drawdown: number; profit_factor: number | null; mean_trade_return: number | null; fees: number; funding: number };
export type Screening = { status: "PASS" | "REJECTED" | "OBSERVE_ONLY"; shortlisted: boolean; deployable: false; policy_version: string; thresholds_enforced?: boolean; observation_codes?: string[]; basis: string; sample_passed: boolean; minimum_daily_observations: number; minimum_trades: number; daily_observations: number; trade_count: number; reason_codes: string[]; checks: Array<{ gate_code: string; metric: string; label: string; value: number | null; operator: string; threshold: number; passed: boolean; enforced?: boolean }> };
export type Trade = { matched_rules?: string[]; pattern_refs?: string[]; regime?: string; side: number; entry_time: string; exit_time: string; entry: number; exit_price: number; quantity: number; stop: number; target: number; net_pnl: number; net_return: number; exit_reason: string; indicators: Record<string, number>; fees: number; funding: number; signal_available_at: string; signal_time?: string; signal_bar_open_time?: string; exit_bar_open_time?: string; exit_time_basis?: string; holding_bars?: number; holding_seconds?: number };
export type WinProbability = { sample_count: number; wins: number; losses_or_flat: number; win_rate: number | null; interval: { lower: number; upper: number } | null; confidence_level: number; assumption: string };
export type Report = ResearchReportExtras & { probability?: { full: WinProbability; validation: WinProbability }; time_convention?: string; input_bars?: Array<{ open_time: string; close_time: string }>; dimensions?: { first_entry_time: string | null; last_exit_time: string | null; average_holding_seconds: number | null; long_count: number; short_count: number; exposure_fraction: number; average_net_pnl: number | null }; engine_version: string; artifact_digest: string; bar_count: number; spec: Spec; indicator_series: { time: string; values: Record<string, number | null> }[]; full: { diagnostics?: { signal_bars: number; expired_signals: number; risk_rejections: number; entries: number; pending_at_end: number }; metrics: Metrics; trades: Trade[]; equity_curve: { time: string; equity: number; bar_open_time?: string; initial?: boolean }[] }; training: Metrics; validation: Metrics; range: { from: string; to: string; validation_from: string; embargo_bars: number }; evidence: { bootstrap_mean_lcb: number | null; confidence_level: number; tested_hypotheses: number; payoff: { empirical_break_even_win_rate: number | null }; positive_validation_evidence: boolean; reasons: string[] }; quality_flags: string[]; candles: { time: string; open: number; high: number; low: number; close: number }[] };
export type Run = { progress?: { phase?: string; processed_bars?: number; total_bars?: number }; screening?: Screening | null; validation?: Metrics | null; id: string; symbol: string; instrument_id: string; timeframe: string; state: string; error: string | null; spec: Spec; bar_limit: number; from_time: string | null; to_time: string | null; created_at: string; completed_at?: string | null; metrics?: Metrics | null; bar_count?: number | null; report?: Report | null };
export const stateLabel: Record<string, string> = { QUEUED: "排队中", RUNNING: "计算中", SUCCEEDED: "已完成", FAILED: "失败" };
export const exitLabel: Record<string, string> = { STOP: "止损", STOP_GAP: "跳空止损", TARGET: "止盈", TIME_EXIT: "到期平仓", SEGMENT_END: "区间结束" };
export const strategyName: Record<string, string> = { pullback: "趋势回踩再入", volume_breakout: "放量通道突破", momentum: "波动过滤动量", trend: "趋势跟随", breakout: "通道突破", reversion: "震荡回归", composite: "形态 / 多策略组合" };
export const fmt = (value: number | null | undefined, percent = false) => value == null || !Number.isFinite(value) ? "—" : percent ? `${(value * 100).toFixed(2)}%` : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
export const activeRun = (run: Run) => ["QUEUED", "RUNNING"].includes(run.state);
export function initialRun(runs: Run[]): string {
  return (runs.find(activeRun) ?? runs.find(r => r.state === "SUCCEEDED") ?? runs[0])?.id ?? "";
}
export function localInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
export function zeroTradeReason(report: Report): string {
  const d = report.full?.diagnostics;
  if (d?.expired_signals) return `${d.signal_bars} 根产生信号，${d.expired_signals} 个待入场信号在数据可用前已过期。可载入参数选择「历史收盘重建」新建研究回测；旧报告不变。`;
  if (d && !d.signal_bars) return "所选规则、趋势筛选和方向在本区间没有共同入场信号。可检查条件组合，单纯增加 K 线不保证成交。";
  if (report.availability?.assumed) return "历史收盘重建中没有完成成交，请检查所选条件、方向、风险约束和持仓期限。";
  const unknown = report.quality_flags.some(f => ["AVAILABLE_AT_UNKNOWN", "LEGACY_FINALITY_ASSUMED"].includes(f));
  return unknown ? "历史行情含可用时间未知标记，严格回放可能跳过入场。可换用近期采集区间再测试；本报告不会改写历史。" : "本区间没有完成成交。请检查策略条件、数据可用时间和样本长度；零成交不代表策略盈利或亏损。";
}

export type Rule = { id: string; name: string; group: string; direction: string };
export type SelectionSpec = { engine_mode?: "INDICATOR" | "STRUCTURE"; structure_patterns?: string[]; structure_entry_phase?: "FORMED" | "CONFIRMED"; entry_delay_bars?: number; walk_forward_folds?: number; rule_ids?: string[]; match_mode?: "ANY" | "ALL"; regimes?: string[]; pattern_confidence?: number; coincidence_bars?: number; availability_mode?: "AS_RECORDED" | "HISTORICAL_CLOSE" };
export type PatternEvent = { evidence_id: string; name: string; pattern: string; confidence: number; observed_time: string; available_at: string; signal_direction?: string; direction: string; points: { time: number; price: number; label?: string }[]; lines: { from: { time: number; price: number }; to: { time: number; price: number }; style?: string }[] };
export type ResearchReportExtras = { detail_storage?: { version: string; base_url: string; counts: Record<string, number>; summary_only: boolean }; cache_coverage?: { available_bars: number; requested_bars: number; missing_bars: number; internal_missing_bars: number; ratio: number | null }; screening?: Screening; availability?: { mode: string; delayed_bars: number; assumed: boolean }; selection?: { rules: Rule[]; match_mode: string; regimes: string[]; coincidence_bars: number; pattern_confidence: number }; pattern_events?: PatternEvent[]; rule_attribution?: (Rule & { trade_count: number; win_rate: number | null; net_pnl: number })[] };
export const annualIndicatorBars = (timeframe: string) => Math.max(300, Math.ceil(31536000 / ({ "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400 }[timeframe] ?? 3600)));
export const regimeName: Record<string, string> = { UP: "上涨趋势", DOWN: "下跌趋势", RANGE: "震荡", UNKNOWN: "未确定" };
export const selectedRules = (spec: Pick<Spec, "strategy" | "rule_ids">) => spec.strategy === "composite" ? spec.rule_ids ?? [] : [spec.strategy];
export function selectionError(spec: Pick<Spec, "strategy" | "rule_ids" | "match_mode">, catalog: Rule[]): string {
  const ids = selectedRules(spec);
  if (!ids.length) return "请选择至少一项策略或形态";
  if (spec.match_mode === "ALL") {
    const sides = new Set(catalog.filter(r => ids.includes(r.id) && r.direction !== "neutral").map(r => r.direction));
    if (sides.size > 1 || (ids.includes("trend") && ids.includes("reversion"))) return "所选规则方向或趋势条件互斥，请改用任一满足";
  }
  return "";
}
export function tradeCsv(trades: Trade[]): string {
  const rows = [["入场时间", "退出时间（记账）", "方向", "入场价", "退出价", "数量", "止损", "止盈", "净损益", "手续费", "资金成本", "退出原因", "信号可用时间", "退出所在K线开盘", "持仓秒数（记账口径）", "持仓K线数"],
    ...trades.map(t => [t.entry_time, t.exit_time, t.side > 0 ? "多" : "空", t.entry, t.exit_price, t.quantity, t.stop, t.target, t.net_pnl, t.fees, t.funding, exitLabel[t.exit_reason] ?? t.exit_reason, t.signal_available_at ?? "", t.exit_bar_open_time ?? "", t.holding_seconds ?? (Date.parse(t.exit_time) - Date.parse(t.entry_time)) / 1000, t.holding_bars ?? ""])];
  return "\uFEFF" + rows.map(row => row.map(v => `"${String(v).replaceAll('"', '""')}"`).join(",")).join("\r\n");
}
