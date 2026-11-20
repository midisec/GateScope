import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PortfolioCurvePanel, PortfolioTradeRow } from "./PortfolioResearchWorkspace";
import { portfolioCurveSegments, portfolioRequest, type PortfolioCurve, type PortfolioSource } from "./portfolioResearchModel";

const source: PortfolioSource = { id: "source", symbol: "SOL_USDT", timeframe: "1h", state: "SUCCEEDED", created_at: "2026-09-14T00:00:00Z" };
const form = (values: Record<string, string | undefined> = {}) => ({ get: (key: string) => values[key] ?? null }) as FormData;
describe("finite-capital portfolio presentation", () => {
  it("converts human percentages to fractions and keeps every chosen source", () => {
    expect(portfolioRequest(form(), [source, { ...source, id: "source-two" }])).toEqual({ source_run_ids: ["source", "source-two"], initial_equity: 10000, risk_fraction: .01, max_positions: 5, max_exposure_fraction: 1, per_position_fraction: .2 });
    expect(portfolioRequest(form({ risk_percent: "0.1", max_exposure_percent: "50", per_position_percent: "10" }), [source]).risk_fraction).toBe(.001);
  });
  it("rejects unfinished, duplicate or excessive sources and invalid risk policy", () => {
    for (const sources of [[], [source, source], [{ ...source, state: "RUNNING" }], Array.from({ length: 9 }, (_, i) => ({ ...source, id: String(i) }))]) expect(() => portfolioRequest(form(), sources)).toThrow();
    for (const invalid of [{ initial_equity: "99" }, { initial_equity: "Infinity" }, { risk_percent: "5" }, { max_positions: "1.5" }, { max_positions: "21" }, { max_exposure_percent: "10", per_position_percent: "20" }, { from_time: "2026-09-15T00:00", to_time: "2026-09-14T00:00" }]) expect(() => portfolioRequest(form(invalid), [source])).toThrow();
  });
  it("preserves unknown exit and profit rather than showing a zero-result closed trade", () => {
    const html = renderToStaticMarkup(<table><tbody><PortfolioTradeRow trade={{ source_run_id: "source", candidate_id: "candidate", symbol: "SOL_USDT", pattern: "double_bottom", side: "LONG", entry_time: "2026-09-14T00:00:00Z", exit_time: null, state: "OPEN", notional: 1000, quantity: 10, entry_price: 100, exit_price: null, net_pnl: null, fees: .5, funding: 0 }} /></tbody></table>);
    expect(html).toContain("OPEN"); expect(html).toContain("100 → —"); expect(html).toContain("<td>—</td>");
  });
  it("breaks unknown-valued curve segments and distinguishes drawdown from equity", () => {
    const rows: PortfolioCurve[] = [100, 120, null, 108, 110].map((equity, index) => ({ time: `2026-09-14T0${index}:00:00Z`, equity, cash: 100, reserved: 0, positions: 0, drawdown: equity === null ? null : index < 2 ? 0 : equity/120-1 }));
    expect(portfolioCurveSegments(rows, "equity")).toHaveLength(2);
    expect(portfolioCurveSegments(rows, "drawdown")).toHaveLength(2);
    expect(portfolioCurveSegments(rows, "equity")).not.toEqual(portfolioCurveSegments(rows, "drawdown"));
    const html = renderToStaticMarkup(<PortfolioCurvePanel rows={rows} />);
    expect(html).toContain("不是完整资金曲线"); expect(html).toContain("缺失估值处不连线");
  });
});
