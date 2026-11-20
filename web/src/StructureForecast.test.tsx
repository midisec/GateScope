import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { StructureForecast } from "./StructureForecast";
import type { PatternEvent } from "./patternModel";

function event(): PatternEvent {
  return {
    id: "wave", evidence_id: "original", pattern: "elliott_impulse_bull", name: "上升五浪",
    group: "波浪", phase: "FORMING", confidence: null, direction: "bullish", signal_direction: "neutral",
    start_time: 0, end_time: 900, points: [], lines: [], stage: "形成中", note: "",
    forecast: { id: "forecast", kind: "NEXT_PIVOT", issued_at: 1000, deadline_time: 2000,
      source: "THEORY_ONLY", guideline_fit: "STRUCTURAL_FALLBACK", history_count: 0,
      status: "PENDING", condition: "第三浪不得最短", confirmation_closes: 2, confirmation: [],
      target: { price: 128, lower: 125.01, upper: 131, theory_price: 128, label: "第五浪候选区" } },
  };
}

describe("wave forecast evidence labels", () => {
  it("distinguishes a structural fallback from a Fibonacci or empirical prediction", () => {
    const html = renderToStaticMarkup(<StructureForecast event={event()} compact />);
    expect(html).toContain("结构约束候选区 · 非斐波那契目标");
    expect(html).toContain("n=0");
    expect(html).not.toContain("按历史样本修正");
  });
  it("retains the meaning of legacy frozen forecasts", () => {
    const legacy = event(); delete legacy.forecast!.guideline_fit;
    const html = renderToStaticMarkup(<StructureForecast event={legacy} compact />);
    expect(html).toContain("发布时使用理论区间");
    expect(html).not.toContain("非斐波那契目标");
  });
  it("does not fill in absent historical predictions", () => {
    const missing = event(); delete missing.forecast;
    expect(renderToStaticMarkup(<StructureForecast event={missing} />)).toContain("不能事后补写");
  });
});
