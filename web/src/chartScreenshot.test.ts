import { describe, expect, it, vi } from "vitest";
import { captureChartScreenshot, type ChartScreenshotState } from "./chartScreenshot";
import { FINE_START, fineChartBars, fineFixture, fineRequest } from "./fineVolumeProfile.testFixtures";
import { computeVolumeProfile, type FineVolumeProfileResponse, type RenderableVolumeProfileResult } from "./volumeProfile";

function setup() {
  let clock = (FINE_START + 1802) * 1000;
  let range = { from: -.5, to: 1.5 }, result: RenderableVolumeProfileResult = computeVolumeProfile([], null);
  const canvas = {} as HTMLCanvasElement, takeScreenshot = vi.fn(() => canvas);
  const clearCrosshairPosition = vi.fn();
  const chart = { timeScale: () => ({ getVisibleLogicalRange: () => range }), takeScreenshot, clearCrosshairPosition };
  const controller = { setProfile: vi.fn((value?: FineVolumeProfileResponse) => { result = value?.profile ?? computeVolumeProfile([], null); }),
    result: () => result, update: vi.fn(), detach: vi.fn() };
  const bundle = fineFixture();
  let state = { chart, controller, bars: fineChartBars(), chartKey: `${fineRequest.instrument_id}:15m`, instrumentId: fineRequest.instrument_id,
    timeframe: "15m", scope: "owner", vpvr: { enabled: true, rows: 48, valueAreaPercent: 70, widthPercent: 25 }, fine: { phase: "READY", bundle } } as unknown as ChartScreenshotState;
  return { canvas, takeScreenshot, clearCrosshairPosition, controller, bundle, read: () => state,
    state: (value: Partial<ChartScreenshotState>) => { state = { ...state, ...value }; },
    range: (value: typeof range) => { range = value; }, now: () => clock,
    wait: async () => { clock += 16; }, advance: (milliseconds: number) => { clock += milliseconds; }, clear: () => { result = computeVolumeProfile([], null); } };
}

describe("chart PNG export with selected remote VPVR", () => {
  it("waits for the current layer, then captures top primitives without crosshair", async () => {
    const test = setup(); test.state({ fine: { phase: "LOADING" } }); let frames = 0;
    const wait = async () => { await test.wait(); if (++frames === 2) test.state({ fine: { phase: "READY", bundle: test.bundle } }); };
    expect(await captureChartScreenshot(test.read, { now: test.now, waitFrame: wait })).toBe(test.canvas);
    expect(test.controller.setProfile).toHaveBeenCalledWith(test.bundle); expect(frames).toBeGreaterThanOrEqual(3);
    expect(test.clearCrosshairPosition).toHaveBeenCalledTimes(2);
    expect(test.takeScreenshot).toHaveBeenCalledWith(true, true);
  });
  it("avoids screenshot crosshair option changes that relayout the axis and invalidate the actual UTC range", async () => {
    const test = setup(); let crosshairVisible = true;
    test.clearCrosshairPosition.mockImplementation(() => { crosshairVisible = false; });
    test.takeScreenshot.mockImplementation((...args: unknown[]) => {
      // LWC's includeCrosshair=false invokes applyOptions and a full layout;
      // reproduce the observed real-window change, not a floating-point jitter.
      if (args[1] === false) test.range({ from: .6, to: 1.4 });
      expect(crosshairVisible).toBe(false);
      return test.canvas;
    });
    const wait = async () => { await test.wait(); crosshairVisible = true; };
    expect(await captureChartScreenshot(test.read, { now: test.now, waitFrame: wait })).toBe(test.canvas);
    expect(test.clearCrosshairPosition).toHaveBeenCalledTimes(2);
    expect(test.takeScreenshot).toHaveBeenCalledWith(true, true);
  });
  it("allows logical jitter when it does not alter the frozen candle window", async () => {
    const test = setup();
    const wait = async () => { test.range({ from: -.4999999999, to: 1.4999999999 }); await test.wait(); };
    expect(await captureChartScreenshot(test.read, { now: test.now, waitFrame: wait })).toBe(test.canvas);
  });
  it.each(["symbol", "range", "rows", "disable", "owner"])("rejects changing %s while waiting rather than exporting mismatched evidence", async mode => {
    const test = setup(); test.state({ fine: { phase: "LOADING" } });
    const wait = async () => {
      if (mode === "symbol") test.state({ instrumentId: "other", chartKey: "other:15m" });
      if (mode === "range") test.range({ from: .6, to: 1.4 });
      if (mode === "rows") test.state({ vpvr: { ...test.read().vpvr!, rows: 96 } });
      if (mode === "disable") test.state({ vpvr: { ...test.read().vpvr!, enabled: false } });
      if (mode === "owner") test.state({ scope: "another-owner" });
      await test.wait();
    };
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: wait })).rejects.toThrow("已切换");
    expect(test.takeScreenshot).not.toHaveBeenCalled();
  });
  it("does not publish a canvas if synchronous screenshot repaint cleared its profile", async () => {
    const test = setup(); test.takeScreenshot.mockImplementation(() => { test.clear(); return test.canvas; });
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: test.wait })).rejects.toThrow("VPVR 区间发生变化");
  });
  it("rechecks expiry after the render wait even if the old bundle reference has not changed", async () => {
    const test = setup(), bundle = { ...test.bundle, expires_at: test.bundle.generated_at + 1 };
    test.state({ fine: { phase: "READY", bundle } });
    const wait = async () => { test.advance(1001); };
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: wait, timeoutMs: 2000 })).rejects.toThrow("暂未导出");
    expect(test.takeScreenshot).not.toHaveBeenCalled();
  });
  it("bounds waiting and refuses missing/error/zero-volume layers rather than silently omitting them", async () => {
    const test = setup(); test.state({ fine: { phase: "LOADING" } });
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: test.wait, timeoutMs: 48 })).rejects.toThrow("暂未导出");
    expect(test.takeScreenshot).not.toHaveBeenCalled();
    test.state({ fine: { phase: "ERROR", error: "细周期接口不可用" } });
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: test.wait })).rejects.toThrow("接口不可用");
    test.state({ fine: { phase: "READY", bundle: fineFixture(fineRequest, { used_bars: 0 }) } });
    await expect(captureChartScreenshot(test.read, { now: test.now, waitFrame: test.wait })).rejects.toThrow("暂无可绘制");
  });
  it("leaves ordinary non-VPVR chart export unchanged", async () => {
    const test = setup(); test.state({ vpvr: undefined });
    expect(await captureChartScreenshot(test.read)).toBe(test.canvas);
    expect(test.controller.setProfile).not.toHaveBeenCalled(); expect(test.takeScreenshot).toHaveBeenCalledWith(true, false);
  });
});
