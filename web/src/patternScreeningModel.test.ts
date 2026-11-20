import { describe, expect, it } from "vitest";
import { screeningHistory, screeningInterval, screeningPatternHistory, screeningProbability, screeningShare, screeningWinRate } from "./patternScreeningModel";
import type { CompletionProbability, CompletionRow, PatternStudy, StudyRow } from "./patternModel";

const probability: CompletionProbability = { sample_count: 100, successes: 80, rate: .8, probability: .8, interval: { lower: .6, upper: .9 } };
const formation = { ...probability, pattern: "double_bottom", phase: "FORMING", score_bucket: "ALL", formation: { ...probability, probability: .9 } } as CompletionRow;
const confirmation = { ...formation, phase: "FORMED", probability: .7 } as CompletionRow;
const returns = { pattern: "double_bottom", phase: "CONFIRMED", horizon: 20, sample_count: 100, win_rate: .6, interval: null } as StudyRow;
const study: PatternStudy = { state: "READY", cursor: 100, total: 100, report: { version: "current", bar_count: 100, from_time: 100, to_time: 200, round_trip_cost: .001,
  completion_rows: [formation, confirmation], rows: [returns], metadata: { snapshot_at: "2026-09-13T00:00:00Z", quality_flags: [], input_digest: "digest" } } };

describe("screening summary evidence boundaries", () => {
  it("uses count ratios without fabricating percentages for an empty or malformed population", () => {
    expect(screeningShare(6, 10)).toBe(.6); expect(screeningShare(0, 10)).toBe(0);
    for (const [count, total] of [[0, 0], [11, 10], [NaN, 10], [-1, 10], [1, 2.5]]) expect(screeningShare(count, total)).toBeNull();
  });
  it("never substitutes empirical rate for an absent probability or insufficient sample", () => {
    expect(screeningProbability(probability)).toBe("80.0%");
    expect(screeningProbability({ ...probability, probability: 0, rate: 0 })).toBe("0.0%");
    expect(screeningProbability({ ...probability, probability: null })).toBe("概率未就绪");
    expect(screeningProbability({ ...probability, sample_count: 19, probability: 1 })).toBe("样本不足 20");
    expect(screeningProbability({ ...probability, sample_count: 0, probability: 0 })).toBe("无有效样本");
    expect(screeningProbability({ ...probability, sample_count: 0 }, true)).toContain("回放中");
    expect(screeningProbability(undefined)).toBe("暂无统计");
    expect(screeningProbability({ ...probability, probability: Infinity })).toBe("概率未就绪");
  });
  it("does not use incomplete, inverted or low-sample probability intervals", () => {
    expect(screeningInterval(probability)).toBe("60.0%–90.0%");
    expect(screeningInterval({ ...probability, sample_count: 5 })).toBe("—");
    expect(screeningInterval({ ...probability, interval: { lower: .9, upper: .2 } })).toBe("—");
    expect(screeningInterval(undefined)).toBe("—");
  });
  it("selects all-score formation and confirmation separately from the chosen return horizon", () => {
    const history = screeningHistory(study, "current");
    const values = screeningPatternHistory(history, "double_bottom", "CONFIRMED", 20);
    expect(values.formation?.probability).toBe(.9); expect(values.confirmation?.probability).toBe(.7);
    expect(values.returns?.win_rate).toBe(.6);
    expect(screeningPatternHistory(history, "double_bottom", "FORMED", 20).returns).toBeUndefined();
    expect(screeningPatternHistory(history, "double_bottom", "CONFIRMED", 5).returns).toBeUndefined();
    const bucketOnly = { ...study, report: { ...study.report!, completion_rows: [{ ...formation, score_bucket: "HIGH" }] } };
    expect(screeningPatternHistory(screeningHistory(bucketOnly, "current"), "double_bottom", "CONFIRMED", 20).formation).toBeUndefined();
  });
  it("rejects mismatched model reports and preserves incomplete or old-snapshot labels", () => {
    expect(screeningHistory(study, "new")).toMatchObject({ state: "VERSION_MISMATCH", report: null });
    expect(screeningHistory(study, "")).toMatchObject({ state: "VERSION_MISMATCH", report: null });
    expect(screeningHistory({ ...study, state: "RUNNING", report: { ...study.report!, partial: true } }, "current")).toMatchObject({ state: "AVAILABLE", partial: true });
    expect(screeningHistory({ ...study, state: "RUNNING" }, "current").label).toContain("上次完成");
    expect(screeningHistory({ ...study, state: "RUNNING", report: null }, "current").label).toContain("尚未生成报告");
    expect(screeningHistory({ ...study, state: "FAILED", report: null }, "current").state).toBe("FAILED");
  });
  it("keeps absent, insufficient and actual zero net-win rates distinct", () => {
    expect(screeningWinRate(undefined)).toBe("暂无统计");
    expect(screeningWinRate({ ...returns, sample_count: 0 }, true)).toContain("回放中");
    expect(screeningWinRate({ ...returns, sample_count: 1, win_rate: 1 })).toBe("样本不足 20");
    expect(screeningWinRate({ ...returns, win_rate: 0 })).toBe("0.0%");
    expect(screeningWinRate(returns)).toBe("60.0%");
  });
});
