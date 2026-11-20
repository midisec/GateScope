import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { confidenceLabel, confidenceParams, parseConfidenceRange, PatternConfidenceFilter } from "./PatternConfidenceFilter";

describe("confidence range filtering", () => {
  it("preserves zero and inclusive endpoints in API units", () => {
    const range = parseConfidenceRange("0", "100");
    expect(range).toEqual({ min: 0, max: 1 });
    expect(confidenceParams(range)).toBe("confidence_min=0&confidence_max=1");
    expect(parseConfidenceRange("70.5", "85")).toEqual({ min: .705, max: .85 });
  });
  it("keeps missing bounds separate from a zero score", () => {
    expect(confidenceParams(parseConfidenceRange("", ""))).toBe("");
    expect(confidenceParams(parseConfidenceRange("", "0"))).toBe("confidence_max=0");
    expect(confidenceLabel({})).toContain("含未评分");
    expect(confidenceLabel({ min: .7 })).toBe("70–100 分");
  });
  it("supports unknown scores without assigning them a numeric zero", () => {
    expect(confidenceParams({ unscoredOnly: true })).toBe("unscored_only=true");
    expect(confidenceLabel({ unscoredOnly: true })).toBe("仅未评分");
  });
  it.each([["90", "80"], ["-1", "100"], ["0", "101"], ["NaN", "100"], ["0", "Infinity"]])("rejects invalid %s–%s without a request", (lower, upper) => {
    expect(() => parseConfidenceRange(lower, upper)).toThrow();
  });
  it("exposes accessible controls and immediate loading feedback", () => {
    const html = renderToStaticMarkup(<PatternConfidenceFilter value={{}} busy onApply={vi.fn()} onReset={vi.fn()} />);
    expect(html).toContain('aria-label="置信度筛选"');
    expect(html).toContain('aria-label="最低评分"');
    expect(html).toContain('aria-label="最高评分"');
    expect(html).toContain("筛选中…");
    expect(html).toContain("几何评分 · 非胜率");
  });
});
