import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EventReturnReference } from "./PatternEventDetail";
import type { PatternEvent, PatternStudy, StudyRow } from "./patternModel";

const event = { pattern: "double_bottom" } as PatternEvent;
const row = { pattern: event.pattern, phase: "CONFIRMED", horizon: 20, sample_count: 80, win_rate: .625, mean_net_return: .02, interval: { lower: .45, upper: .77 } } as StudyRow;
const study = { state: "READY", report: { version: "current", rows: [row], round_trip_cost: .001, from_time: 100, to_time: 200 } } as PatternStudy;
const render = (value?: PatternStudy, horizon = 20) => renderToStaticMarkup(<EventReturnReference event={event} study={value} version="current" horizon={horizon} />);

describe("individual structure historical return reference", () => {
  it("labels historical returns independently of the current geometry range", () => {
    const html = render(study);
    expect(html).toContain("62.5%");
    expect(html).toContain("同类全部评分");
    expect(html).toContain("不按当前评分区间或方向细分");
    expect(html).toContain("不是止盈止损成交胜率");
  });
  it("does not borrow another horizon or invent empty statistics", () => {
    expect(render(study, 50)).not.toContain("62.5%");
    expect(render(study, 50)).toContain("暂无统计");
    expect(render()).toContain("暂无收益统计");
  });
  it("rejects old-model historical reports", () => {
    const html = render({ ...study, report: { ...study.report!, version: "old" } });
    expect(html).toContain("旧模型历史报告");
    expect(html).not.toContain("62.5%");
  });
  it("retains actual zero win rate when nonempty samples all lost", () => {
    const html = render({ ...study, report: { ...study.report!, rows: [{ ...row, win_rate: 0 }] } });
    expect(html).toContain("0.0%");
  });
  it.each([1, 19, 20])("uses the same minimum sample rule as the summary at n=%i", sample_count => {
    const html = render({ ...study, report: { ...study.report!, rows: [{ ...row, sample_count, win_rate: 1, interval: { lower: .9, upper: 1 } }] } });
    expect(html).toContain(`<dt>有效样本</dt><dd>${sample_count}</dd>`);
    if (sample_count < 20) {
      expect(html).toContain("样本不足 20");
      expect(html).not.toContain("100.0%");
      expect(html).toContain("<dt>95% 校正区间</dt><dd>—</dd>");
    } else {
      expect(html).toContain("<dt>历史净胜率</dt><dd>100.0%</dd>");
      expect(html).toContain("90.0%–100.0%");
      expect(html).not.toContain("样本不足 20");
    }
  });
  it("distinguishes an unknown sample count from an observed empty cohort", () => {
    const unknown = render({ ...study, report: { ...study.report!, rows: [{ ...row, sample_count: null } as unknown as StudyRow] } });
    expect(unknown).toContain("暂无统计");
    expect(unknown).toContain("<dt>有效样本</dt><dd>—</dd>");
    expect(unknown).not.toContain("62.5%");
    const empty = render({ ...study, report: { ...study.report!, rows: [{ ...row, sample_count: 0, win_rate: 0 }] } });
    expect(empty).toContain("无有效样本");
    expect(empty).toContain("<dt>有效样本</dt><dd>0</dd>");
    expect(empty).not.toContain("<dt>历史净胜率</dt><dd>0.0%</dd>");
  });
});
