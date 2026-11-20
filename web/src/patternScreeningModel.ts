import type { CompletionProbability, PatternStudy, Phase, StudyRow } from "./patternModel";

export type ScreeningDirections = { bullish: number; bearish: number; neutral: number };
export type PatternScreeningGroup = {
  pattern: string; name: string; family: "chart" | "wave"; group: string;
  count: number; share: number; directions: ScreeningDirections;
  score_min: number | null; score_max: number | null;
};
export type PatternScreeningSummaryData = {
  total: number; scored_count: number; unscored_count: number;
  directions: ScreeningDirections; phase_counts: Partial<Record<Phase, number>>;
  patterns: PatternScreeningGroup[];
};
export type ScreeningReturnPhase = "FORMED" | "CONFIRMED";
export type ScreeningHorizon = 5 | 10 | 20 | 50;
export type ScreeningHistory = {
  report: NonNullable<PatternStudy["report"]> | null;
  state: "AVAILABLE" | "WAITING" | "FAILED" | "VERSION_MISMATCH";
  partial: boolean;
  label: string;
};

const validCount = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value >= 0;
const validRate = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

export function screeningShare(count: number, total: number): number | null {
  return validCount(count) && validCount(total) && total > 0 && count <= total ? count / total : null;
}

export function screeningHistory(study: PatternStudy | undefined, version: string): ScreeningHistory {
  if (!study?.report) return { report: null, state: study?.state === "FAILED" ? "FAILED" : "WAITING", partial: false,
    label: study?.state === "FAILED" ? "历史回放待恢复" : study?.state === "QUEUED" || study?.state === "RUNNING" ? "历史回放中，尚未生成报告" : "等待历史回放" };
  if (!version || study.report.version !== version) return { report: null, state: "VERSION_MISMATCH", partial: false, label: "模型版本不一致，暂不引用历史统计" };
  const partial = !!study.report.partial;
  return { report: study.report, state: "AVAILABLE", partial,
    label: partial ? "阶段结果 · 回放尚未完成" : study.state === "READY" ? "历史回放已完成" : "上次完成的历史报告" };
}

/** A geometric score or empirical rate must never fill an absent probability. */
export function screeningProbability(value: CompletionProbability | undefined | null, partial = false): string {
  const count = value?.sample_count;
  if (!validCount(count)) return partial ? "回放中 · 暂无统计" : "暂无统计";
  if (count === 0) return partial ? "回放中 · 暂无样本" : "无有效样本";
  if (count < 20) return "样本不足 20";
  return validRate(value?.probability) ? `${(value.probability * 100).toFixed(1)}%` : "概率未就绪";
}

export function screeningWinRate(row: StudyRow | undefined, partial = false): string {
  const count = row?.sample_count;
  if (!validCount(count)) return partial ? "回放中 · 暂无统计" : "暂无统计";
  if (count === 0) return partial ? "回放中 · 暂无样本" : "无有效样本";
  if (count < 20) return "样本不足 20";
  return validRate(row?.win_rate) ? `${(row.win_rate * 100).toFixed(1)}%` : "胜率未就绪";
}

export function screeningInterval(value: Pick<CompletionProbability, "sample_count" | "interval"> | undefined | null): string {
  const interval = value?.interval;
  if (!validCount(value?.sample_count) || value.sample_count < 20 || !interval || !validRate(interval.lower)
      || !validRate(interval.upper) || interval.lower > interval.upper) return "—";
  return `${(interval.lower * 100).toFixed(1)}%–${(interval.upper * 100).toFixed(1)}%`;
}

export function screeningPatternHistory(history: ScreeningHistory, pattern: string, phase: ScreeningReturnPhase, horizon: ScreeningHorizon) {
  const report = history.report;
  const formation = report?.completion_rows?.find(row => row.pattern === pattern && row.phase === "FORMING" && row.score_bucket === "ALL");
  const confirmation = report?.completion_rows?.find(row => row.pattern === pattern && row.phase === "FORMED" && row.score_bucket === "ALL");
  return {
    formation: formation?.formation,
    confirmation,
    returns: report?.rows.find(row => row.pattern === pattern && row.phase === phase && row.horizon === horizon),
    supportsForming: report?.catalog?.find(item => item.id === pattern)?.supports_forming !== false,
  };
}
