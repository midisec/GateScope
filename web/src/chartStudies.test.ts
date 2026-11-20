import { describe, expect, it } from "vitest";
import {
  CHART_COINGLASS_METRICS, clearChartStudies, defaultChartStudiesSettings, filterChartStudies,
  normalizeChartStudiesSettings, selectedChartStudies, setChartStudyEnabled, toggleChartStudy,
  type ChartStudyId, type ChartStudySearchItem,
} from "./chartStudies";

describe("persisted chart studies settings", () => {
  it("starts with no upstream selections and fresh independent VPVR defaults", () => {
    const first = defaultChartStudiesSettings(), second = defaultChartStudiesSettings();
    expect(first).toEqual({ version: 1, coinglassMetrics: [], vpvr: { enabled: false, rows: 48, valueAreaPercent: 70, widthPercent: 25 } });
    first.coinglassMetrics.push("oi"); first.vpvr.rows = 24;
    expect(second.coinglassMetrics).toEqual([]); expect(second.vpvr.rows).toBe(48);
  });
  it.each([undefined, null, [], true, "settings", { version: 2, coinglassMetrics: ["oi"] }, { coinglassMetrics: ["oi"] }])(
    "rejects malformed or unsupported layout versions: %j", value => {
      expect(normalizeChartStudiesSettings(value)).toEqual(defaultChartStudiesSettings());
    },
  );
  it("whitelists and deduplicates CG metrics, preserving the user's first selection order", () => {
    const input = { version: 1, coinglassMetrics: ["lsr", "oi", "oi", "funding", "oi_change", "liquidation_long", "liquidation_short", "inflow", "vpvr", 1, null],
      vpvr: { enabled: true, rows: 96, valueAreaPercent: 80, widthPercent: 35 }, secret: "not-layout-data" };
    const normalized = normalizeChartStudiesSettings(input);
    expect(normalized.coinglassMetrics).toEqual(["lsr", "oi", "funding", "oi_change", "liquidation_long", "liquidation_short"]);
    expect(normalized.coinglassMetrics).toHaveLength(CHART_COINGLASS_METRICS.length);
    expect(selectedChartStudies(normalized)).toHaveLength(7);
    expect(normalized).not.toHaveProperty("secret");
    expect(input.coinglassMetrics).toHaveLength(11);
  });
  it("bounds hostile input length and cannot add unknown chart studies", () => {
    const normalized = normalizeChartStudiesSettings({ version: 1, coinglassMetrics: Array.from({ length: 5000 }, () => "oi") });
    expect(normalized.coinglassMetrics).toEqual(["oi"]);
    expect(setChartStudyEnabled(normalized, "unknown" as ChartStudyId, true)).toEqual(normalized);
  });
  it.each([
    { enabled: "true", rows: 0, valueAreaPercent: 49, widthPercent: 9 },
    { enabled: 1, rows: "96", valueAreaPercent: 96, widthPercent: 41 },
    { enabled: false, rows: 144, valueAreaPercent: NaN, widthPercent: Infinity },
    { enabled: null, rows: -24, valueAreaPercent: 70.5, widthPercent: "25" },
  ])("uses safe defaults for invalid individual VPVR fields: %j", vpvr => {
    expect(normalizeChartStudiesSettings({ version: 1, vpvr }).vpvr).toEqual(defaultChartStudiesSettings().vpvr);
  });
  it.each([24, 48, 96] as const)("preserves valid rows and inclusive rendering boundaries: %s", rows => {
    for (const [valueAreaPercent, widthPercent] of [[50, 10], [95, 40]]) {
      const vpvr = { enabled: true, rows, valueAreaPercent, widthPercent };
      expect(normalizeChartStudiesSettings({ version: 1, vpvr }).vpvr).toEqual(vpvr);
    }
  });
  it("survives JSON round trips without mutating or sharing caller data", () => {
    const input = { version: 1, coinglassMetrics: ["oi", "oi_change"], vpvr: { enabled: true, rows: 96, valueAreaPercent: 85, widthPercent: 40 } };
    const normalized = normalizeChartStudiesSettings(JSON.parse(JSON.stringify(input)));
    expect(normalized).toEqual(input);
    normalized.coinglassMetrics.push("funding"); normalized.vpvr.rows = 24;
    expect(input.coinglassMetrics).toEqual(["oi", "oi_change"]); expect(input.vpvr.rows).toBe(96);
  });
  it("supports multiple CG studies with VPVR and idempotent enable without reordering", () => {
    const start = defaultChartStudiesSettings();
    const selected = ["oi", "lsr", "vpvr", "funding"].reduce((value, id) => toggleChartStudy(value, id as ChartStudyId), start);
    expect(selectedChartStudies(selected)).toEqual(["oi", "lsr", "funding", "vpvr"]);
    expect(setChartStudyEnabled(selected, "lsr", true)).toEqual(selected);
    expect(selectedChartStudies(toggleChartStudy(selected, "lsr"))).toEqual(["oi", "funding", "vpvr"]);
    expect(start).toEqual(defaultChartStudiesSettings());
  });
  it("clears selections but keeps the VPVR preferences for the next use", () => {
    const selected = normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi"], vpvr: { enabled: true, rows: 96, valueAreaPercent: 90, widthPercent: 30 } });
    const cleared = clearChartStudies(selected);
    expect(selectedChartStudies(cleared)).toEqual([]);
    expect(cleared.vpvr).toEqual({ enabled: false, rows: 96, valueAreaPercent: 90, widthPercent: 30 });
    expect(toggleChartStudy(cleared, "vpvr").vpvr.rows).toBe(96);
    expect(selected.vpvr.enabled).toBe(true);
  });
});

describe("study search and categories", () => {
  const items: ChartStudySearchItem[] = [
    { id: "oi", title: "OI 美元估值", description: "未平仓合约", category: "coinglass", keywords: "持仓量 Coinglass" },
    { id: "oi_change", title: "OI美元估值变化率", description: "相邻周期", category: "coinglass" },
    { id: "vpvr", title: "VPVR 可见区间成交量分布", description: "本地估算", category: "volume", keywords: "volume profile POC" },
  ];
  it("searches case-insensitively across titles and aliases with all query terms", () => {
    expect(filterChartStudies(items, "  oi   美元  ", "all").map(item => item.id)).toEqual(["oi", "oi_change"]);
    expect(filterChartStudies(items, "持仓量", "all").map(item => item.id)).toEqual(["oi"]);
    expect(filterChartStudies(items, "VOLUME poc", "all").map(item => item.id)).toEqual(["vpvr"]);
    expect(filterChartStudies(items, "OI volume", "all")).toEqual([]);
  });
  it("combines category and search without changing the source list", () => {
    expect(filterChartStudies(items, "", "coinglass")).toHaveLength(2);
    expect(filterChartStudies(items, "", "volume").map(item => item.id)).toEqual(["vpvr"]);
    expect(filterChartStudies(items, "OI", "volume")).toEqual([]);
    expect(items).toHaveLength(3);
  });
});
