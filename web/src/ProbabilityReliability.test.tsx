import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProbabilityReliability, type ProbabilityReliabilityData, type ReliabilityWindow } from "./ProbabilityReliability";

const empty: ReliabilityWindow = { status: "INSUFFICIENT_PREDICTIONS", sample_count: 3, calibration_count: 0, unpredicted_count: 3,
  confirmation_rate: 1 / 3, confirmation_interval: null, formation_rate: 2 / 3, formation_interval: null,
  mean_prediction: null, predicted_cohort_confirmation_rate: null, overconfidence_gap: null, brier: null,
  bins: Array.from({ length: 5 }, (_, index) => ({ lower: index / 5, upper: (index + 1) / 5, sample_count: 0, mean_prediction: null, observed_confirmation_rate: null, interval: null })) };
const data: ProbabilityReliabilityData = { version: "probability-reliability-1", as_of: 1700000000, annual_from: 1600000000, recent_from: 1690000000,
  recent: empty, annual: empty, brier_delta: null, minimum_predictions: 20, interval_method: "WILSON", interval_comparisons: 1000,
  scope: "ALL", comparison: "OVERLAPPING", current_regime_status: "UNAVAILABLE", formation_calibration_status: "UNAVAILABLE", profit_probability: null };

describe("probability reliability presentation", () => {
  it("shows empty predictions as unknown and keeps unpredicted outcomes", () => {
    const html = renderToStaticMarkup(<ProbabilityReliability value={data} phase="FORMING" />);
    expect(html).toContain("有效预测不足 20 个");
    expect(html).toContain("没有原始概率的 3 个样本保留");
    expect(html).toContain("<td>—</td>");
    expect(html).toContain("样本不足 · 暂无区间");
    expect(html).toContain("历史形成率");
    expect(html).toContain("确认率不是盈利概率");
  });
  it("compares the same predicted cohort without calling formation a win rate", () => {
    const ready: ReliabilityWindow = { ...empty, status: "READY", sample_count: 100, calibration_count: 60, unpredicted_count: 40,
      mean_prediction: .8, predicted_cohort_confirmation_rate: .5, brier: .34 };
    const html = renderToStaticMarkup(<ProbabilityReliability value={{ ...data, recent: ready, annual: ready }} phase="FORMED" />);
    expect(html).toContain("100 / 60");
    expect(html).toContain("80.0%");
    expect(html).toContain("50.0%");
    expect(html).toContain("0.340");
    expect(html).not.toContain("有效预测不足 20 个");
    expect(html).toContain("窗口相互重叠");
  });
  it("keeps unmatched legacy environments in overall statistics and discloses fallback", () => {
    const value = { ...data, environment_reference: { key: "se1:UP:NORMAL:NORMAL", as_of: 1700000000,
      matched: empty, known_sample_count: 3, legacy_unlabelled_count: 80, fallback: "OVERALL", confidence_applied: false } };
    const html = renderToStaticMarkup(<ProbabilityReliability value={value} phase="FORMING" />);
    expect(html).toContain("报告末端环境对照");
    expect(html).toContain("同环境有效样本 3");
    expect(html).toContain("匹配样本不足，继续参考整体历史");
    expect(html).toContain("80 个缺少当时环境标签");
    expect(html).not.toContain("自动提升");
  });
  it("shows unresolved and interrupted sampled cases beside resolved sample counts", () => {
    const window = { ...empty, sampled_candidate_count: 18, unresolved_count: 12, censored_count: 3 };
    const html = renderToStaticMarkup(<ProbabilityReliability value={{ ...data, recent: window, annual: window }} phase="FORMING" />);
    expect(html).toContain("待定 12 · 中断排除 3");
    expect(html).toContain("不是所有新候选的最终成功率");
  });
});
