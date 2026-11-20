import { beforeEach, describe, expect, it, vi } from "vitest";
import { useResearchResource } from "./useResearchResource";

type EffectSlot = { deps: unknown[]; cleanup?: () => void };
const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as Array<() => void>, api: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
    useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
    useEffect(effect: () => (() => void) | undefined, deps: unknown[]) { const index = harness.cursor++, previous = harness.slots[index] as EffectSlot | undefined; if (!previous || deps.some((value, position) => !Object.is(value, previous.deps[position]))) harness.effects.push(() => { previous?.cleanup?.(); harness.slots[index] = { deps, cleanup: effect() }; }); },
  };
});
vi.mock("./api", () => ({ api: harness.api }));
function render(url: string | null, refresh = 0) { harness.cursor = 0; return useResearchResource<{ id: string }>(url, refresh); }
function flushEffects() { const effects = harness.effects.splice(0); effects.forEach(effect => effect()); }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
beforeEach(() => { harness.slots = []; harness.effects = []; harness.cursor = 0; harness.api.mockReset(); });

describe("research resources", () => {
  it("does not fetch before a report has been selected", () => { const state = render(null); flushEffects(); expect(state.loading).toBe(false); expect(harness.api).not.toHaveBeenCalled(); });
  it("aborts a previous selection and ignores late data rather than showing another record", async () => {
    const first = deferred<{ id: string }>(), second = deferred<{ id: string }>(); harness.api.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render("/first"); flushEffects(); const signal = harness.api.mock.calls[0][1].signal as AbortSignal;
    expect(render("/second").data).toBeUndefined(); flushEffects(); expect(signal.aborted).toBe(true);
    first.resolve({ id: "first" }); await settle(); expect(render("/second").data).toBeUndefined();
    second.resolve({ id: "second" }); await settle(); expect(render("/second").data).toEqual({ id: "second" });
  });
  it("hides previously loaded data immediately when the selected URL changes", async () => {
    harness.api.mockResolvedValueOnce({ id: "first" }); render("/first"); flushEffects(); await settle(); expect(render("/first").data?.id).toBe("first");
    const next = render("/second"); expect(next.data).toBeUndefined(); expect(next.error).toBe(""); expect(next.loading).toBe(true);
  });
  it("retains the current record during refresh errors and allows a user retry", async () => {
    harness.api.mockResolvedValueOnce({ id: "current" }).mockRejectedValueOnce(new Error("暂时无法连接")).mockResolvedValueOnce({ id: "updated" });
    render("/same"); flushEffects(); await settle();
    render("/same", 1); flushEffects(); expect(render("/same", 1).data?.id).toBe("current"); await settle();
    const failed = render("/same", 1); expect(failed.error).toBe("暂时无法连接"); expect(failed.data?.id).toBe("current");
    failed.retry(); render("/same", 1); flushEffects(); await settle();
    expect(render("/same", 1).data?.id).toBe("updated"); expect(render("/same", 1).error).toBe("");
  });
});
