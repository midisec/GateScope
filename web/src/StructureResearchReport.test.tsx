import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CaseCostFacts, StructureReportView, SummaryMetrics, TimeBlockEvidence } from "./StructureResearchReport";
import type { CaseSummary, StructureReport, TimeBlockComparison } from "./structureResearchModel";
import type { Spec } from "./researchModel";

const empty: CaseSummary = { case_count: 17, sample_count: 0, wins: 0, losses: 0, net_win_rate: null, target_first_rate: null, stop_first_rate: null, timeout_rate: null, mean_net_return: null, mean_net_r: null, profit_factor: null, mean_fees: null, mean_funding: null, mean_win: null, mean_loss: null, sample_sufficient: false, minimum_sample: 20, ambiguous_count: 0, censored_count: 2, pending_count: 15, states: { OPEN: 15, CENSORED: 2 } };
const comparison: TimeBlockComparison = { status: "INSUFFICIENT_SAMPLES", selected_samples: 0, baseline_samples: 0, selected_mean_net_return: null, baseline_mean_net_return: null, mean_net_return_delta: null, selected_net_win_rate: null, baseline_net_win_rate: null, retention_ratio: null, mean_delta_interval: null, selected_mean_interval: null, selected_win_rate_interval: null, block_days: 7, calendar_days: 0, note: "descriptive" };
const report: StructureReport = { engine_version: "structure-research-1", detector_version: "detector-test", artifact_digest: "digest", input_digest: "input-digest", bar_count: 8760, range: { from: "2025-01-01T00:00:00Z", to: "2026-01-01T00:00:00Z" }, spec: { engine_mode: "STRUCTURE" } as Spec, quality_flags: [], data_gaps: 0, structure_research: { summary: empty, baseline: empty, case_count: 17, candidate_counts: { FORMING: 3, INVALIDATED: 2, EXPIRED: 4, CONFIRMED: 8 }, retention_ratio: 1, environment_groups: [], walk_forward: { status: "INSUFFICIENT_HISTORY", embargo_bars: 48, folds: [], sealed_holdout: false, parameters_retrained: false, note: "fixed rules" }, cost_stress: [{ multiplier: 1, summary: empty }, { multiplier: 2, summary: { ...empty, mean_costs: .012 } }], cost_stress_basis: "固定成交路径，仅改变成本假设", portfolio_metrics_available: false, portfolio_note: "不是共享资金组合", stop_note: "冻结止损价触及模拟；同根止盈止损按止损优先", time_block_comparison: comparison } };

describe("same-source result presentation", () => {
  it("distinguishes absent settled observations from a true zero-percent result", () => {
    const absent = renderToStaticMarkup(<SummaryMetrics summary={empty} />);
    expect(absent).toContain("尚无结算样本"); expect(absent).not.toContain("0.00%");
    expect(renderToStaticMarkup(<SummaryMetrics summary={{ ...empty, sample_count: 5, net_win_rate: 0 }} />)).toContain("0.00%");
  });
  it("retains failure and expiry counts and does not restore deployment gates", () => {
    const html = renderToStaticMarkup(<StructureReportView report={report} />);
    for (const text of ["形成失败", "到期未确认", "形成中", "不设部署门槛", "几何评分", "平均总成本", "1.20%", "止损优先"]) expect(html).toContain(text);
    expect(html).not.toContain("夏普比率"); expect(html).not.toContain("通过部署验证");
  });
  it("shows an unavailable block interval honestly", () => {
    const html = renderToStaticMarkup(<TimeBlockEvidence comparison={comparison} />);
    expect(html).toContain("已结算样本不足"); expect(html).toContain("尚未就绪"); expect(html).not.toContain("0.00%");
  });
  it("renders negative-to-positive time-block differences without claiming certified improvement", () => {
    const html = renderToStaticMarkup(<TimeBlockEvidence comparison={{ ...comparison, status: "DESCRIPTIVE_ONLY", selected_samples: 40, baseline_samples: 100, calendar_days: 90, mean_net_return_delta: .004, mean_delta_interval: { lower: -.002, upper: .012 }, selected_win_rate_interval: { lower: .4, upper: .6 } }} />);
    expect(html).toContain("-0.20% ～ 1.20%"); expect(html).toContain("40.00% ～ 60.00%"); expect(html).toContain("未作多次搜索校正");
  });
  it("shows the complete cost split without equating percentages with money", () => {
    const html = renderToStaticMarkup(<CaseCostFacts rawGross={.05} costs={.004} slippage={.002} fees={.001} funding={.001} />);
    expect(html).toContain("价格毛收益"); expect(html).toContain("5.00%"); expect(html).toContain("0.40%"); expect(html).toContain("0.20%"); expect(html).not.toContain("$");
  });
  it("does not present a partially cached request as a complete year", () => {
    const html = renderToStaticMarkup(<StructureReportView report={{ ...report, cache_coverage: { requested_bars: 8760, available_bars: 6000, missing_bars: 2760, ratio: 6000 / 8760 } }} />);
    expect(html).toContain("并非完整请求历史"); expect(html).toContain((2760).toLocaleString());
  });
});
