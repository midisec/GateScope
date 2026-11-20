import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { CoinglassControls, CoinglassPatternReview, CoinglassReviewContent, type CoinglassPatternReviewProps } from "./CoinglassContext";
import { PageState } from "./ui";
import type { CoinglassBundle, CoinglassReview, CoinglassReviewResponse } from "./coinglassModel";
import type { PatternEvent } from "./patternModel";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => (() => void))[], api: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useEffect(effect: () => (() => void)) { harness.effects.push(effect); },
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
    useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
  };
});
vi.mock("./api", () => ({ api: harness.api, jsonBody: JSON.stringify }));

type Element = ReactElement<Record<string, unknown>>;
const props: CoinglassPatternReviewProps = { event: { id: "candidate", evidence_id: "evidence", context_id: "context", name: "双底", phase: "FORMING" } as PatternEvent,
  instrumentId: "btc", timeframe: "1m", onClose: vi.fn(), onPreview: vi.fn() };
const review = { assessment_id: "hash", state: "READY", instrument_id: "btc", timeframe: "1m", candidate_id: "candidate", evidence_id: "evidence", original: { forecast: { target: { lower: 110, upper: 120 } } }, scenario: null } as CoinglassReview;
const response: CoinglassReviewResponse = { review_id: "saved", review };
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean) { const element = elements(node).find(predicate); if (!element) throw new Error("Element not found"); return element; }
function render(values = props) { harness.cursor = 0; return CoinglassPatternReview(values); }
function startEffect() { return harness.effects.at(-1)!(); }
function content(node: ReactNode) { return elements(node).find(element => element.type === CoinglassReviewContent); }
function input(node: ReactNode, label: string, value: string) { (find(node, element => element.props["aria-label"] === label).props.onChange as (event: { target: { value: string } }) => void)({ target: { value } }); }

beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); vi.mocked(props.onClose).mockClear(); vi.mocked(props.onPreview!).mockClear(); });

describe("Coinglass assessment request lifecycle", () => {
  it("sends a candidate reference rather than trusting front-end forecast prices", async () => {
    harness.api.mockResolvedValue(response); render(); const cleanup = startEffect();
    expect(harness.api.mock.calls[0][0]).toBe("/api/v2/coinglass/reviews");
    expect(JSON.parse(harness.api.mock.calls[0][1].body)).toEqual({ instrument_id: "btc", timeframe: "1m", context_id: "context", candidate_id: "candidate", expected_evidence_id: "evidence" });
    await vi.waitFor(() => expect(content(render())?.props.review).toBe(review)); cleanup();
  });
  it("aborts when closed and ignores a late resolved snapshot", async () => {
    let resolve!: (value: CoinglassReviewResponse) => void;
    harness.api.mockReturnValue(new Promise<CoinglassReviewResponse>(yes => { resolve = yes; }));
    const node = render(), cleanup = startEffect(); (node.props.onClose as () => void)();
    expect(harness.api.mock.calls[0][1].signal.aborted).toBe(true);
    resolve(response); await Promise.resolve(); await Promise.resolve(); expect(content(render())).toBeUndefined(); cleanup();
  });
  it("hides old data immediately when switching coin and rejects its late response", async () => {
    let oldResolve!: (value: CoinglassReviewResponse) => void;
    harness.api.mockReturnValueOnce(new Promise<CoinglassReviewResponse>(yes => { oldResolve = yes; })).mockResolvedValueOnce({ ...response, review: { ...review, instrument_id: "eth" } });
    render(); const oldCleanup = startEffect();
    const next = { ...props, instrumentId: "eth" }; expect(content(render(next))).toBeUndefined();
    oldCleanup(); const newCleanup = startEffect(); oldResolve(response);
    await vi.waitFor(() => expect((content(render(next))?.props.review as CoinglassReview).instrument_id).toBe("eth")); newCleanup();
  });
  it("rejects a response for a different evidence revision", async () => {
    harness.api.mockResolvedValue({ ...response, review: { ...review, evidence_id: "new-evidence" } });
    render(); const cleanup = startEffect();
    await vi.waitFor(() => expect(find(render(), element => element.type === PageState).props.error).toContain("不一致"));
    expect(content(render())).toBeUndefined(); cleanup();
  });
  it("validates reversed prices without a second POST and locks duplicate submits", async () => {
    harness.api.mockResolvedValueOnce(response).mockReturnValueOnce(new Promise(() => {}));
    render(); const cleanup = startEffect(); await vi.waitFor(() => expect(content(render())).toBeDefined());
    input(render(), "观察参考价", "100"); input(render(), "候选区下界", "130");
    await (find(render(), element => element.type === "form").props.onSubmit as (event: { preventDefault: () => void }) => Promise<void>)({ preventDefault: vi.fn() });
    expect(harness.api).toHaveBeenCalledOnce();
    expect(find(render(), element => element.props.role === "alert").props.children).toBe("区间下界不能高于上界");
    input(render(), "候选区下界", "110");
    const submit = find(render(), element => element.type === "form").props.onSubmit as (event: { preventDefault: () => void }) => Promise<void>;
    void submit({ preventDefault: vi.fn() }); void submit({ preventDefault: vi.fn() });
    expect(harness.api).toHaveBeenCalledTimes(2);
    expect(JSON.parse(harness.api.mock.calls[1][1].body).scenario).toEqual({ entry: 100, target_lower: 110, target_upper: 120 }); cleanup();
  });
  it("only adds the scenario to the chart after an explicit preview click", async () => {
    const scenario = { kind: "NEXT_PIVOT_OBSERVATION", entry: 100, target_lower: 110, target_upper: 120, issued_at: 1000, target_label: "下一拐点观察区", risk_reward: null };
    harness.api.mockResolvedValue({ ...response, review: { ...review, scenario } });
    render(); const cleanup = startEffect(); await vi.waitFor(() => expect(content(render())).toBeDefined());
    expect(props.onPreview).not.toHaveBeenCalled();
    (find(render(), element => element.type === "button" && element.props.children === "在图表查看情景").props.onClick as () => void)();
    expect(props.onPreview).toHaveBeenCalledWith(scenario); expect(props.onClose).toHaveBeenCalledOnce(); cleanup();
  });
  it("offers a deliberate reassessment retry for unavailable snapshots", async () => {
    harness.api.mockResolvedValue({ ...response, review: { ...review, state: "UNAVAILABLE" } });
    render(); const firstCleanup = startEffect(); await vi.waitFor(() => expect(content(render())).toBeDefined());
    expect(elements(render()).some(element => element.type === "form")).toBe(false);
    (find(render(), element => element.type === "button" && element.props.children === "重新获取数据研判").props.onClick as () => void)();
    render(); firstCleanup(); const secondCleanup = startEffect();
    expect(harness.api).toHaveBeenCalledTimes(2); secondCleanup();
  });
  it("uses one selectable subchart on mobile and reduces existing desktop selection", () => {
    vi.stubGlobal("window", { matchMedia: () => ({ matches: true }) });
    try {
      const bundle = { match: { status: "EXACT" }, status: "PARTIAL", scope: null, timeframe: "1m", expires_at: 9999999999,
        series: ["oi", "lsr"].map(id => ({ id, unit: id === "oi" ? "USD" : "RATIO", unit_status: "VERIFIED", native_timeframe: "1m", status: "READY", points: [{ time: 60, close_time: 120, available_at: 121, value: 1 }] })) } as CoinglassBundle;
      const changed = vi.fn(); harness.cursor = 0;
      const node = CoinglassControls({ bundle, metrics: ["oi", "lsr"], onMetricsChange: changed, onRefresh: vi.fn() });
      expect(elements(node).some(element => element.type === "input")).toBe(false);
      const select = find(node, element => element.props["aria-label"] === "手机衍生品副图");
      expect(select.props.value).toBe("oi"); startEffect(); expect(changed).toHaveBeenCalledWith(["oi"]);
      (select.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "lsr" } });
      expect(changed).toHaveBeenLastCalledWith(["lsr"]);
      (select.props.onChange as (event: { target: { value: string } }) => void)({ target: { value: "" } });
      expect(changed).toHaveBeenLastCalledWith([]);
    } finally { vi.unstubAllGlobals(); }
  });
  it("disables every manual refresh entry during cooldown, then unlocks after the UI clock ticks", () => {
    let tick!: () => void;
    const clock = vi.spyOn(Date, "now").mockReturnValue(1000000);
    vi.stubGlobal("window", { setInterval: (callback: () => void) => { tick = callback; return 1; }, clearInterval: vi.fn() });
    try {
      const refresh = vi.fn(), bundle = { status: "RATE_LIMITED", retry_at: 1002, series: [] } as unknown as CoinglassBundle;
      const draw = () => { harness.cursor = 0; return CoinglassControls({ bundle, error: "连接失败", metrics: [], onMetricsChange: vi.fn(), onRefresh: refresh }); };
      let node = draw(); const cleanup = harness.effects[0]();
      let button = find(node, element => element.type === "button"); expect(button.props.disabled).toBe(true);
      (button.props.onClick as () => void)(); expect(refresh).not.toHaveBeenCalled();
      expect(find(node, element => element.type === PageState).props.onRetry).toBeUndefined();
      clock.mockReturnValue(1002000); tick(); node = draw(); button = find(node, element => element.type === "button");
      expect(button.props.disabled).toBe(false); (button.props.onClick as () => void)(); expect(refresh).toHaveBeenCalledOnce(); cleanup();
    } finally { clock.mockRestore(); vi.unstubAllGlobals(); }
  });
  it("does not enqueue repeated pattern reviews while provider cooldown is active", async () => {
    harness.api.mockResolvedValue({ ...response, review: { ...review, state: "UNAVAILABLE", bundle: { retry_at: Date.now() / 1000 + 120 } } });
    render(); const cleanup = startEffect(); await vi.waitFor(() => expect(content(render())).toBeDefined());
    const button = find(render(), element => element.type === "button" && element.props.children === "重新获取数据研判");
    expect(button.props.disabled).toBe(true); const before = harness.slots.slice();
    (button.props.onClick as () => void)(); expect(harness.slots).toEqual(before); expect(harness.api).toHaveBeenCalledOnce(); cleanup();
  });
});
