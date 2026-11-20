import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { IChartApi, IPrimitivePaneView, ISeriesApi, ISeriesPrimitive, SeriesAttachedParameter, Time } from "lightweight-charts";
import { fineCoverageLabel, fineProfileIsPartial, fineVolumeProfileWindow } from "./fineVolumeProfileModel";
import { computeVolumeProfile, resolveVolumeProfileOptions, type ResolvedVolumeProfileOptions,
  type VolumeProfileBar, type VolumeProfileOptions, type VolumeProfileRange, type VolumeProfileResult,
  type RenderableVolumeProfileResult, type FineVolumeProfileResponse } from "./volumeProfile";

export type VolumeProfileChange = (result: VolumeProfileResult) => void;
export type VolumeProfileController = {
  update: (bars: readonly VolumeProfileBar[], options?: VolumeProfileOptions) => void;
  result: () => RenderableVolumeProfileResult;
  setProfile: (value: FineVolumeProfileResponse | undefined) => void;
  detach: () => void;
};
export type RemoteVolumeProfileOptions = { instrumentId: string; timeframe: string; onVisibleRange: (range: VolumeProfileRange | null) => void };
export function fineProfileCanvasLabel(value: FineVolumeProfileResponse) {
  const partial = fineProfileIsPartial(value);
  return `VPVR · ${value.source_timeframe} OHLCV估算 · 时间覆盖 ${fineCoverageLabel(value.coverage)}${partial ? " · 部分数据" : ""}`;
}

const finiteCoordinate = (value: number | null): value is number => value != null && Number.isFinite(value);
const sameRange = (a: VolumeProfileRange | null, b: VolumeProfileRange | null) => a?.from === b?.from && a?.to === b?.to;

/** A series primitive draws in its actual pane and price scale, not a DOM layer.
 * Never adds a series, invents timestamps, or supplies autoscaleInfo. Scrolling
 * calculations are coalesced into RAF; render-time callbacks never update React.
 */
export class VolumeProfilePrimitive implements ISeriesPrimitive<Time> {
  private bars: readonly VolumeProfileBar[];
  private options: ResolvedVolumeProfileOptions;
  private profile: RenderableVolumeProfileResult;
  private remoteProfile: FineVolumeProfileResponse | undefined;
  private chart: SeriesAttachedParameter<Time>["chart"] | undefined;
  private series: SeriesAttachedParameter<Time>["series"] | undefined;
  private requestUpdate: (() => void) | undefined;
  private frame: number | null = null;
  private dirty = true;
  private lastRange: VolumeProfileRange | null = null;
  private readonly emptyViews: readonly IPrimitivePaneView[] = [];
  private readonly views: readonly IPrimitivePaneView[] = [{
    zOrder: () => "top", renderer: () => ({ draw: target => this.draw(target) }),
  }];
  private readonly rangeChanged = () => {
    if (this.remoteProfile && !this.matchesRemote(this.remoteProfile)) { this.clearRemote(); this.requestUpdate?.(); }
    this.dirty = true; this.schedule();
  };
  private readonly sizeChanged = () => { this.requestUpdate?.(); };

  constructor(bars: readonly VolumeProfileBar[], options: VolumeProfileOptions = {}, private readonly onChange?: VolumeProfileChange,
    private readonly remote?: RemoteVolumeProfileOptions) {
    this.bars = bars;
    this.options = resolveVolumeProfileOptions(options);
    this.profile = computeVolumeProfile([], null, this.options);
  }

  attached(parameters: SeriesAttachedParameter<Time>): void {
    // Be defensive if a caller reuses the same primitive on another series.
    this.detached();
    this.chart = parameters.chart; this.series = parameters.series; this.requestUpdate = parameters.requestUpdate;
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(this.rangeChanged);
    this.chart.timeScale().subscribeSizeChange(this.sizeChanged);
    this.dirty = true; this.schedule();
  }

  detached(): void {
    if (this.frame != null) cancelAnimationFrame(this.frame);
    this.frame = null;
    if (this.chart) {
      this.chart.timeScale().unsubscribeVisibleLogicalRangeChange(this.rangeChanged);
      this.chart.timeScale().unsubscribeSizeChange(this.sizeChanged);
    }
    this.chart = undefined; this.series = undefined; this.requestUpdate = undefined;
    this.lastRange = null;
  }

  update(bars: readonly VolumeProfileBar[], options?: VolumeProfileOptions): void {
    this.bars = bars;
    this.options = resolveVolumeProfileOptions({ ...this.options, ...options });
    if (this.remoteProfile && !this.matchesRemote(this.remoteProfile)) this.clearRemote();
    this.dirty = true; this.schedule();
  }

  private clearRemote() { this.remoteProfile = undefined; this.profile = computeVolumeProfile([], null, this.options); }
  private matchesRemote(value: FineVolumeProfileResponse) {
    if (!this.remote) return false;
    const range = fineVolumeProfileWindow(this.bars, this.visibleRange(), this.remote.timeframe);
    return range !== null && value.instrument_id === this.remote.instrumentId && value.timeframe === this.remote.timeframe
      && value.from_time === range.from_time && value.to_time === range.to_time
      && value.profile.requestedBins === this.options.bins && Math.abs(value.profile.valueArea - this.options.valueArea) < 1e-12;
  }
  setProfile(value: FineVolumeProfileResponse | undefined): void {
    if (!this.remote) return;
    if (value && this.matchesRemote(value)) { this.remoteProfile = value; this.profile = value.profile; } else this.clearRemote();
    this.requestUpdate?.();
  }
  result(): RenderableVolumeProfileResult { return this.profile; }
  paneViews(): readonly IPrimitivePaneView[] { return this.series ? this.views : this.emptyViews; }

  updateAllViews(): void {
    // Price-scale/log/pane-height changes only require reprojection in draw().
    // If an axis change also changes the logical range, defer the calculation.
    if (this.chart && !sameRange(this.visibleRange(), this.lastRange)) {
      // Layout/screenshot can perturb logical floats without changing any
      // selected candle. Invalidate by actual closed UTC window, not pixels.
      if (this.remoteProfile && !this.matchesRemote(this.remoteProfile)) this.clearRemote();
      this.dirty = true; this.schedule();
    }
  }

  private visibleRange(): VolumeProfileRange | null {
    const range = this.chart?.timeScale().getVisibleLogicalRange();
    return range ? { from: range.from, to: range.to } : null;
  }

  private schedule(): void {
    if (!this.chart || this.frame != null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      if (!this.chart || !this.series || !this.dirty) return;
      this.dirty = false; this.lastRange = this.visibleRange();
      // This callback is outside renderer/updateAllViews and may safely update
      // the React legend. Detach during it suppresses the pending chart redraw.
      try {
        if (this.remote) this.remote.onVisibleRange(this.lastRange);
        else { const result = computeVolumeProfile(this.bars, this.lastRange, this.options); this.profile = result; this.onChange?.(result); }
      } finally { this.requestUpdate?.(); }
    });
  }

  private draw(target: CanvasRenderingTarget2D): void {
    const series = this.series, result = this.profile;
    if (!series || result.status !== "AVAILABLE" || !result.bins.length) return;
    target.useBitmapCoordinateSpace(({ context: ctx, mediaSize, bitmapSize, horizontalPixelRatio: rx, verticalPixelRatio: ry }) => {
      if (!(rx > 0 && ry > 0 && mediaSize.width > 0 && mediaSize.height > 0)
        || !Number.isFinite(rx) || !Number.isFinite(ry)) return;
      const right = Math.max(0, mediaSize.width-8), width = Math.min(right, mediaSize.width*this.options.widthPercent/100);
      if (width <= 0) return;
      const left = right-width, peak = Math.max(...result.bins.map(bin => bin.volumeFraction));
      if (!(peak > 0)) return;
      ctx.save();
      try {
        ctx.beginPath(); ctx.rect(0, 0, bitmapSize.width, bitmapSize.height); ctx.clip();
        // Every edge is reprojected through the owning candlestick scale on
        // every frame. Linear interpolation of pixel Y would be wrong on log.
        for (const bin of result.bins) {
          if (!(bin.volumeFraction > 0)) continue;
          const low = series.priceToCoordinate(bin.low), high = series.priceToCoordinate(bin.high);
          if (!finiteCoordinate(low) || !finiteCoordinate(high)) continue;
          const flat = bin.low === bin.high;
          const top = Math.max(0, Math.min(low, high)-(flat ? 1.5 : 0));
          const bottom = Math.min(mediaSize.height, Math.max(low, high)+(flat ? 1.5 : 0));
          if (bottom <= top) continue;
          const x = Math.round((right-width*bin.volumeFraction/peak)*rx), edge = Math.round(right*rx);
          const y = Math.round(top*ry), height = Math.max(1, Math.round(bottom*ry)-y);
          ctx.globalAlpha = bin.isPoc ? .88 : bin.inValueArea ? .64 : .34;
          ctx.fillStyle = bin.isPoc ? "#e6bb65" : bin.inValueArea ? "#58a7d7" : "#687e9d";
          ctx.fillRect(x, y, Math.max(1, edge-x), height);
        }
        ctx.globalAlpha = .95;
        const labels: number[] = [];
        for (const [label, price, color] of [["POC", result.poc, "#ebc577"], ["VAH", result.vah, "#9dc5dd"], ["VAL", result.val, "#9dc5dd"]] as const) {
          if (price == null) continue;
          const y = series.priceToCoordinate(price);
          if (!finiteCoordinate(y) || y < 0 || y > mediaSize.height) continue;
          ctx.strokeStyle = color; ctx.lineWidth = Math.max(1, ry);
          ctx.setLineDash(label === "POC" ? [] : [4*rx, 3*rx]);
          ctx.beginPath(); ctx.moveTo(left*rx, y*ry); ctx.lineTo(right*rx, y*ry); ctx.stroke();
          if (width < 48 || labels.some(previous => Math.abs(previous-y) < 15)) continue;
          labels.push(y);
          // Text coordinates use media units, avoiding stretched typography on
          // asymmetric/fractional DPR. Price lines themselves never move.
          ctx.save(); ctx.scale(rx, ry); ctx.setLineDash([]);
          ctx.font = "600 10px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
          const textY = Math.max(12, Math.min(mediaSize.height-2, y-3));
          ctx.fillStyle = "#101722"; ctx.fillRect(left+2, textY-11, 28, 12);
          ctx.fillStyle = color; ctx.fillText(label, left+4, textY); ctx.restore();
        }
        if (this.remoteProfile) {
          // Canvas labels are included in PNG exports, unlike DOM-only readouts.
          ctx.save(); ctx.scale(rx, ry); ctx.globalAlpha = 1; ctx.font = "10px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
          ctx.fillStyle = "#101722dd"; ctx.fillRect(6, 5, Math.max(0, mediaSize.width - 12), 18);
          ctx.fillStyle = fineProfileIsPartial(this.remoteProfile) ? "#e8bb70" : "#b6d1e7";
          ctx.fillText(fineProfileCanvasLabel(this.remoteProfile), 10, 8, Math.max(1, mediaSize.width - 20)); ctx.restore();
        } else {
        // The React legend is absent from chart.takeScreenshot(). Keep the
        // estimate qualifier on canvas too, confined to the profile's area.
        ctx.save(); ctx.scale(rx, ry);
        ctx.font = "500 9px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
        const fullLabel = "VPVR · OHLCV estimate";
        const label = ctx.measureText(fullLabel).width+8 <= width ? fullLabel : "VPVR EST.";
        const labelWidth = Math.min(width, ctx.measureText(label).width+8);
        const labelTop = Math.max(0, Math.min(4, mediaSize.height-16));
        ctx.globalAlpha = .88; ctx.fillStyle = "#101722";
        ctx.fillRect(right-labelWidth, labelTop, labelWidth, Math.min(16, mediaSize.height));
        ctx.fillStyle = "#bed0df";
        ctx.fillText(label, right-labelWidth+4, labelTop+3, Math.max(1, labelWidth-8));
        ctx.restore();
        }
      } finally { ctx.restore(); }
    });
  }
}

/** Keep bars aligned with the main candle series; call update after setData or
 * a final/live candle update. The chart argument matches the other overlay APIs;
 * attached() receives the authoritative owning chart/series from Lightweight.
 * Detach before removing the candlestick series or destroying the chart.
 */
export function attachVolumeProfile(_chart: IChartApi, candles: ISeriesApi<"Candlestick">,
                                    bars: readonly VolumeProfileBar[], options: VolumeProfileOptions = {},
                                    onChange?: VolumeProfileChange, remote?: RemoteVolumeProfileOptions): VolumeProfileController {
  const primitive = new VolumeProfilePrimitive(bars, options, onChange, remote);
  candles.attachPrimitive(primitive);
  let detached = false;
  return {
    update: (next, config) => { if (!detached) primitive.update(next, config); },
    result: () => primitive.result(),
    setProfile: value => { if (!detached) primitive.setProfile(value); },
    detach: () => {
      if (detached) return;
      detached = true; candles.detachPrimitive(primitive);
    },
  };
}
