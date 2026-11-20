import { isValidElement, type ReactElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChartStudiesPicker, type ChartStudiesPickerProps } from "./ChartStudiesPicker";
import { defaultChartStudiesSettings, normalizeChartStudiesSettings, selectedChartStudies, type ChartStudiesSettings } from "./chartStudies";
import type { CoinglassBundle } from "./coinglassModel";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, now: 180182 }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useState(initial: unknown) {
    const index = harness.cursor++;
    if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial;
    return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }];
  } };
});
vi.mock("./useCoinglassClock", () => ({ useCoinglassClock: () => harness.now }));
type Element = ReactElement<Record<string, unknown>>;
function elements(node: ReactNode): Element[] {
  return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node)
    ? [node, ...elements(node.props.children as ReactNode)] : [];
}
function find(node: ReactNode, predicate: (element: Element) => boolean): Element {
  const result = elements(node).find(predicate); if (!result) throw new Error("Element not found"); return result;
}
const byLabel = (node: ReactNode, label: string) => find(node, element => element.props["aria-label"] === label);
const button = (node: ReactNode, text: string) => find(node, element => element.type === "button" && element.props.children === text);
function click(element: Element) { (element.props.onClick as () => void)(); }
function change(element: Element, value = "") { (element.props.onChange as (event: { target: { value: string } }) => void)({ target: { value } }); }
function source(extra: Partial<CoinglassBundle> = {}): CoinglassBundle {
  return { status: "QUEUED", instrument_id: "sol", timeframe: "1m", scope: null, match: { status: "EXACT" },
    as_of: null, fetched_at: harness.now, expires_at: harness.now + 60, series: [], ...extra };
}
function setup(initial = defaultChartStudiesSettings(), extra: Partial<ChartStudiesPickerProps> = {}) {
  let settings = initial;
  const onChange = vi.fn((next: ChartStudiesSettings) => { settings = next; }), onRefresh = vi.fn(), onClose = vi.fn();
  const props = { onChange, onRefresh, onClose, ...extra };
  return { onChange, onRefresh, onClose, settings: () => settings, props,
    render: () => { harness.cursor = 0; return ChartStudiesPicker({ ...props, settings }); } };
}
beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.now = 180182; });

describe("chart study picker controlled interactions", () => {
  it("selects several native and derived studies plus VPVR without a save or network request", () => {
    const page = setup();
    for (const title of ["OI 美元估值", "资金费率", "OI美元估值变化率", "VPVR 可见区间成交量分布"])
      change(byLabel(page.render(), `显示 ${title}`));
    expect(selectedChartStudies(page.settings())).toEqual(["oi", "funding", "oi_change", "vpvr"]);
    expect(page.onChange).toHaveBeenCalledTimes(4);
    expect(page.onRefresh).not.toHaveBeenCalled(); expect(page.onClose).not.toHaveBeenCalled();
    expect(byLabel(page.render(), "显示 OI 美元估值").props.checked).toBe(true);
  });
  it("searches aliases and categories without losing hidden selections", () => {
    const page = setup(normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi", "funding"] }));
    change(byLabel(page.render(), "搜索精选指标"), "持仓量");
    expect(elements(page.render()).filter(element => element.type === "input" && element.props.type === "checkbox")).toHaveLength(2);
    click(button(page.render(), "量价"));
    expect(elements(page.render()).filter(element => element.type === "input" && element.props.type === "checkbox")).toHaveLength(0);
    expect(selectedChartStudies(page.settings())).toEqual(["oi", "funding"]);
    click(button(page.render(), "查看全部"));
    expect(elements(page.render()).filter(element => element.type === "input" && element.props.type === "checkbox")).toHaveLength(7);
    expect(byLabel(page.render(), "搜索精选指标").props.value).toBe("");
    expect(button(page.render(), "全部").props["aria-pressed"]).toBe(true);
    expect(page.onChange).not.toHaveBeenCalled();
  });
  it("clears the search without clearing selected studies", () => {
    const page = setup(normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["lsr"] }));
    change(byLabel(page.render(), "搜索精选指标"), "VPVR");
    click(byLabel(page.render(), "清除指标搜索"));
    expect(byLabel(page.render(), "搜索精选指标").props.value).toBe("");
    expect(selectedChartStudies(page.settings())).toEqual(["lsr"]);
  });
  it("removes a selected item even when filtered out and clears while preserving display preferences", () => {
    const page = setup(normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi", "funding"],
      vpvr: { enabled: true, rows: 96, valueAreaPercent: 90, widthPercent: 35 } }));
    click(button(page.render(), "量价")); click(byLabel(page.render(), "移除 OI 美元估值"));
    expect(selectedChartStudies(page.settings())).toEqual(["funding", "vpvr"]);
    click(button(page.render(), "清空已选"));
    expect(selectedChartStudies(page.settings())).toEqual([]);
    expect(page.settings().vpvr).toEqual({ enabled: false, rows: 96, valueAreaPercent: 90, widthPercent: 35 });
    change(byLabel(page.render(), "显示 VPVR 可见区间成交量分布"));
    expect(byLabel(page.render(), "VPVR 价格分层").props.value).toBe(96);
  });
  it("treats both closing mechanisms as close-only and preserves persisted selections on reopen", () => {
    const page = setup(); change(byLabel(page.render(), "显示 OI 美元估值"));
    const beforeClose = page.onChange.mock.calls.length;
    click(button(page.render(), "完成"));
    (page.render().props.onClose as () => void)();
    expect(page.onClose).toHaveBeenCalledTimes(2); expect(page.onChange).toHaveBeenCalledTimes(beforeClose);
    const persisted = JSON.parse(JSON.stringify(page.settings())); harness.slots = [];
    const reopened = setup(normalizeChartStudiesSettings(persisted));
    expect(byLabel(reopened.render(), "显示 OI 美元估值").props.checked).toBe(true);
  });
  it.each(["NOT_CONFIGURED", "QUEUED", "RATE_LIMITED"])("does not disable selection when data is %s", status => {
    const page = setup(defaultChartStudiesSettings(), { bundle: source({ status }) });
    const input = byLabel(page.render(), "显示 OI 美元估值");
    expect(input.props.disabled).toBeUndefined(); change(input);
    expect(page.settings().coinglassMetrics).toEqual(["oi"]); expect(page.onRefresh).not.toHaveBeenCalled();
  });
  it("only refreshes explicitly after a CG selection and blocks inactive or busy refresh callbacks", () => {
    const page = setup();
    const emptyRefresh = byLabel(page.render(), "刷新 Coinglass 数据");
    expect(emptyRefresh.props.disabled).toBe(true); click(emptyRefresh); expect(page.onRefresh).not.toHaveBeenCalled();
    change(byLabel(page.render(), "显示 VPVR 可见区间成交量分布"));
    expect(byLabel(page.render(), "刷新 Coinglass 数据").props.disabled).toBe(true);
    change(byLabel(page.render(), "显示 OI 美元估值"));
    expect(page.onRefresh).not.toHaveBeenCalled(); click(byLabel(page.render(), "刷新 Coinglass 数据"));
    expect(page.onRefresh).toHaveBeenCalledOnce();
    page.props.loading = true;
    const busy = byLabel(page.render(), "刷新 Coinglass 数据");
    expect(busy.props.disabled).toBe(true); expect(busy.props["aria-busy"]).toBe(true); click(busy);
    expect(page.onRefresh).toHaveBeenCalledOnce();
  });
  it("unlocks refresh only after the absolute cooldown expires", () => {
    const page = setup(defaultChartStudiesSettings(), { bundle: source({ status: "RATE_LIMITED", retry_at: harness.now + 5 }) });
    let refresh = byLabel(page.render(), "刷新 Coinglass 数据");
    expect(refresh.props.disabled).toBe(true); click(refresh); expect(page.onRefresh).not.toHaveBeenCalled();
    harness.now += 5; refresh = byLabel(page.render(), "刷新 Coinglass 数据");
    expect(refresh.props.disabled).toBe(false); click(refresh); expect(page.onRefresh).toHaveBeenCalledOnce();
  });
  it("updates one VPVR parameter at a time while retaining other studies and bounds", () => {
    const page = setup(normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi"], vpvr: { enabled: true } }));
    change(byLabel(page.render(), "VPVR 价格分层"), "96");
    change(byLabel(page.render(), "VPVR 价值区间百分比"), "95");
    change(byLabel(page.render(), "VPVR 侧边宽度百分比"), "40");
    expect(page.settings().vpvr).toEqual({ enabled: true, rows: 96, valueAreaPercent: 95, widthPercent: 40 });
    expect(page.settings().coinglassMetrics).toEqual(["oi"]);
    change(byLabel(page.render(), "VPVR 价值区间百分比"), "999");
    expect(page.settings().vpvr.valueAreaPercent).toBe(70);
  });
  it("never reduces an existing multi-selection based on mobile viewport width", () => {
    const media = vi.fn(() => ({ matches: true })); vi.stubGlobal("window", { matchMedia: media });
    try {
      const page = setup(normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi", "lsr", "funding"], vpvr: { enabled: true } }));
      expect(elements(page.render()).filter(element => element.type === "input" && element.props.type === "checkbox")).toHaveLength(7);
      expect(selectedChartStudies(page.settings())).toEqual(["oi", "lsr", "funding", "vpvr"]);
      expect(media).not.toHaveBeenCalled(); expect(page.onChange).not.toHaveBeenCalled();
    } finally { vi.unstubAllGlobals(); }
  });
});
