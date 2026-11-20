import { describe, expect, it } from "vitest";
import { alignedCurve, alignedIndicator, chartPriceFormat, priceText, drawdownPoints, drawdownSummary, tradeBarTimes, candleUpdateKind } from "./chartModel";
import type { Bar } from "./types";
import type { Report, Trade } from "./researchModel";

const times = Array.from({ length: 6 }, (_, i) => `2026-01-01T0${i}:00:00Z`);
const report = { spec: { initial_equity: 100 }, range: { to: times[5] }, candles: times.slice(0, 5).map(time => ({ time })), full: { equity_curve: [100, 120, 90, 110, 130].map((equity, i) => ({ time: times[i], equity })) } } as Report;
describe("chart temporal and statistical alignment", () => {
  it("updates a live tail without replacing or shifting the historical viewport", () => {
    const bars = times.slice(0,3).map(open_time => ({ open_time, open:"10", high:"12", low:"9", close:"11", volume:"20" })) as Bar[];
    expect(candleUpdateKind(bars, structuredClone(bars))).toBe("none");
    expect(candleUpdateKind(bars, [...bars.slice(0,2), {...bars[2],close:"12"}])).toBe("tail");
    expect(candleUpdateKind(bars, [...bars,{...bars[2],open_time:times[3]}])).toBe("tail");
    expect(candleUpdateKind(bars, [{...bars[0],high:"15"},...bars.slice(1)])).toBe("replace");
    expect(candleUpdateKind(bars, bars.slice(1))).toBe("replace");
    expect(candleUpdateKind([],bars)).toBe("replace");
  });
  it("preserves low-price chart axes instead of rounding them to zero", () => {
    const format = chartPriceFormat([.0000012345]);
    expect(format.precision).toBe(10);
    expect(format.minMove).toBe(1e-10);
    expect(priceText(.0000012345)).toBe("0.0000012345");
    expect(chartPriceFormat([]).precision).toBe(2);
  });
  it("uses running peak and recovers to zero, not total return", () => {
    const points = drawdownPoints(report.full.equity_curve, 100);
    expect(points.map(p => Math.round(p.drawdown * 100) / 100)).toEqual([0, 0, -25, -8.33, 0]);
    const d = drawdownSummary(report)!;
    expect(d.peak.time).toBe(times[1]); expect(d.trough.time).toBe(times[2]); expect(d.recovery?.time).toBe(times[4]);
  });
  it("maps a close-accounted exit onto the preceding candle, not next entry", () => {
    const bars = tradeBarTimes(report, { entry_time: times[1], exit_time: times[3] } as Trade);
    expect(bars.entry).toBe(Date.parse(times[1]) / 1000);
    expect(bars.exit).toBe(Date.parse(times[2]) / 1000);
    const explicit = tradeBarTimes(report, { entry_time: times[1], exit_time: times[3], exit_bar_open_time: times[2] } as Trade);
    expect(explicit).toEqual(bars);
  });
  it("does not snap unrecognized timestamps to an unrelated candle", () => {
    expect(tradeBarTimes(report, { entry_time: "2025-01-01", exit_time: "2027-01-01" } as Trade)).toEqual({ entry: undefined, exit: undefined });
  });
  it("aligns accounting points by candle close without a one-bar shift", () => {
    const points = alignedCurve(report);
    expect(points[0].chartTime).toBe(Date.parse(times[0]) / 1000);
    expect(points[0].equity).toBe(120);
  });
  it("joins indicator values by timestamps and leaves missing/warmup gaps", () => {
    const bars = times.slice(0, 3).map(open_time => ({ open_time }));
    const values = alignedIndicator(bars, [{ open_time: times[2], ready: true, values: { ema: 3 } }, { open_time: times[0], ready: false, values: { ema: 1 } }, { open_time: times[4], ready: true, values: { ema: 8 } }], "ema");
    expect(values.map(p => "value" in p ? p.value : null)).toEqual([null, null, 3]);
  });
});
