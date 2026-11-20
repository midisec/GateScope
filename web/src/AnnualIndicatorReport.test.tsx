import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { annualIndicatorBars, type Report } from "./researchModel";
import { drawdownPoints } from "./chartModel";
import { ReportDimensions } from "./ReportDimensions";

describe("annual indicator report facts", () => {
  it("uses a real year for every supported source period", () => {
    expect(annualIndicatorBars("1m")).toBe(525600);
    expect(annualIndicatorBars("5m")).toBe(105120);
    expect(annualIndicatorBars("15m")).toBe(35040);
    expect(annualIndicatorBars("1h")).toBe(8760);
    expect(annualIndicatorBars("4h")).toBe(2190);
    expect(annualIndicatorBars("1d")).toBe(365);
  });
  it("does not reset the global drawdown peak when loading a later chart page", () => {
    const frozen = [{ time: "2026-01-01", equity: 900, peak_equity: 1200 },
      { time: "2026-01-02", equity: 1000, peak_equity: 1200 }];
    const points = drawdownPoints(frozen, 1000);
    expect(points[0].drawdown).toBe(-25);
    expect(points[1].drawdown).toBeCloseTo(-16.666666);
  });
  it("uses full-report dimensions instead of treating a paged empty array as no trades", () => {
    const report = { full: { trades: [], metrics: { net_pnl: 12 } },
      range: { validation_from: "2026-01-01" }, dimensions: { first_entry_time: "2025-01-01",
        last_exit_time: "2025-12-31", average_holding_seconds: 3600,
        long_count: 121, short_count: 83, average_net_pnl: 2.34 } } as unknown as Report;
    const html = renderToStaticMarkup(<ReportDimensions report={report} />);
    expect(html).not.toContain("无成交");
    expect(html).toContain("121 / 83");
    expect(html).toContain("2.34");
  });
});
