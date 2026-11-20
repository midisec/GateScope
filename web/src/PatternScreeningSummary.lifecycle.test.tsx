import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { PatternScreeningSummary, PatternScreeningView, ScreeningPatternRow, type PatternScreeningSummaryProps } from "./PatternScreeningSummary";
import { Pagination } from "./ui";
import { screeningHistory, type PatternScreeningSummaryData } from "./patternScreeningModel";
import type { PatternStudy } from "./patternModel";

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
const summary: PatternScreeningSummaryData = { total: 13, scored_count: 13, unscored_count: 0, directions: { bullish: 13, bearish: 0, neutral: 0 }, phase_counts: { FORMED: 13 },
  patterns: Array.from({ length: 13 }, (_, i) => ({ pattern: `p${i}`, name: `结构${i}`, family: "chart" as const, group: "反转", count: 1, share: 1/13, directions: { bullish: 1, bearish: 0, neutral: 0 }, score_min: .8, score_max: .9 })) };
const props: PatternScreeningSummaryProps = { summary, params: "instrument_id=btc&timeframe=1h", asOf: 100, filterLabel: "全部", version: "current", onClose: vi.fn(), onPattern: vi.fn() };
const report: PatternStudy = { state: "READY", cursor: 80, total: 80, report: null };
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean): Element { const item = elements(node).find(predicate); if (!item) throw new Error("Element not found"); return item; }
function render(values = props) { harness.cursor = 0; return PatternScreeningSummary(values); }
function view(node: ReactNode) { return find(node, element => element.type === PatternScreeningView); }
function startEffect() { return harness.effects.at(-1)!(); }
function renderView() { harness.cursor = 0; return PatternScreeningView({ ...props, study: report, onRetry: vi.fn() }); }

beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); vi.mocked(props.onClose).mockClear(); vi.mocked(props.onPattern).mockClear(); });

describe("pattern screening dialog request lifecycle and interactions", () => {
  it("loads one read-only study only when the mounted dialog effect runs", async () => {
    harness.api.mockResolvedValue(report);
    render(); expect(harness.api).not.toHaveBeenCalled();
    const cleanup = startEffect();
    expect(harness.api).toHaveBeenCalledOnce(); expect(harness.api.mock.calls[0][0]).toBe("/api/v2/patterns/study?instrument_id=btc&timeframe=1h");
    expect(harness.api.mock.calls[0][1].method).toBeUndefined();
    await vi.waitFor(() => expect(view(render()).props.study).toBe(report));
    cleanup(); expect((harness.api.mock.calls[0][1].signal as AbortSignal).aborted).toBe(true);
  });
  it("aborts immediately on close and ignores a late response", async () => {
    let resolve!: (value: PatternStudy) => void;
    harness.api.mockReturnValue(new Promise<PatternStudy>(yes => { resolve = yes; }));
    const root = render(), cleanup = startEffect();
    (root.props.onClose as () => void)();
    expect(props.onClose).toHaveBeenCalledOnce(); expect((harness.api.mock.calls[0][1].signal as AbortSignal).aborted).toBe(true);
    resolve(report); await Promise.resolve(); await Promise.resolve();
    expect(view(render()).props.study).toBeUndefined(); cleanup();
  });
  it("hides the old study as soon as the instrument changes and rejects its late response", async () => {
    let oldResolve!: (value: PatternStudy) => void;
    harness.api.mockReturnValueOnce(new Promise<PatternStudy>(yes => { oldResolve = yes; })).mockResolvedValueOnce({ ...report, cursor: 90 });
    render(); const cleanupOld = startEffect();
    const next = { ...props, params: "instrument_id=eth&timeframe=4h" };
    const changing = view(render(next));
    expect(changing.props.study).toBeUndefined(); expect(changing.props.loading).toBe(true);
    cleanupOld(); const cleanupNew = startEffect();
    oldResolve(report); await vi.waitFor(() => expect((view(render(next)).props.study as PatternStudy).cursor).toBe(90));
    expect(harness.api.mock.calls[1][0]).toContain("instrument_id=eth&timeframe=4h"); cleanupNew();
  });
  it("exposes a failed read and retries deliberately without creating a study", async () => {
    harness.api.mockRejectedValueOnce(new Error("请求超时，请重试")).mockResolvedValueOnce(report);
    render(); const cleanupOld = startEffect();
    await vi.waitFor(() => expect(view(render()).props.error).toBe("请求超时，请重试"));
    (view(render()).props.onRetry as () => void)(); render(); cleanupOld(); const cleanupNew = startEffect();
    await vi.waitFor(() => expect(view(render()).props.study).toBe(report));
    expect(harness.api).toHaveBeenCalledTimes(2);
    for (const [, request] of harness.api.mock.calls) expect(request.method).toBeUndefined();
    cleanupNew();
  });
  it("pages every matching type and applies explicit return phase and horizon selections", () => {
    const first = renderView();
    expect(elements(first).filter(element => element.type === ScreeningPatternRow)).toHaveLength(8);
    (find(first, element => element.type === Pagination).props.onChange as (value: number) => void)(1);
    const second = renderView(), rows = elements(second).filter(element => element.type === ScreeningPatternRow);
    expect(rows).toHaveLength(5); expect((rows[0].props.item as { pattern: string }).pattern).toBe("p8");
    (find(second, element => element.props["aria-label"] === "收益样本阶段").props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "FORMED" } });
    (find(second, element => element.props["aria-label"] === "收益观察期").props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "50" } });
    const selected = find(renderView(), element => element.type === ScreeningPatternRow);
    expect(selected.props.phase).toBe("FORMED"); expect(selected.props.horizon).toBe(50);
  });
  it("routes view-matching to the exact pattern identifier without an order or history write", () => {
    const row = ScreeningPatternRow({ item: summary.patterns[0], history: screeningHistory(undefined, "current"), phase: "CONFIRMED", horizon: 20, onPattern: props.onPattern });
    (find(row, element => element.type === "button").props.onClick as () => void)();
    expect(props.onPattern).toHaveBeenCalledWith("p0"); expect(harness.api).not.toHaveBeenCalled();
  });
});
