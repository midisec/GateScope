import { afterEach, describe, expect, it, vi } from "vitest";
import { attachVolumeProfile, fineProfileCanvasLabel, type RemoteVolumeProfileOptions, type VolumeProfilePrimitive } from "./volumeProfilePrimitive";
import type { VolumeProfileBar, VolumeProfileRange } from "./volumeProfile";
import { fineChartBars, fineFixture, fineRequest } from "./fineVolumeProfile.testFixtures";

function rows(count = 4): VolumeProfileBar[] {
  return Array.from({ length: count }, (_, index) => ({ open_time: new Date(index*60_000).toISOString(),
    low: 100, high: 124, open: 100, close: 124, volume: 240, is_final: true }));
}

function fixture(data = rows(), remote?: RemoteVolumeProfileOptions) {
  const frames = new Map<number, FrameRequestCallback>(); let next = 0;
  const request = vi.fn((callback: FrameRequestCallback) => { frames.set(++next, callback); return next; });
  const cancel = vi.fn((id: number) => { frames.delete(id); });
  vi.stubGlobal("requestAnimationFrame", request); vi.stubGlobal("cancelAnimationFrame", cancel);
  let range: VolumeProfileRange | null = { from: -.5, to: data.length-.5 };
  let rangeHandler: (() => void) | undefined, sizeHandler: (() => void) | undefined;
  const scale = { getVisibleLogicalRange: () => range,
    subscribeVisibleLogicalRangeChange: vi.fn((callback: () => void) => { rangeHandler = callback; }),
    unsubscribeVisibleLogicalRangeChange: vi.fn(), subscribeSizeChange: vi.fn((callback: () => void) => { sizeHandler = callback; }),
    unsubscribeSizeChange: vi.fn(), setVisibleLogicalRange: vi.fn(), fitContent: vi.fn() };
  const chart = { timeScale: () => scale, addSeries: vi.fn() };
  let primitive: VolumeProfilePrimitive;
  let convert: (price: number) => number | null = price => 250-price;
  const redraw = vi.fn(), changed = vi.fn();
  const series = { priceToCoordinate: vi.fn((price: number) => convert(price)), setData: vi.fn(),
    attachPrimitive: vi.fn((value: VolumeProfilePrimitive) => {
      primitive = value; value.attached({ chart, series, requestUpdate: redraw, horzScaleBehavior: {} } as any);
    }), detachPrimitive: vi.fn((value: VolumeProfilePrimitive) => value.detached()) };
  const controller = attachVolumeProfile(chart as any, series as any, data, { bins: 24 }, changed, remote);
  return { controller, series, chart, scale, redraw, changed, request, cancel, frames,
    primitive: () => primitive!,
    flush: () => { const current = [...frames.entries()]; frames.clear(); for (const [, callback] of current) callback(0); },
    range: (value: VolumeProfileRange | null, emit = true) => { range = value; if (emit) rangeHandler?.(); },
    resize: () => sizeHandler?.(), projection: (value: (price: number) => number | null) => { convert = value; },
  };
}

function draw(primitive: VolumeProfilePrimitive, width = 400, height = 300, rx = 1, ry = rx) {
  const rectangles: { x: number; y: number; width: number; height: number; style: string }[] = [];
  const context: any = { save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), rect: vi.fn(), clip: vi.fn(),
    moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), setLineDash: vi.fn(), scale: vi.fn(), fillText: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length*5 })),
    fillRect: vi.fn((x: number, y: number, w: number, h: number) => rectangles.push({ x, y, width: w, height: h, style: context.fillStyle })) };
  primitive.paneViews()[0]?.renderer()?.draw({ useBitmapCoordinateSpace: (callback: (scope: any) => void) => callback({
    context, mediaSize: { width, height }, bitmapSize: { width: width*rx, height: height*ry }, horizontalPixelRatio: rx, verticalPixelRatio: ry,
  }) } as any);
  return { context, rectangles: rectangles.filter(rect => rect.style !== "#101722") };
}

afterEach(() => vi.unstubAllGlobals());

describe("visible volume profile series primitive", () => {
  it("keeps remote mode blank until exact data arrives and reports ranges outside paint only", () => {
    const onVisibleRange = vi.fn(), data = fineChartBars(), test = fixture(data, { instrumentId: fineRequest.instrument_id, timeframe: "15m", onVisibleRange });
    test.flush(); expect(onVisibleRange).toHaveBeenCalledWith({ from: -.5, to: 1.5 });
    expect(test.changed).not.toHaveBeenCalled(); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
    const detail = fineFixture({ ...fineRequest, rows: 24 }); test.controller.setProfile(detail);
    expect(test.controller.result()).toBe(detail.profile); draw(test.primitive());
    expect(onVisibleRange).toHaveBeenCalledOnce(); expect(test.chart.addSeries).not.toHaveBeenCalled();
    test.range({ from: -.4, to: .4 }); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
    expect(draw(test.primitive()).rectangles).toEqual([]); test.flush();
    expect(onVisibleRange).toHaveBeenCalledTimes(2);
    test.range({ from: -.5, to: 1.5 }); test.flush(); test.controller.setProfile(detail); test.controller.update(data, { widthPercent: 10 }); test.flush();
    expect(test.controller.result()).toBe(detail.profile);
    test.controller.setProfile(undefined); expect(draw(test.primitive()).rectangles).toEqual([]);
  });
  it("includes the real source, estimate and incomplete time coverage in PNG canvas text", () => {
    const test = fixture(fineChartBars(), { instrumentId: fineRequest.instrument_id, timeframe: "15m", onVisibleRange: vi.fn() }); test.flush();
    const detail = { ...fineFixture({ ...fineRequest, rows: 24 }), status: "PARTIAL" as const, expected_bars: 17280, used_bars: 17279, coverage: 17279 / 17280 };
    test.controller.setProfile(detail); const rendered = draw(test.primitive());
    expect(rendered.context.fillText).toHaveBeenCalledWith(expect.stringContaining("1m OHLCV估算"), 10, 8, 380);
    expect(fineProfileCanvasLabel(detail)).toContain("99.994% · 部分数据");
    expect(fineProfileCanvasLabel(detail)).not.toContain("100%");
    const before = test.redraw.mock.calls.length; test.controller.detach(); test.controller.setProfile(detail);
    expect(test.redraw).toHaveBeenCalledTimes(before);
  });
  it("preserves remote geometry through screenshot layout jitter when the closed UTC window is unchanged", () => {
    const rangeChanged = vi.fn(), test = fixture(fineChartBars(), { instrumentId: fineRequest.instrument_id, timeframe: "15m", onVisibleRange: rangeChanged }); test.flush();
    const detail = fineFixture({ ...fineRequest, rows: 24 }); test.controller.setProfile(detail);
    // takeScreenshot hides the crosshair and synchronously recalculates layout,
    // before our next RAF / React commit can republish the profile.
    test.range({ from: -.5 + 1e-10, to: 1.5 - 1e-10 }, false); test.primitive().updateAllViews();
    expect(test.controller.result()).toBe(detail.profile);
    expect(draw(test.primitive()).rectangles.length).toBeGreaterThan(0);
    expect(rangeChanged).toHaveBeenCalledOnce(); test.flush();
    test.range({ from: -.4, to: 1.4 }); expect(test.controller.result()).toBe(detail.profile);
    test.range({ from: .6, to: 1.4 }, false); test.primitive().updateAllViews();
    expect(test.controller.result().status).toBe("NO_VISIBLE_BARS"); expect(draw(test.primitive()).rectangles).toEqual([]);
    test.controller.setProfile(detail); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
  });
  it("rejects stale remote identities and updates when rows or closed candle dates change", () => {
    const data = fineChartBars(), test = fixture(data, { instrumentId: fineRequest.instrument_id, timeframe: "15m", onVisibleRange: vi.fn() }); test.flush();
    const detail = fineFixture({ ...fineRequest, rows: 24 });
    test.controller.setProfile({ ...detail, instrument_id: "other" }); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
    test.controller.setProfile(detail); test.controller.update(data, { bins: 48 }); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
    test.controller.update(data, { bins: 24 }); test.controller.setProfile(detail);
    test.controller.update(data.map(bar => ({ ...bar, is_final: false }))); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
  });
  it("coalesces rapid viewport changes and publishes the legend outside rendering", () => {
    const test = fixture(rows(10));
    expect(test.changed).not.toHaveBeenCalled(); expect(test.frames.size).toBe(1);
    test.range({ from: 1, to: 4 }); test.range({ from: 2, to: 6 });
    expect(test.frames.size).toBe(1); test.flush();
    expect(test.controller.result().includedCount).toBe(5);
    expect(test.changed).toHaveBeenCalledTimes(1);
    expect(test.controller.result().firstIndex).toBe(2);
    draw(test.primitive()); test.primitive().updateAllViews();
    expect(test.changed).toHaveBeenCalledTimes(1); expect(test.frames.size).toBe(0);
    expect(test.primitive().paneViews()).toBe(test.primitive().paneViews());
  });

  it("recalculates when input candles/settings change, without adding data or touching autoscale", () => {
    const test = fixture(); test.flush();
    test.controller.update(rows(4).map((row, index) => ({ ...row, is_final: index !== 3 })), { bins: 48, widthPercent: 10 });
    expect(test.changed).toHaveBeenCalledTimes(1); test.flush();
    expect(test.controller.result().includedCount).toBe(3); expect(test.controller.result().effectiveBins).toBe(48);
    const drawn = draw(test.primitive());
    expect(Math.max(...drawn.rectangles.map(rect => rect.width))).toBe(40);
    expect(test.chart.addSeries).not.toHaveBeenCalled(); expect(test.series.setData).not.toHaveBeenCalled();
    expect(test.scale.fitContent).not.toHaveBeenCalled(); expect(test.scale.setVisibleLogicalRange).not.toHaveBeenCalled();
    expect("autoscaleInfo" in test.primitive()).toBe(false);
  });

  it("projects every bin edge through the actual series, including log/inverted price scales", () => {
    const test = fixture(); test.flush();
    const first = draw(test.primitive());
    expect(first.rectangles[0]).toMatchObject({ x: 292, y: 149, width: 100, height: 1 });
    expect(test.series.priceToCoordinate).toHaveBeenCalledWith(100);
    expect(test.series.priceToCoordinate).toHaveBeenCalledWith(101);
    test.projection(price => 600-100*Math.log(price));
    const logged = draw(test.primitive());
    expect(logged.rectangles[0].y).toBe(Math.round(600-100*Math.log(101)));
    expect(logged.context.moveTo.mock.calls[0][1]).toBeCloseTo(600-100*Math.log(100.5));
    test.projection(price => price);
    expect(draw(test.primitive()).rectangles[0].y).toBe(100);
    expect(test.changed).toHaveBeenCalledTimes(1);
  });

  it("uses actual pane dimensions and independent pixel ratios on every frame", () => {
    const test = fixture(); test.flush();
    const retina = draw(test.primitive(), 400, 300, 2, 3);
    expect(retina.rectangles[0]).toMatchObject({ x: 584, y: 447, width: 200, height: 3 });
    expect(retina.context.moveTo.mock.calls[0][1]).toBe(149.5*3);
    expect(retina.context.scale).toHaveBeenCalledWith(2, 3);
    const widePane = draw(test.primitive(), 800, 200, 1.5, 1.5);
    expect(widePane.rectangles[0]).toMatchObject({ x: 888, width: 300 });
    expect(widePane.rectangles.every(rect => rect.y >= 0 && rect.y+rect.height <= 301)).toBe(true);
    const calls = test.changed.mock.calls.length; test.resize();
    expect(test.changed).toHaveBeenCalledTimes(calls); expect(test.redraw).toHaveBeenCalled();
  });

  it("includes an estimate qualifier in exported canvas without leaving the profile area", () => {
    const test = fixture(); test.flush();
    const narrow = draw(test.primitive(), 320, 240, 2, 2);
    const shortLabel = narrow.context.fillText.mock.calls.find((call: unknown[]) => call[0] === "VPVR EST.");
    expect(shortLabel).toBeDefined();
    expect(shortLabel[1]).toBeGreaterThanOrEqual(312-80);
    expect(shortLabel[1]+shortLabel[3]).toBeLessThanOrEqual(312);
    expect(shortLabel[2]).toBeGreaterThanOrEqual(0); expect(shortLabel[2]).toBeLessThan(20);
    const wide = draw(test.primitive(), 800, 300);
    expect(wide.context.fillText.mock.calls.some((call: unknown[]) => call[0] === "VPVR · OHLCV estimate")).toBe(true);
    test.controller.update(rows(), { widthPercent: 10 }); test.flush();
    const smallest = draw(test.primitive(), 280, 240);
    const compressed = smallest.context.fillText.mock.calls.find((call: unknown[]) => call[0] === "VPVR EST.");
    expect(compressed[1]).toBeGreaterThanOrEqual(272-28);
    expect(compressed[1]+compressed[3]).toBeLessThanOrEqual(272);
    expect(compressed[3]).toBeGreaterThan(0);
  });

  it("omits unprojectable/offscreen prices and renders true flat-price bins without padding prices", () => {
    const test = fixture([{ ...rows(1)[0], low: 100, high: 100, open: 100, close: 100 }]); test.flush();
    const flat = draw(test.primitive());
    expect(flat.rectangles[0]).toMatchObject({ y: 149, height: 3 });
    expect(test.controller.result().bins[0]).toMatchObject({ low: 100, high: 100 });
    test.projection(() => null); expect(draw(test.primitive()).rectangles).toEqual([]);
    test.projection(() => NaN); expect(draw(test.primitive()).rectangles).toEqual([]);
    test.projection(() => 1000); expect(draw(test.primitive()).rectangles).toEqual([]);
  });

  it("handles a logical range changed during chart layout without synchronously calling React", () => {
    const test = fixture(rows(10)); test.flush();
    test.range({ from: 5, to: 7 }, false); test.primitive().updateAllViews();
    expect(test.changed).toHaveBeenCalledTimes(1); expect(test.frames.size).toBe(1);
    test.flush(); expect(test.controller.result().includedCount).toBe(3);
    test.range(null); test.flush(); expect(test.controller.result().status).toBe("NO_VISIBLE_BARS");
    expect(draw(test.primitive()).rectangles).toEqual([]);
  });

  it("cancels queued work, unsubscribes, and never calls back or redraws after detach", () => {
    const test = fixture();
    test.controller.detach(); test.controller.detach();
    expect(test.series.detachPrimitive).toHaveBeenCalledTimes(1);
    expect(test.cancel).toHaveBeenCalledTimes(1); expect(test.frames.size).toBe(0);
    expect(test.scale.unsubscribeVisibleLogicalRangeChange).toHaveBeenCalledWith(test.scale.subscribeVisibleLogicalRangeChange.mock.calls[0][0]);
    expect(test.scale.unsubscribeSizeChange).toHaveBeenCalledWith(test.scale.subscribeSizeChange.mock.calls[0][0]);
    test.flush(); test.resize(); test.range({ from: 1, to: 2 }); test.controller.update(rows());
    expect(test.frames.size).toBe(0); expect(test.changed).not.toHaveBeenCalled();
    expect(test.redraw).not.toHaveBeenCalled(); expect(test.primitive().paneViews()).toEqual([]);
  });
});
