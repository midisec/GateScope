import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useFineVolumeProfile, type FineVolumeProfileInput } from "./useFineVolumeProfile";
import { FINE_START, fineChartBars, fineFixture, fineRequest } from "./fineVolumeProfile.testFixtures";
import type { FineProfileRequest } from "./fineVolumeProfileModel";

type EffectSlot = { deps: unknown[]; cleanup?: () => void };
const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as Array<() => void>, fetch: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
    useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
    useEffect(effect: () => (() => void) | undefined, deps: unknown[]) { const index = harness.cursor++, previous = harness.slots[index] as EffectSlot | undefined;
      if (!previous || deps.some((value, position) => !Object.is(value, previous.deps[position]))) harness.effects.push(() => { previous?.cleanup?.(); harness.slots[index] = { deps, cleanup: effect() }; }); },
  };
});
const defaults: FineVolumeProfileInput = { instrumentId: fineRequest.instrument_id, timeframe: "15m", bars: fineChartBars(),
  range: { from: 0, to: 1 }, enabled: true, rows: 48, valueAreaPercent: 70, scope: "owner" };
function render(extra: Partial<FineVolumeProfileInput> = {}) { harness.cursor = 0; return useFineVolumeProfile({ ...defaults, ...extra }); }
function flush() { harness.effects.splice(0).forEach(effect => effect()); }
function response(payload: unknown, status = 200, retry?: string) { return { ok: status >= 200 && status < 300, status,
  headers: new Headers(retry ? { "Retry-After": retry } : {}), json: async () => payload }; }
function requested(path: string): FineProfileRequest {
  const p = new URL(path, "https://unit.test").searchParams;
  return { instrument_id: p.get("instrument_id")!, timeframe: p.get("timeframe")!, from_time: Number(p.get("from_time")),
    to_time: Number(p.get("to_time")), rows: Number(p.get("rows")) as 24 | 48 | 96, value_area: Number(p.get("value_area")) };
}
const succeed = (path: string) => Promise.resolve(response(fineFixture(requested(path), {}, Math.floor(Date.now() / 1000))));
const settle = async () => { for (let index = 0; index < 6; index++) await Promise.resolve(); };
beforeEach(() => {
  harness.slots = []; harness.effects = []; harness.cursor = 0; harness.fetch.mockReset();
  vi.useFakeTimers(); vi.setSystemTime((FINE_START + 1802) * 1000);
  vi.stubGlobal("fetch", harness.fetch); vi.stubGlobal("window", new EventTarget());
  vi.stubGlobal("document", Object.assign(new EventTarget(), { hidden: false }));
});
afterEach(() => {
  for (const slot of harness.slots) if (slot && typeof slot === "object" && "cleanup" in slot) (slot as EffectSlot).cleanup?.();
  vi.unstubAllGlobals(); vi.useRealTimers();
});

describe("fine VPVR request lifecycle", () => {
  it("debounces visible changes and only sends one read-only final-range request", async () => {
    harness.fetch.mockImplementation(succeed); render(); flush();
    await vi.advanceTimersByTimeAsync(200); render({ range: { from: 0, to: 0 } }); flush();
    await vi.advanceTimersByTimeAsync(349); expect(harness.fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); expect(harness.fetch).toHaveBeenCalledOnce();
    expect(requested(harness.fetch.mock.calls[0][0]).to_time).toBe(FINE_START + 900);
    expect(harness.fetch.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin" });
    expect(render({ range: { from: 0, to: 0 } }).phase).toBe("READY");
  });
  it.each([
    { instrumentId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }, { timeframe: "1m", bars: fineChartBars(60, 2) },
    { range: { from: 1, to: 1 } }, { rows: 96 as const }, { valueAreaPercent: 85 }, { scope: "other-owner" },
  ])("aborts, hides and rejects stale responses after switching identity or parameters: %j", async extra => {
    let resolve!: (value: ReturnType<typeof response>) => void;
    harness.fetch.mockReturnValueOnce(new Promise(yes => { resolve = yes; })).mockImplementation(succeed);
    render(); flush(); await vi.advanceTimersByTimeAsync(350); const signal = harness.fetch.mock.calls[0][1].signal;
    expect(render(extra).bundle).toBeUndefined(); flush(); expect(signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(350); const latest = render(extra).bundle;
    expect(latest).toBeDefined(); resolve(response(fineFixture())); await settle();
    expect(render(extra).bundle).toBe(latest); expect(harness.fetch).toHaveBeenCalledTimes(2);
  });
  it("uses the exact-window cache on return without extending its absolute TTL", async () => {
    harness.fetch.mockImplementation(succeed); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    const original = render().bundle;
    render({ range: { from: 0, to: 0 } }); flush(); await vi.advanceTimersByTimeAsync(350);
    render(); flush(); expect(render().bundle).toBe(original); expect(harness.fetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30000); expect(harness.fetch).toHaveBeenCalledTimes(3);
    expect(render().bundle?.generated_at).toBeGreaterThan(original!.generated_at);
  });
  it("does not fetch again for fractional panning within the same selected candles", async () => {
    harness.fetch.mockImplementation(succeed); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    const old = render().bundle;
    render({ range: { from: -.1, to: 1.1 } }); flush(); await vi.advanceTimersByTimeAsync(1000);
    expect(harness.fetch).toHaveBeenCalledOnce(); expect(render({ range: { from: -.1, to: 1.1 } }).bundle).toBe(old);
  });
  it("does not fetch for hidden/disabled/empty charts and cancels pending work on hide", async () => {
    Object.assign(document, { hidden: true }); harness.fetch.mockImplementation(succeed);
    render(); flush(); await vi.advanceTimersByTimeAsync(60000); expect(harness.fetch).not.toHaveBeenCalled();
    Object.assign(document, { hidden: false }); document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(350); expect(harness.fetch).toHaveBeenCalledOnce();
    Object.assign(document, { hidden: true }); document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(120000); expect(harness.fetch).toHaveBeenCalledOnce(); expect(render().phase).toBe("PAUSED");
    render({ enabled: false }); flush(); expect(render({ enabled: false }).bundle).toBeUndefined();
    render({ range: null }); flush(); expect(render({ range: null }).phase).toBe("WAITING");
  });
  it("aborts in-flight requests on disable and cannot accept their late result", async () => {
    let resolve!: (value: ReturnType<typeof response>) => void;
    harness.fetch.mockReturnValue(new Promise(yes => { resolve = yes; })); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    const signal = harness.fetch.mock.calls[0][1].signal;
    render({ enabled: false }); flush(); expect(signal.aborted).toBe(true);
    resolve(response(fineFixture())); await settle(); expect(render({ enabled: false }).bundle).toBeUndefined();
  });
  it("does not use a legacy endpoint failure as a coarse fallback or automatic retry loop", async () => {
    harness.fetch.mockResolvedValue(response({}, 404)); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    expect(render().phase).toBe("ERROR"); expect(render().error).toContain("接口尚不可用"); expect(render().bundle).toBeUndefined();
    await vi.advanceTimersByTimeAsync(120000); expect(harness.fetch).toHaveBeenCalledOnce();
    harness.fetch.mockImplementation(succeed); render().reload(); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    expect(render().phase).toBe("READY");
  });
  it("honors Retry-After across panning and caps automatic retries", async () => {
    harness.fetch.mockResolvedValue(response({}, 429, "2")); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    expect(render().phase).toBe("COOLDOWN"); render().reload(); render({ range: { from: 0, to: 0 } }); flush();
    await vi.advanceTimersByTimeAsync(1999); expect(harness.fetch).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1); expect(harness.fetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000); expect(harness.fetch).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(120000); expect(harness.fetch).toHaveBeenCalledTimes(3);
    expect(render({ range: { from: 0, to: 0 } }).phase).toBe("ERROR");
  });
  it("does not shorten a long Retry-After or retry it in the background", async () => {
    harness.fetch.mockResolvedValue(response({}, 429, "7200")); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    await vi.advanceTimersByTimeAsync(7199000); render().reload(); render(); flush();
    expect(harness.fetch).toHaveBeenCalledOnce(); expect(render().phase).toBe("COOLDOWN");
  });
  it("rejects mismatched data and expired snapshots without caching them or polling rapidly", async () => {
    harness.fetch.mockResolvedValue(response(fineFixture(fineRequest, { instrument_id: "wrong" })));
    render(); flush(); await vi.advanceTimersByTimeAsync(350);
    expect(render().phase).toBe("ERROR"); expect(render().bundle).toBeUndefined();
    await vi.advanceTimersByTimeAsync(120000); expect(harness.fetch).toHaveBeenCalledOnce();
    harness.fetch.mockResolvedValue(response(fineFixture())); render().reload(); render(); flush(); await vi.advanceTimersByTimeAsync(350);
    expect(render().phase).toBe("ERROR"); expect(render().bundle).toBeUndefined();
  });
  it("bounds request duration and unregisters visibility listeners on cleanup", async () => {
    harness.fetch.mockImplementation((_path: string, init: RequestInit) => new Promise((_yes, no) => init.signal!.addEventListener("abort", () => no(new DOMException("Aborted", "AbortError")))));
    const remove = vi.spyOn(document, "removeEventListener");
    render(); flush(); await vi.advanceTimersByTimeAsync(30350);
    expect(render().phase).toBe("ERROR"); expect(render().error).toContain("超时");
    render({ enabled: false }); flush(); expect(remove).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
  });
});
