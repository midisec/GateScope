import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { McpMarketPreview, McpSnapshotView } from "./McpWorkspace";
import { InstrumentPicker } from "./InstrumentPicker";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => (() => void))[], api: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useEffect(effect: () => (() => void)) { harness.effects.push(effect); },
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
    useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
  };
});
vi.mock("./api", () => ({ api: harness.api }));
type Element = ReactElement<Record<string, unknown>>;
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean): Element { const item = elements(node).find(predicate); if (!item) throw new Error("Element not found"); return item; }
function render() { harness.cursor = 0; return McpMarketPreview({ notify: vi.fn() }); }
function choose(id = "btc") { (find(render(), node => node.type === InstrumentPicker).props.onChange as (value: unknown) => void)({ id, symbol: id.toUpperCase() }); }
function submit() { return (find(render(), node => node.type === "form").props.onSubmit as (value: unknown) => void)({ preventDefault: () => undefined }); }
beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); });

describe("MCP preview request lifecycle", () => {
  it("only reads after user submits and exposes the selected URI and loading state", async () => {
    let resolve!: (value: unknown) => void;
    harness.api.mockReturnValue(new Promise(yes => { resolve = yes; }));
    render(); choose(); expect(harness.api).not.toHaveBeenCalled(); submit();
    expect(harness.api.mock.calls[0][0]).toBe("/api/v2/mcp/market-preview?instrument_id=btc&timeframe=15m&limit=500");
    expect(harness.api.mock.calls[0][1].method).toBeUndefined();
    expect(elements(render()).some(node => node.props.children === "读取中…" && node.props.disabled === true)).toBe(true);
    resolve({ instrument: { id: "btc" } });
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === McpSnapshotView)).toBe(true));
  });
  it("aborts on symbol change and refuses late data for the previous symbol", async () => {
    let resolve!: (value: unknown) => void;
    harness.api.mockReturnValue(new Promise(yes => { resolve = yes; }));
    choose(); submit(); const signal = harness.api.mock.calls[0][1].signal as AbortSignal;
    choose("eth"); expect(signal.aborted).toBe(true);
    resolve({ instrument: { id: "btc" } }); await Promise.resolve(); await Promise.resolve();
    expect(elements(render()).some(node => node.type === McpSnapshotView)).toBe(false);
  });
  it("aborts and clears results when timeframe changes", async () => {
    harness.api.mockReturnValue(new Promise(() => undefined)); choose(); submit();
    const signal = harness.api.mock.calls[0][1].signal as AbortSignal;
    (find(render(), node => node.props["aria-label"] === "MCP 输出周期").props.onChange as (value: unknown) => void)({ target: { value: "1h" } });
    expect(signal.aborted).toBe(true); submit(); expect(harness.api.mock.calls[1][0]).toContain("timeframe=1h");
  });
  it("shows errors without retry storms and permits explicit retry", async () => {
    harness.api.mockRejectedValueOnce(new Error("读取超时")); choose(); submit();
    await vi.waitFor(() => expect(elements(render()).some(node => node.props.role === "alert")).toBe(true));
    expect(harness.api).toHaveBeenCalledOnce(); harness.api.mockReturnValue(new Promise(() => undefined)); submit();
    expect(harness.api).toHaveBeenCalledTimes(2);
  });
});
