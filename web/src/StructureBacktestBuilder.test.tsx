import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type FormEvent, type ReactElement, type ReactNode } from "react";
import { StructureBacktestBuilder, structureRequestError, yearBarCounts, type StructureRequest } from "./StructureBacktestBuilder";
import { InstrumentPicker, type InstrumentChoice } from "./InstrumentPicker";
import type { Spec } from "./researchModel";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, api: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useEffect: vi.fn(), useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; }, useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; } };
});
vi.mock("./api", () => ({ api: harness.api, jsonBody: JSON.stringify }));
type Element = ReactElement<Record<string, unknown>>;
const defaults: Spec = { strategy: "trend", direction: "BOTH", fast_period: 20, slow_period: 50, lookback: 20, adx_threshold: 20, stop_atr: 2, reward_risk: 2, max_holding_bars: 48, initial_equity: 10000, risk_fraction: .01, fee_bps: 5, slippage_bps: 5, funding_bps_per_8h: 1 };
const catalog = [{ id: "double_bottom", name: "双底", group: "反转" }, { id: "wave_up", name: "上升五浪", group: "波浪" }];
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean): Element { const result = elements(node).find(predicate); if (!result) throw new Error("Element not found"); return result; }
function render(onCreated = vi.fn(), onBusy = vi.fn()) { harness.cursor = 0; return StructureBacktestBuilder({ defaults, catalog, onCreated, onBusy }); }
function selectInstrument(node: ReactNode, item: InstrumentChoice = { id: "btc-id", symbol: "BTC_USDT", market: "FUTURES_USDT" }) { return (find(node, e => e.type === InstrumentPicker).props.onChange as (item: InstrumentChoice) => Promise<void>)(item); }
function submit(node: ReactNode) { return (find(node, e => e.type === "form").props.onSubmit as (event: FormEvent) => Promise<void>)({ preventDefault: vi.fn() } as unknown as FormEvent); }
function request(): StructureRequest { return JSON.parse(harness.api.mock.calls[0][1].body); }
beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.api.mockReset(); harness.api.mockResolvedValue({ id: "created-id" }); });

describe("same-source backtest builder", () => {
  it("sends separate STRUCTURE mode and full-year period bounds to the real API contract", async () => {
    selectInstrument(render()); const root = render();
    const period = find(root, e => e.type === "select" && e.props.value === "4h");
    (period.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "1m" } });
    await submit(render());
    expect(harness.api.mock.calls[0][0]).toBe("/api/v2/research/backtests");
    expect(request().bar_limit).toBe(525600);
    expect(request().spec).toMatchObject({ engine_mode: "STRUCTURE", structure_patterns: ["double_bottom", "wave_up"], structure_entry_phase: "CONFIRMED", pattern_confidence: 0, entry_delay_bars: 0, walk_forward_folds: 4 });
  });
  it("prevents same-tick duplicate submissions and exposes busy state", async () => {
    let resolve!: (value: { id: string }) => void;
    harness.api.mockReturnValueOnce(new Promise(resolveValue => { resolve = resolveValue; }));
    const onCreated = vi.fn(), onBusy = vi.fn(); selectInstrument(render(onCreated, onBusy)); const root = render(onCreated, onBusy);
    const pending = submit(root); void submit(root);
    expect(harness.api).toHaveBeenCalledOnce(); expect(onBusy).toHaveBeenCalledWith(true);
    expect(find(render(onCreated, onBusy), e => e.type === "fieldset" && e.props.className === "structure-builder-fields").props.disabled).toBe(true);
    resolve({ id: "one-id" }); await pending;
    expect(onCreated).toHaveBeenCalledOnce(); expect(onBusy).toHaveBeenLastCalledWith(false);
  });
  it("keeps the selected patterns after a failed submission and allows deliberate retry", async () => {
    harness.api.mockRejectedValueOnce(new Error("请求失败，请重试")); selectInstrument(render());
    const root = render(), clear = find(root, e => e.type === "button" && e.props.children === "清空选择");
    (clear.props.onClick as () => void)();
    const first = find(render(), e => e.type === "input" && e.props.type === "checkbox");
    (first.props.onChange as (event: { target: { checked: boolean } }) => void)({ target: { checked: true } });
    await submit(render());
    expect(find(render(), e => e.props.role === "alert").props.children).toBe("请求失败，请重试");
    expect(request().spec.structure_patterns).toEqual(["double_bottom"]);
    await submit(render()); expect(harness.api).toHaveBeenCalledTimes(2);
    expect(JSON.parse(harness.api.mock.calls[1][1].body).spec.structure_patterns).toEqual(["double_bottom"]);
  });
  it("rejects missing instrument and explicitly cleared selection without starting a job", async () => {
    await submit(render()); expect(harness.api).not.toHaveBeenCalled();
    selectInstrument(render()); (find(render(), e => e.type === "button" && e.props.children === "清空选择").props.onClick as () => void)();
    await submit(render()); expect(harness.api).not.toHaveBeenCalled(); expect(find(render(), e => e.props.role === "alert").props.children).toContain("至少选择一个");
  });
  it("validates ordered timestamps and the large but bounded history request", () => {
    const valid: StructureRequest = { instrument_id: "btc", timeframe: "1h", bar_limit: 8760, from_time: null, to_time: null, spec: { ...defaults, structure_patterns: ["double_bottom"] } };
    expect(structureRequestError(valid)).toBeNull();
    expect(structureRequestError({ ...valid, bar_limit: 550001 })).toContain("550000");
    expect(structureRequestError({ ...valid, from_time: "2026-02-01", to_time: "2026-01-01" })).toContain("早于");
    expect(structureRequestError({ ...valid, from_time: "bad" })).toContain("有效");
    expect(yearBarCounts["1h"]).toBe(8760);
  });
  it("automatically changes spot to LONG, disables invalid directions and sends only LONG", async () => {
    await selectInstrument(render(), { id: "spot-id", symbol: "BTC_USDT", market: "SPOT" });
    const root = render(), direction = find(root, e => e.props["aria-label"] === "模拟方向");
    expect(direction.props.value).toBe("LONG");
    for (const value of ["BOTH", "SHORT"]) expect(find(direction, e => e.type === "option" && e.props.value === value).props.disabled).toBe(true);
    expect(harness.api).not.toHaveBeenCalled();
    await submit(root); expect(request().spec.direction).toBe("LONG");
  });
  it("allows BOTH or SHORT again after returning from spot to futures", async () => {
    await selectInstrument(render(), { id: "spot-id", symbol: "BTC_USDT", market: "spot" });
    await selectInstrument(render());
    const direction = find(render(), e => e.props["aria-label"] === "模拟方向");
    for (const value of ["BOTH", "SHORT"]) expect(find(direction, e => e.type === "option" && e.props.value === value).props.disabled).toBe(false);
    (direction.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "SHORT" } });
    await submit(render()); expect(request().spec.direction).toBe("SHORT");
  });
  it("resolves a watchlist choice by exact ID and blocks submission until its market is known", async () => {
    let resolve!: (value: { items: InstrumentChoice[] }) => void;
    harness.api.mockReturnValueOnce(new Promise(yes => { resolve = yes; }));
    const pending = selectInstrument(render(), { id: "spot-id", symbol: "BTC_USDT" });
    expect(harness.api.mock.calls[0][0]).toBe("/api/v2/instruments?q=BTC_USDT&limit=100");
    expect(find(render(), e => e.type === "button" && e.props.className === "primary").props.disabled).toBe(true);
    await submit(render()); expect(harness.api).toHaveBeenCalledOnce();
    resolve({ items: [{ id: "other-id", symbol: "BTC_USDT", market: "FUTURES_USDT" }, { id: "spot-id", symbol: "BTC_USDT", market: "SPOT" }] }); await pending;
    expect(find(render(), e => e.props["aria-label"] === "模拟方向").props.value).toBe("LONG");
  });
  it("ignores a late market lookup after the selected instrument changes", async () => {
    let resolve!: (value: { items: InstrumentChoice[] }) => void;
    harness.api.mockReturnValueOnce(new Promise(yes => { resolve = yes; }));
    const pending = selectInstrument(render(), { id: "spot-id", symbol: "BTC_USDT" });
    const signal = harness.api.mock.calls[0][1].signal as AbortSignal;
    await selectInstrument(render()); expect(signal.aborted).toBe(true);
    resolve({ items: [{ id: "spot-id", symbol: "BTC_USDT", market: "SPOT" }] }); await pending;
    expect(find(render(), e => e.props["aria-label"] === "模拟方向").props.value).toBe("BOTH");
    await submit(render());
    const created = JSON.parse(harness.api.mock.calls[1][1].body); expect(created.instrument_id).toBe("btc-id"); expect(created.spec.direction).toBe("BOTH");
  });
  it("keeps an unresolved market blocked and offers a deliberate metadata retry", async () => {
    harness.api.mockResolvedValueOnce({ items: [{ id: "not-this-id", symbol: "BTC_USDT", market: "SPOT" }] }).mockResolvedValueOnce({ items: [{ id: "spot-id", symbol: "BTC_USDT", market: "SPOT" }] });
    await selectInstrument(render(), { id: "spot-id", symbol: "BTC_USDT" });
    expect(find(render(), e => e.props.role === "alert").props.children).toContain("无法确认");
    expect(find(render(), e => e.type === "button" && e.props.className === "primary").props.disabled).toBe(true);
    await submit(render()); expect(harness.api).toHaveBeenCalledOnce();
    (find(render(), e => e.type === "button" && e.props.children === "重试读取标的信息").props.onClick as () => void)();
    await vi.waitFor(() => expect(find(render(), e => e.props["aria-label"] === "模拟方向").props.value).toBe("LONG"));
    expect(harness.api).toHaveBeenCalledTimes(2);
  });
});
