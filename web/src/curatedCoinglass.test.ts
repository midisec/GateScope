import { describe, expect, it } from "vitest";
import {
  buildCoinglassChartStudies, coinglassChartMetricStatus, COINGLASS_CHART_CATALOG,
  type CoinglassChartMetricId,
} from "./curatedCoinglass";
import type { CoinglassBundle, CoinglassMetricId, CoinglassPoint, CoinglassSeries } from "./coinglassModel";
import type { Bar } from "./types";

const START = Date.parse("2026-09-15T00:00:00Z") / 1000;
const NOW = START + 182;
const iso = (time: number) => new Date(time * 1000).toISOString();
function bar(index: number, extra: Partial<Bar> = {}): Bar {
  return { open_time: iso(START + index * 60), close_time: iso(START + (index + 1) * 60),
    available_at: iso(START + (index + 1) * 60), is_final: true, revision: 1,
    open: "100", high: "101", low: "99", close: "100", volume: "10", quality_flags: [], ...extra };
}
function point(index: number, value = 100, extra: Partial<CoinglassPoint> = {}): CoinglassPoint {
  return { time: START + index * 60, close_time: START + (index + 1) * 60,
    available_at: START + (index + 1) * 60, value, ...extra };
}
function source(id: CoinglassMetricId = "oi", extra: Partial<CoinglassSeries> = {}): CoinglassSeries {
  return { id, label: id, unit: id === "funding" ? "PERCENT" : id === "lsr" ? "RATIO" : "USD",
    unit_status: "VERIFIED", native_timeframe: "1m", population: "TEST", status: "READY", quality_flags: [],
    points: [point(0, 100), point(1, 110), point(2, 99)], ...extra };
}
function bundle(series = [source()], extra: Partial<CoinglassBundle> = {}): CoinglassBundle {
  return { status: "READY", instrument_id: "SOL_USDT", timeframe: "1m", scope: null,
    match: { status: "EXACT" }, as_of: START + 180, fetched_at: NOW, expires_at: NOW + 60, series, ...extra };
}
const bars = [bar(0), bar(1), bar(2)];
const plan = (data = bundle(), id: CoinglassChartMetricId = "oi", rows = bars, now = NOW) =>
  buildCoinglassChartStudies(data, [id], rows, "1m", now, "SOL_USDT")[0];

describe("curated Coinglass chart plans", () => {
  it("offers five native metrics and one clearly named non-flow derivative", () => {
    expect(COINGLASS_CHART_CATALOG).toHaveLength(6);
    const derivative = COINGLASS_CHART_CATALOG.find(item => item.id === "oi_change")!;
    expect(derivative.title).toBe("OI美元估值变化率");
    expect(derivative.description).toContain("不是真实资金流");
    expect(derivative.type).toBe("histogram");
  });
  it("calculates only adjacent closed OI changes without manufacturing the first value", () => {
    const output = plan(bundle(), "oi_change");
    expect(output.status).toBe("READY");
    expect(output.data).toEqual([{ time: START },
      { time: START + 60, value: 10, actualValue: 10, color: "#22c997" },
      { time: START + 120, value: -10, actualValue: -10, color: "#f0526e" }]);
    expect(output.latest).toMatchObject({ value: -10, plotValue: -10, time: START + 120 });
    expect(output.baseline).toBe(0);
  });
  it("retains actual zeros but does not derive from zero or negative previous OI", () => {
    const zero = plan(bundle([source("oi", { points: [point(0, 100), point(1, 0), point(2, 30)] })]), "oi_change");
    expect(zero.data.map(item => item.value)).toEqual([undefined, -100, undefined]);
    expect(plan(bundle([source("oi", { points: [point(0, -10), point(1, 10)] })]), "oi_change").validPoints).toBe(0);
    expect(plan(bundle([source("oi", { points: [point(0, 0), point(1, 10)] })]), "oi_change").status).toBe("UNAVAILABLE");
  });
  it("does not bridge a missing native CG period", () => {
    const data = bundle([source("oi", { points: [point(0, 100), point(2, 120)] })]);
    expect(plan(data).data.map(item => item.value)).toEqual([100, undefined, 120]);
    expect(plan(data).segments.map(segment => segment.map(item => item.time))).toEqual([[START], [START + 120]]);
    expect(plan(data, "oi_change").validPoints).toBe(0);
  });
  it("splits sparse chart bars without inserting any new global chart times", () => {
    const output = plan(bundle(), "oi", [bars[0], bars[2]]);
    expect(output.data.map(item => item.time)).toEqual([START, START + 120]);
    expect(output.segments.map(segment => segment.length)).toEqual([1, 1]);
    expect(plan(bundle(), "oi_change", [bars[0], bars[2]]).validPoints).toBe(0);
  });
  it("keeps chart order deterministic and never shifts a provider point to the nearest candle", () => {
    const data = bundle([source("oi", { points: [point(0, 10, { time: START + 1, close_time: START + 61 }), point(2, 30)] })]);
    expect(plan(data, "oi", [...bars].reverse()).data.map(item => item.value)).toEqual([undefined, undefined, 30]);
    expect(plan(data).data.map(item => item.time)).toEqual(bars.map(item => Date.parse(item.open_time) / 1000));
  });
  it.each([
    { is_final: false }, { is_final: undefined }, { close_time: undefined },
    { close_time: iso(START + 181) }, { available_at: iso(NOW + 1) },
  ])("does not plot unconfirmed, malformed or unavailable chart bars: %j", change => {
    const output = plan(bundle(), "oi", [bars[0], bars[1], bar(2, change)]);
    expect(output.data.at(-1)).toEqual({ time: START + 120 });
    expect(output.validPoints).toBe(2);
  });
  it("rejects future, malformed and unavailable CG points", () => {
    for (const change of [{ close_time: START + 181 }, { available_at: NOW + 1 },
      { available_at: START + 179 }, { value: Infinity }, { value: NaN }, { value: -1 }]) {
      expect(plan(bundle([source("oi", { points: [point(2, 20, change)] })])).data.at(-1)).toEqual({ time: START + 120 });
    }
    expect(plan(bundle([source("oi", { points: [point(3)] })]), "oi", [bar(3)]).validPoints).toBe(0);
  });
  it("quarantines duplicated source timestamps and duplicated chart bars", () => {
    const duplicated = bundle([source("oi", { points: [point(0), point(1), point(1, 999), point(1, 20), point(2)] })]);
    expect(plan(duplicated).data[1]).toEqual({ time: START + 60 });
    expect(plan(bundle(), "oi", [bars[0], bars[1], { ...bars[1] }, bars[2]]).data[1]).toEqual({ time: START + 60 });
    expect(plan(bundle([source(), source()])).validPoints).toBe(0);
  });
  it("shares one USD pane for real long/short liquidations and preserves nonnegative actual amounts", () => {
    const data = bundle([source("liquidation_long", { points: [point(2, 200)] }),
      source("liquidation_short", { points: [point(2, 350)] })]);
    const [long, short] = buildCoinglassChartStudies(data, ["liquidation_long", "liquidation_short"], bars, "1m", NOW);
    expect(long.paneId).toBe(short.paneId);
    expect(long.type).toBe("histogram");
    expect(short.type).toBe("histogram");
    expect(long.latest).toMatchObject({ value: 200, plotValue: 200 });
    expect(short.latest).toMatchObject({ value: 350, plotValue: -350 });
    expect(short.data.at(-1)).toMatchObject({ value: -350, actualValue: 350 });
    expect(short.title).toContain("向下镜像");
    expect(short.description).toContain("金额非负");
  });
  it("keeps percent-point funding and ratio neutral baselines in their own panes", () => {
    const data = bundle([source("funding", { points: [point(0, 0), point(1, -.01), point(2, .01)] }),
      source("lsr", { points: [point(2, 1.25)] })]);
    const [funding, ratio] = buildCoinglassChartStudies(data, ["funding", "lsr"], bars, "1m", NOW);
    expect(funding.data.map(item => item.value)).toEqual([0, -.01, .01]);
    expect(funding.baseline).toBe(0);
    expect(ratio.baseline).toBe(1);
    expect(funding.paneId).not.toBe(ratio.paneId);
  });
  it("makes negative liquidation data missing, not a valid mirrored amount", () => {
    expect(plan(bundle([source("liquidation_short", { points: [point(2, -10)] })]), "liquidation_short").validPoints).toBe(0);
    const zero = plan(bundle([source("liquidation_short", { points: [point(2, 0)] })]), "liquidation_short");
    expect(zero.latest?.plotValue).toBe(0);
    expect(Object.is(zero.latest?.plotValue, -0)).toBe(false);
  });
  it("rejects unknown units, mismatched native periods and wrong chart contracts", () => {
    for (const extra of [{ unit: "COIN" }, { unit_status: "UNKNOWN" as const }, { native_timeframe: "5m" }])
      expect(plan(bundle([source("oi", extra)])).validPoints).toBe(0);
    expect(plan(bundle([source()], { instrument_id: "BTC_USDT" })).validPoints).toBe(0);
    expect(plan(bundle([source()], { match: { status: "AMBIGUOUS" } })).validPoints).toBe(0);
    expect(buildCoinglassChartStudies(bundle(), ["oi"], bars, "5m", NOW)[0].validPoints).toBe(0);
  });
  it("deduplicates selected metrics and handles absent data without zero placeholders", () => {
    expect(buildCoinglassChartStudies(bundle(), [], bars, "1m", NOW)).toEqual([]);
    expect(buildCoinglassChartStudies(bundle(), ["oi", "oi"], bars, "1m", NOW)).toHaveLength(1);
    const missing = buildCoinglassChartStudies(undefined, ["oi"], bars, "1m", NOW, "SOL_USDT")[0];
    expect(missing.status).toBe("UNAVAILABLE");
    expect(missing.statusReason).toBe("按需读取");
    expect(missing.latest).toBeNull();
    expect(missing.segments).toEqual([]);
    expect(missing.data).toEqual(bars.map(item => ({ time: Date.parse(item.open_time) / 1000 })));
  });
});

describe("curated Coinglass freshness", () => {
  it("uses absolute expiry without resetting it on render", () => {
    const data = bundle();
    expect(coinglassChartMetricStatus(data, "oi", NOW).status).toBe("READY");
    expect(coinglassChartMetricStatus(data, "oi", data.expires_at).status).toBe("HISTORICAL");
    expect(plan(data, "oi", bars, data.expires_at).validPoints).toBe(3);
  });
  it.each([
    { expires_at: 0 }, { expires_at: NaN }, { fetched_at: 0 }, { fetched_at: NOW + 1 },
    { fetched_at: START + 100 }, { cache_retained: true }, { status: "STALE" },
  ])("does not default unverifiable or stale bundle clocks to fresh: %j", change => {
    expect(coinglassChartMetricStatus(bundle([source()], change), "oi", NOW).status).toBe("HISTORICAL");
  });
  it("honors per-metric age and retained state independently from fresh bundle clocks", () => {
    for (const change of [{ expires_at: NOW - 1 }, { fetched_at: NOW + 1 }, { cache_retained: true }, { status: "STALE" }])
      expect(coinglassChartMetricStatus(bundle([source("oi", change)]), "oi", NOW).status).toBe("HISTORICAL");
  });
  it("does not let another metric's expired cache label a newly collected metric stale", () => {
    const mixed = bundle([
      source("oi", { fetched_at: NOW, expires_at: NOW + 60 }),
      source("funding", { fetched_at: NOW - 100, expires_at: NOW - 1, status: "STALE", cache_retained: true }),
    ], { status: "STALE", cache_retained: true, fetched_at: NOW - 100, expires_at: NOW - 1 });
    expect(coinglassChartMetricStatus(mixed, "oi", NOW).status).toBe("READY");
    expect(coinglassChartMetricStatus(mixed, "oi_change", NOW).status).toBe("READY");
    expect(coinglassChartMetricStatus(mixed, "funding", NOW).status).toBe("HISTORICAL");
    expect(coinglassChartMetricStatus({ ...mixed, series: [source()] }, "oi", NOW).status).toBe("HISTORICAL");
  });
  it("does not claim collection preceded the availability of its newest native sample", () => {
    const value = bundle([source("oi", { points: [point(2, 100, { available_at: NOW })], fetched_at: NOW - 1, expires_at: NOW + 60 })]);
    expect(coinglassChartMetricStatus(value, "oi", NOW).status).toBe("HISTORICAL");
  });
  it("rejects invisible future points and flags stale native time despite long cache TTL", () => {
    const old = bundle([source("oi", { points: [point(0)] })], { expires_at: NOW + 100000 });
    expect(coinglassChartMetricStatus(old, "oi", NOW).status).toBe("HISTORICAL");
    expect(coinglassChartMetricStatus(bundle(), "oi", NaN).status).toBe("UNAVAILABLE");
    expect(coinglassChartMetricStatus(bundle(), "oi", START + 1).status).toBe("UNAVAILABLE");
  });
  it("keeps refresh queues separate from valid retained series", () => {
    expect(coinglassChartMetricStatus(bundle([source()], { status: "QUEUED" }), "oi", NOW).status).toBe("READY");
    expect(coinglassChartMetricStatus(bundle([], { status: "QUEUED" }), "oi", NOW).reason).toBe("等待后台采集");
    expect(coinglassChartMetricStatus(bundle([source()], { status: "DISABLED" }), "oi", NOW).status).toBe("UNAVAILABLE");
  });
});
