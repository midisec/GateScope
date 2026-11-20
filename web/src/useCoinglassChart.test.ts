import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCoinglassChart } from "./useCoinglassChart";
import { useCoinglassClock } from "./useCoinglassClock";
import { coinglassRetrySeconds, type CoinglassBundle } from "./coinglassModel";
import { coinglassChartMetricStatus } from "./curatedCoinglass";

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
const bundle: CoinglassBundle = { status: "PARTIAL", instrument_id: "btc", timeframe: "1m", scope: null, match: { status: "EXACT" }, as_of: 900, fetched_at: 901, expires_at: 1100,
  series: [{ id: "oi", label: "OI", unit: "USD", unit_status: "VERIFIED", native_timeframe: "1m", population: "OPEN_INTEREST_MARK_VALUE", status: "READY", quality_flags: [], points: [{ time: 840, close_time: 900, available_at: 901, value: 100 }] }] };
function render(instrument = "btc", timeframe = "1m", user = "owner", enabled = true) { harness.cursor = 0; return useCoinglassChart(instrument, timeframe, enabled, user); }
function flushEffects() { harness.effects.splice(0).forEach(effect => effect()); }
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
beforeEach(() => {
  harness.slots = []; harness.effects = []; harness.cursor = 0; harness.api.mockReset();
  vi.useFakeTimers(); vi.setSystemTime(1000000);
  const browser = new EventTarget();
  Object.assign(browser, { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval });
  vi.stubGlobal("window", browser); vi.stubGlobal("document", Object.assign(new EventTarget(), { hidden: false }));
});
afterEach(() => {
  for (const slot of harness.slots) if (slot && typeof slot === "object" && "cleanup" in slot) (slot as EffectSlot).cleanup?.();
  vi.unstubAllGlobals(); vi.useRealTimers();
});

describe("Coinglass cache polling lifecycle", () => {
  it("suppresses manual, online, visibility and periodic GETs during cooldown, then wakes at retry_at", async () => {
    const cooling = { ...bundle, status: "STALE", cache_retained: true, retry_at: 1120, retry_after_seconds: 120,
      series: bundle.series.map(series => ({ ...series, status: "STALE" })) };
    harness.api.mockResolvedValueOnce(cooling).mockResolvedValue(bundle);
    render(); flushEffects(); await settle();
    expect(render().bundle).toEqual(cooling);
    render().reload(); render().reload(); window.dispatchEvent(new Event("online")); document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(119000); expect(harness.api).toHaveBeenCalledOnce();
    expect(render().bundle?.fetched_at).toBe(901);
    await vi.advanceTimersByTimeAsync(1000); expect(harness.api).toHaveBeenCalledTimes(2);
    expect(render().bundle).toEqual(bundle);
  });
  it("retains the current scoped cache on transport failure but never merges missing points itself", async () => {
    harness.api.mockResolvedValueOnce(bundle).mockRejectedValueOnce(new Error("暂时无法连接"))
      .mockResolvedValueOnce({ ...bundle, status: "RATE_LIMITED", retry_at: 1120, series: [] });
    render(); flushEffects(); await settle(); render().reload(); await settle();
    expect(render().bundle).toEqual(bundle); expect(render().error).toBe("暂时无法连接");
    render().reload(); await settle(); expect(render().bundle?.series).toEqual([]);
  });
  it("locks duplicate reloads while a request is pending", async () => {
    harness.api.mockReturnValue(new Promise(() => undefined));
    render(); flushEffects(); render().reload(); render().reload();
    window.dispatchEvent(new Event("online")); expect(harness.api).toHaveBeenCalledOnce(); expect(render().loading).toBe(true);
  });
  it.each([["eth", "1m", "owner"], ["btc", "1h", "owner"], ["btc", "1m", "second-user"]])("aborts and hides the previous selection when switching to %s %s %s", async (coin, timeframe, user) => {
    let resolve!: (value: CoinglassBundle) => void;
    harness.api.mockReturnValueOnce(new Promise<CoinglassBundle>(yes => { resolve = yes; })).mockResolvedValueOnce({ ...bundle, instrument_id: coin, timeframe });
    render(); flushEffects(); const oldSignal = harness.api.mock.calls[0][1].signal;
    expect(render(coin, timeframe, user).bundle).toBeUndefined(); flushEffects(); expect(oldSignal.aborted).toBe(true);
    resolve({ ...bundle, retry_at: 1120, status: "RATE_LIMITED" }); await settle();
    expect(render(coin, timeframe, user).bundle?.instrument_id).toBe(coin);
    expect(render(coin, timeframe, user).bundle?.retry_at).toBeUndefined();
    expect(harness.api).toHaveBeenCalledTimes(2);
  });
  it("does not carry another user's cooldown to the next selection", async () => {
    harness.api.mockResolvedValueOnce({ ...bundle, retry_at: 1120 }).mockResolvedValueOnce(bundle);
    render(); flushEffects(); await settle();
    expect(render("btc", "1m", "second-user").bundle).toBeUndefined(); flushEffects(); await settle();
    expect(harness.api).toHaveBeenCalledTimes(2); expect(render("btc", "1m", "second-user").bundle?.retry_at).toBeUndefined();
  });
  it("rejects responses whose coin or native timeframe does not match the request", async () => {
    harness.api.mockResolvedValue({ ...bundle, instrument_id: "eth" });
    render(); flushEffects(); await settle();
    expect(render().bundle).toBeUndefined(); expect(render().error).toContain("不一致");
  });
  it("does not poll a hidden or disabled panel and cancels pending requests on close", async () => {
    Object.assign(document, { hidden: true }); harness.api.mockReturnValue(new Promise(() => undefined));
    render(); flushEffects(); expect(harness.api).not.toHaveBeenCalled();
    Object.assign(document, { hidden: false }); document.dispatchEvent(new Event("visibilitychange")); expect(harness.api).toHaveBeenCalledOnce();
    const signal = harness.api.mock.calls[0][1].signal;
    render("btc", "1m", "owner", false); flushEffects(); expect(signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(120000); expect(harness.api).toHaveBeenCalledOnce();
    expect(render("btc", "1m", "owner", false).bundle).toBeUndefined();
  });
  it("does not treat a stale relative retry hint as a fresh cooldown", async () => {
    harness.api.mockResolvedValue({ ...bundle, retry_after_seconds: 3600 });
    render(); flushEffects(); await settle(); await vi.advanceTimersByTimeAsync(30000);
    expect(harness.api).toHaveBeenCalledTimes(2);
  });
  it("checks queued/collecting jobs at the server interval and stops when completion explicitly disables polling", async () => {
    harness.api.mockResolvedValueOnce({ ...bundle, refresh_state: "QUEUED", next_poll_after_seconds: 5 })
      .mockResolvedValueOnce({ ...bundle, refresh_state: "COLLECTING", next_poll_after_seconds: 3 })
      .mockResolvedValue({ ...bundle, refresh_state: "IDLE", next_poll_after_seconds: 0 });
    render(); flushEffects(); await settle(); expect(render().bundle?.series).toEqual(bundle.series);
    await vi.advanceTimersByTimeAsync(4999); expect(harness.api).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1); expect(harness.api).toHaveBeenCalledTimes(2);
    expect(render().bundle?.refresh_state).toBe("COLLECTING");
    await vi.advanceTimersByTimeAsync(3000); expect(harness.api).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(120000); expect(harness.api).toHaveBeenCalledTimes(3);
    render().reload(); await settle(); expect(harness.api).toHaveBeenCalledTimes(4);
  });
  it.each([[0.1, 2], [120, 30]])("bounds queue polling hint %s to %s seconds", async (hint, seconds) => {
    harness.api.mockResolvedValue({ ...bundle, refresh_state: "QUEUED", next_poll_after_seconds: hint });
    render(); flushEffects(); await settle();
    await vi.advanceTimersByTimeAsync(seconds * 1000 - 1); expect(harness.api).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1); expect(harness.api).toHaveBeenCalledTimes(2);
  });
  it("does not use a short queue hint to bypass cooldown", async () => {
    harness.api.mockResolvedValue({ ...bundle, refresh_state: "COOLDOWN", retry_at: 1100, next_poll_after_seconds: 3 });
    render(); flushEffects(); await settle(); await vi.advanceTimersByTimeAsync(99000);
    expect(harness.api).toHaveBeenCalledOnce(); await vi.advanceTimersByTimeAsync(1000); expect(harness.api).toHaveBeenCalledTimes(2);
  });
  it("backs off fast progress polling after a transport error while retaining data", async () => {
    const pending = { ...bundle, refresh_state: "COLLECTING", next_poll_after_seconds: 3 };
    harness.api.mockResolvedValueOnce(pending).mockRejectedValueOnce(new Error("请求较频繁"))
      .mockResolvedValue({ ...bundle, refresh_state: "IDLE" });
    render(); flushEffects(); await settle(); await vi.advanceTimersByTimeAsync(3000);
    expect(harness.api).toHaveBeenCalledTimes(2); expect(render().bundle?.series).toEqual(bundle.series);
    await vi.advanceTimersByTimeAsync(29999); expect(harness.api).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1); expect(harness.api).toHaveBeenCalledTimes(3);
  });
  it.each(["IDLE", "NOT_REQUESTED"])("keeps visibility and manual checks available when %s explicitly stops automatic polling", async state => {
    harness.api.mockResolvedValue({ ...bundle, refresh_state: state, next_poll_after_seconds: 0 });
    render(); flushEffects(); await settle(); await vi.advanceTimersByTimeAsync(120000);
    expect(harness.api).toHaveBeenCalledOnce();
    document.dispatchEvent(new Event("visibilitychange")); await settle(); expect(harness.api).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(120000); expect(harness.api).toHaveBeenCalledTimes(2);
    render().reload(); await settle(); expect(harness.api).toHaveBeenCalledTimes(3);
  });
  it("updates the countdown every second without making any cache request", async () => {
    function clock() { harness.cursor = 0; return useCoinglassClock(1003); }
    expect(coinglassRetrySeconds({ retry_at: 1003 }, clock())).toBe(3); flushEffects();
    await vi.advanceTimersByTimeAsync(1000); expect(coinglassRetrySeconds({ retry_at: 1003 }, clock())).toBe(2);
    await vi.advanceTimersByTimeAsync(2000); expect(coinglassRetrySeconds({ retry_at: 1003 }, clock())).toBe(0);
    flushEffects(); expect(harness.api).not.toHaveBeenCalled();
  });
  it("uses the current render clock for a fresh response arriving between normal ticks", async () => {
    function clock() { harness.cursor = 0; return useCoinglassClock(); }
    expect(clock()).toBe(1000); flushEffects();
    await vi.advanceTimersByTimeAsync(500);
    const fresh = { ...bundle, fetched_at: 1000.5, expires_at: 1010 };
    expect(clock()).toBe(1000.5);
    expect(coinglassChartMetricStatus(fresh, "oi", clock()).status).toBe("READY");
    // A genuinely future response still fails the freshness check.
    expect(coinglassChartMetricStatus({ ...fresh, fetched_at: 1001 }, "oi", clock()).status).toBe("HISTORICAL");
    expect(harness.api).not.toHaveBeenCalled();
  });
  it("detects expiry on an unrelated render before the next 30-second tick", async () => {
    function clock() { harness.cursor = 0; return useCoinglassClock(); }
    const expiring = { ...bundle, fetched_at: 999, expires_at: 1001 };
    expect(coinglassChartMetricStatus(expiring, "oi", clock()).status).toBe("READY"); flushEffects();
    await vi.advanceTimersByTimeAsync(1000);
    expect(coinglassChartMetricStatus(expiring, "oi", clock()).status).toBe("HISTORICAL");
    expect(harness.api).not.toHaveBeenCalled();
  });
  it("keeps the normal 30-second and active/cooldown one-second scheduling intervals", async () => {
    const intervals = vi.spyOn(window, "setInterval");
    function clock(retryAt?: number, active = false) { harness.cursor = 0; return useCoinglassClock(retryAt, active); }
    clock(); flushEffects(); expect(intervals.mock.calls.at(-1)?.[1]).toBe(30000);
    clock(1003); flushEffects(); expect(intervals.mock.calls.at(-1)?.[1]).toBe(1000);
    await vi.advanceTimersByTimeAsync(3000);
    clock(1003); flushEffects(); expect(intervals.mock.calls.at(-1)?.[1]).toBe(30000);
    clock(undefined, true); flushEffects(); expect(intervals.mock.calls.at(-1)?.[1]).toBe(1000);
    expect(harness.api).not.toHaveBeenCalled(); intervals.mockRestore();
  });
});
