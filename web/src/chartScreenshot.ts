import type { IChartApi } from "lightweight-charts";
import type { Bar } from "./types";
import type { ChartStudiesSettings } from "./chartStudies";
import type { FineVolumeProfileView } from "./useFineVolumeProfile";
import type { VolumeProfileController } from "./volumeProfilePrimitive";
import { fineProfileKey, fineVolumeProfileWindow, validateFineProfile, type FineProfileRequest } from "./fineVolumeProfileModel";

export type ChartScreenshotState = {
  chart: IChartApi | null; controller: VolumeProfileController | null; bars: readonly Bar[];
  chartKey: string; instrumentId?: string; timeframe: string; scope?: string;
  vpvr?: ChartStudiesSettings["vpvr"]; fine: Pick<FineVolumeProfileView, "phase" | "bundle" | "error">;
};
function captureWindow(state: ChartScreenshotState): FineProfileRequest | undefined {
  if (!state.vpvr?.enabled) return undefined;
  const visible = state.chart?.timeScale().getVisibleLogicalRange();
  const range = fineVolumeProfileWindow(state.bars, visible ?? null, state.timeframe);
  return range && state.instrumentId ? { instrument_id: state.instrumentId, timeframe: state.timeframe, ...range,
    rows: state.vpvr.rows, value_area: state.vpvr.valueAreaPercent } : undefined;
}
function identity(state: ChartScreenshotState, request?: FineProfileRequest) {
  return JSON.stringify([state.scope, state.chartKey, state.vpvr?.enabled === true, request ? fineProfileKey(request, state.scope) : null]);
}
function nextFrame() {
  return new Promise<void>(resolve => {
    let frame: number | undefined, timer: ReturnType<typeof setTimeout> | undefined, done = false;
    const finish = () => { if (done) return; done = true; if (frame !== undefined) cancelAnimationFrame(frame); clearTimeout(timer); resolve(); };
    timer = setTimeout(finish, 100); frame = requestAnimationFrame(finish);
  });
}

/** Wait for the CURRENT selected detail layer. Never download a silently missing VPVR. */
export async function captureChartScreenshot(read: () => ChartScreenshotState,
  options: { waitFrame?: () => Promise<void>; now?: () => number; timeoutMs?: number } = {}): Promise<HTMLCanvasElement> {
  const first = read(), chart = first.chart;
  if (!chart) throw new Error("图表尚未就绪");
  if (!first.vpvr?.enabled) return chart.takeScreenshot(true, false);
  const expected = captureWindow(first);
  if (!expected) throw new Error("可视区间暂无已收盘 K 线，无法导出 VPVR");
  const frozen = identity(first, expected), now = options.now ?? Date.now, wait = options.waitFrame ?? nextFrame;
  const deadline = now() + Math.max(1, Math.min(8000, options.timeoutMs ?? 8000));
  const current = () => {
    const value = read();
    if (value.chart !== chart || identity(value, captureWindow(value)) !== frozen)
      throw new Error("导出期间图表区间或指标已切换，请重新导出");
    if (typeof document !== "undefined" && document.hidden) throw new Error("请回到前台后重新导出图表");
    return value;
  };
  while (now() < deadline) {
    const state = current(), bundle = state.fine.bundle, controller = state.controller;
    if (["ERROR", "COOLDOWN", "PAUSED"].includes(state.fine.phase))
      throw new Error(state.fine.error || "VPVR 尚未就绪，请等待数据恢复后重新导出");
    if (bundle && controller) {
      let valid = false;
      try { validateFineProfile(bundle, expected, now() / 1000); valid = true; } catch { /* Wait for the currently bound refresh, never reuse another window. */ }
      if (valid) {
        if (bundle.profile.status !== "AVAILABLE") throw new Error("该区间暂无可绘制的 VPVR，请关闭该指标后导出或选择其他区间");
        controller.setProfile(bundle);
        // Clearing the position invalidates only the cursor layer. In LWC,
        // takeScreenshot(..., false) hides it through applyOptions, forcing a
        // full price-axis layout that can change the actual visible UTC window.
        chart.clearCrosshairPosition();
        await wait();
        if (now() >= deadline) break;
        const ready = current();
        if (ready.controller !== controller || ready.fine.bundle !== bundle || controller.result() !== bundle.profile) continue;
        try { validateFineProfile(bundle, expected, now() / 1000); } catch { continue; }
        // A pointer may have moved during the awaited frame. Clear once more
        // synchronously, then include the now-empty cursor layer without the
        // screenshot helper's full-layout side effect.
        chart.clearCrosshairPosition();
        const canvas = chart.takeScreenshot(true, true);
        current();
        if (controller.result() !== bundle.profile) throw new Error("截图时 VPVR 区间发生变化，请重新导出");
        try { validateFineProfile(bundle, expected, now() / 1000); } catch { continue; }
        return canvas;
      }
    }
    await wait();
  }
  throw new Error("VPVR 仍在更新，暂未导出；请就绪后重试");
}
