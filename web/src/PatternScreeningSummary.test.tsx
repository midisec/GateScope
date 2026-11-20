import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PatternScreeningSummary, PatternScreeningView, ScreeningOverview, ScreeningPatternRow } from "./PatternScreeningSummary";
import { screeningHistory, type PatternScreeningGroup, type PatternScreeningSummaryData } from "./patternScreeningModel";
import type { CompletionRow, PatternStudy, StudyRow } from "./patternModel";

const group: PatternScreeningGroup = { pattern: "double_bottom", name: "双底", family: "chart", group: "反转", count: 6, share: .6, directions: { bullish: 6, bearish: 0, neutral: 0 }, score_min: .8, score_max: .9 };
const summary: PatternScreeningSummaryData = { total: 10, scored_count: 6, unscored_count: 4, directions: { bullish: 6, bearish: 3, neutral: 1 }, phase_counts: { FORMED: 6, FORMING: 4 }, patterns: [group, { ...group, pattern: "double_top", name: "双顶", count: 4, share: .4, directions: { bullish: 0, bearish: 3, neutral: 1 }, score_min: null, score_max: null }] };
const probability = { sample_count: 100, successes: 80, probability: .8, rate: .8, interval: { lower: .6, upper: .9 } };
const study: PatternStudy = { state: "READY", cursor: 100, total: 100, report: { version: "current", bar_count: 100, from_time: 100, to_time: 200, round_trip_cost: .001,
  completion_rows: [{ ...probability, pattern: group.pattern, phase: "FORMING", score_bucket: "ALL", formation: { ...probability, probability: .9 } } as CompletionRow, { ...probability, pattern: group.pattern, phase: "FORMED", score_bucket: "ALL", probability: .7 } as CompletionRow],
  rows: [{ pattern: group.pattern, phase: "CONFIRMED", horizon: 20, sample_count: 100, win_rate: .6, interval: { lower: .4, upper: .8 }, pending: 3, overlap_excluded: 7 } as StudyRow], metadata: { snapshot_at: "2026-09-13T00:00:00Z", quality_flags: [], input_digest: "digest" } } };

describe("pattern screening summary presentation", () => {
  it("shows all matching counts, not the displayed page or a market direction probability", () => {
    const html = renderToStaticMarkup(<ScreeningOverview summary={summary} asOf={100} filterLabel="80–90 分" />);
    expect(html).toContain('aria-label="全部匹配数量">10'); expect(html).toContain("全部匹配，非当前页");
    expect(html).toContain("60.0%"); expect(html).toContain("30.0%"); expect(html).toContain("10.0%");
    expect(html).toContain("不是行情上涨或下跌概率"); expect(html).toContain("已评分 / 未评分");
  });
  it("renders all-score history, counts and intervals without calling it score-filtered performance", () => {
    const html = renderToStaticMarkup(<ScreeningPatternRow item={group} history={screeningHistory(study, "current")} phase="CONFIRMED" horizon={20} onPattern={vi.fn()} />);
    for (const expected of ["90.0%", "70.0%", "60.0%", "n=100", "40.0%–80.0%", "历史参考采用同类全部评分与方向", "未按当前评分区间重新统计", "查看匹配"]) expect(html).toContain(expected);
    expect(html.match(/<button/g)).toHaveLength(1); expect(html).not.toContain("止盈率");
  });
  it("does not invent zero probabilities while waiting for replay, during partial replay or on model mismatch", () => {
    for (const history of [screeningHistory(undefined, "current"), screeningHistory(study, "new"), screeningHistory({ ...study, state: "RUNNING", report: { ...study.report!, partial: true, completion_rows: [], rows: [] } }, "current")]) {
      const html = renderToStaticMarkup(<ScreeningPatternRow item={{ ...group, share: .6 }} history={history} phase="CONFIRMED" horizon={20} onPattern={vi.fn()} />);
      expect(html).not.toContain(">0.0%</dd>"); expect(html).not.toContain("n=0");
    }
  });
  it("marks unsupported forming capabilities instead of presenting a zero forming probability", () => {
    const specific = { ...study, report: { ...study.report!, catalog: [{ id: group.pattern, name: group.name, family: "chart" as const, group: group.group, supports_forming: false }] } };
    const html = renderToStaticMarkup(<ScreeningPatternRow item={group} history={screeningHistory(specific, "current")} phase="CONFIRMED" horizon={20} onPattern={vi.fn()} />);
    expect(html).toContain("不支持形成中预判");
  });
  it("pages type rows by eight but leaves the complete population summary visible", () => {
    const many = { ...summary, total: 26, patterns: Array.from({ length: 13 }, (_, i) => ({ ...group, pattern: `p${i}`, name: `结构${i}`, count: 2, share: 2/26 })) };
    const html = renderToStaticMarkup(<PatternScreeningView summary={many} asOf={100} filterLabel="全部" version="current" study={study} onRetry={vi.fn()} onPattern={vi.fn()} />);
    expect(html.match(/class="screening-pattern-row"/g)).toHaveLength(8);
    expect(html).toContain('aria-label="全部匹配数量">26'); expect(html).toContain("共 13 条"); expect(html).not.toContain("结构8");
    expect(html).toContain("历史参考 · 同类全部评分"); expect(html).toContain("不能相加样本数或平均概率");
  });
  it("exposes loading, retry and empty results without needing any history writes", () => {
    const html = renderToStaticMarkup(<PatternScreeningView summary={{ ...summary, total: 0, patterns: [] }} asOf={null} filterLabel="无匹配" version="current" error="请求超时，请重试" onRetry={vi.fn()} onPattern={vi.fn()} />);
    expect(html).toContain("重新加载"); expect(html).toContain("当前筛选没有匹配结构"); expect(html).toContain('role="alert"');
    const dialog = renderToStaticMarkup(<PatternScreeningSummary summary={summary} params="instrument_id=btc&timeframe=1h" asOf={100} filterLabel="全部" version="current" onClose={vi.fn()} onPattern={vi.fn()} />);
    expect(dialog).toContain("筛选结果详情"); expect(dialog).toContain("读取中…");
  });
});
