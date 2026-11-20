import type { Spec } from "./researchModel";

export type CaseSummary = {
  case_count: number; sample_count: number; wins: number; losses: number; net_win_rate: number | null;
  target_first_rate: number | null; stop_first_rate: number | null; timeout_rate: number | null;
  mean_net_return: number | null; mean_net_r: number | null; profit_factor: number | null;
  mean_fees: number | null; mean_funding: number | null; mean_win: number | null; mean_loss: number | null;
  mean_raw_gross_return?: number | null; mean_slippage?: number | null; mean_costs?: number | null;
  sample_sufficient: boolean; minimum_sample: number; ambiguous_count: number; censored_count: number;
  pending_count: number; states: Record<string, number>;
};
export type StructureFold = { fold: number; train_from: number; train_to: number; test_from: number; test_to: number; embargo_bars: number; training: CaseSummary; validation: CaseSummary; baseline: CaseSummary; parameters_frozen: boolean; retention_ratio: number | null };
export type ResearchInterval = { lower: number; upper: number } | null;
export type TimeBlockComparison = { status: string; selected_samples: number; baseline_samples: number; selected_mean_net_return: number | null; baseline_mean_net_return: number | null; mean_net_return_delta: number | null; selected_net_win_rate: number | null; baseline_net_win_rate: number | null; retention_ratio: number | null; mean_delta_interval: ResearchInterval; selected_mean_interval: ResearchInterval; selected_win_rate_interval: ResearchInterval; block_days: number; calendar_days: number; confidence_level?: number; note: string };
export type StructureReport = {
  engine_version: string; detector_version: string; artifact_digest: string; bar_count: number; range: { from: string; to: string }; spec: Spec;
  quality_flags: string[]; data_gaps: number; input_digest: string;
  cache_coverage?: { requested_bars: number; available_bars: number; missing_bars: number; ratio: number };
  structure_research: {
    summary: CaseSummary; baseline: CaseSummary; case_count: number; candidate_counts: Record<string, number>; retention_ratio: number | null;
    time_block_comparison?: TimeBlockComparison;
    environment_groups: Array<{ dimension: string; value: string; summary: CaseSummary; baseline: CaseSummary }>;
    walk_forward: { status: string; embargo_bars: number; folds: StructureFold[]; sealed_holdout: false; parameters_retrained: false; note: string };
    cost_stress: Array<{ multiplier: number; summary: CaseSummary }>; cost_stress_basis: string; portfolio_metrics_available: false; portfolio_note: string; stop_note: string;
  };
};
export type StructureRun = { id: string; symbol: string; timeframe: string; instrument_id: string; state: string; error: string | null; created_at: string; spec: Spec; bar_limit: number; progress?: { processed_bars: number; total_bars: number; phase: string; through_time?: number }; report?: StructureReport | null };
export type StructureCase = { candidate_id: string; pattern: string; observed_time: number; selected: boolean; baseline: boolean; selection_reasons: string[]; environment: Record<string, unknown>; case: { state: string; reason: string | null; plan: Record<string, unknown>; entry_time: number | null; entry_price: number | null; exit_time: number | null; exit_price: number | null; net_return: number | null; net_r: number | null; fees: number | null; funding: number | null; raw_gross_return?: number | null; slippage_return?: number | null; costs_return?: number | null; ambiguous?: boolean } };
export const structureCaseStates: Record<string, string> = { WAITING_CONFIRMATION: "等待突破确认", WAITING_ENTRY: "等待模拟入场", OPEN: "模拟持仓中", TARGET: "目标先触及", STOP: "止损先触及", TIMEOUT: "到期结束", MISSED: "错过入场", UNAVAILABLE: "计划不可用", CENSORED: "观测中断 / 区间结束" };
export const phaseLabels: Record<string, string> = { FORMING: "形成中", FORMED: "已形成", CONFIRMED: "已确认", INVALIDATED: "形成失败", EXPIRED: "到期未确认", CENSORED: "观测中断" };
export const environmentDimensions: Record<string, string> = { trend: "趋势环境", volatility: "波动环境", volume: "成交量环境", higher_alignment: "高周期方向", score_bucket: "几何评分" };
export const environmentValue = (value: unknown) => ({ UP: "上涨", DOWN: "下跌", RANGE: "震荡", HIGH: "高", NORMAL: "正常", LOW: "低", MID: "中", UNSCORED: "未评分", UNKNOWN: "未就绪", ALIGNED: "方向一致", OPPOSED: "方向相反" } as Record<string, string>)[String(value)] ?? String(value ?? "—");
export function progressPercent(progress: StructureRun["progress"]): number | null {
  return progress && progress.total_bars > 0 && Number.isFinite(progress.processed_bars) ? Math.max(0, Math.min(100, progress.processed_bars / progress.total_bars * 100)) : null;
}
export const selectionReason = (value: string) => ({ PATTERN_NOT_SELECTED: "未选择此形态", DIRECTION_NOT_SELECTED: "方向不匹配", BELOW_GEOMETRY_FILTER: "低于所选几何评分", REGIME_NOT_SELECTED: "不在所选趋势环境" } as Record<string, string>)[value] ?? value;
