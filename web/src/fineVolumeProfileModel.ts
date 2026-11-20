import type { Bar } from "./types";
import type { FineVolumeProfileResponse, VolumeProfileRange, VolumeProfileRows } from "./volumeProfile";

export const FINE_PROFILE_STEPS: Record<string, number> = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
  "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "1w": 604800 };
export type FineProfileRequest = { instrument_id: string; timeframe: string; from_time: number; to_time: number;
  rows: VolumeProfileRows; value_area: number };

/** Half-open UTC bounds of actual closed visible chart candles; never all history. */
export function fineVolumeProfileWindow(bars: readonly Pick<Bar, "open_time" | "close_time" | "available_at" | "is_final">[], range: VolumeProfileRange | null, timeframe: string, now = Date.now() / 1000) {
  const step = FINE_PROFILE_STEPS[timeframe];
  if (!range || !step || !Number.isFinite(now) || !Number.isFinite(range.from) || !Number.isFinite(range.to) || range.from > range.to) return null;
  const firstSlot = Math.floor(range.from - .5) + 1, lastSlot = Math.ceil(range.to + .5) - 1;
  if (!Number.isSafeInteger(firstSlot) || !Number.isSafeInteger(lastSlot)) return null;
  const first = Math.max(0, firstSlot), last = Math.min(bars.length - 1, lastSlot);
  if (first > last) return null;
  let from = Infinity, to = -Infinity;
  for (let index = first; index <= last; index++) {
    const bar = bars[index], open = Date.parse(bar.open_time) / 1000, close = Date.parse(bar.close_time ?? "") / 1000;
    const available = bar.available_at ? Date.parse(bar.available_at) / 1000 : close;
    if (bar.is_final !== true || !Number.isSafeInteger(open) || open <= 0 || close !== open + step || close > now || available < close || available > now || !Number.isFinite(available)) continue;
    from = Math.min(from, open); to = Math.max(to, close);
  }
  return Number.isFinite(from) && to > from ? { from_time: from, to_time: to } : null;
}

export function fineProfileKey(request: FineProfileRequest, scope = "") {
  return JSON.stringify([scope, request.instrument_id, request.timeframe, request.from_time, request.to_time, request.rows, request.value_area]);
}
export function fineProfilePath(request: FineProfileRequest) {
  return `/api/v2/market/volume-profile?${new URLSearchParams(Object.entries(request).map(([key, value]) => [key, String(value)]))}`;
}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const count = (value: unknown): value is number => finite(value) && Number.isInteger(value) && value >= 0;
const strings = (value: unknown) => Array.isArray(value) && value.length <= 100 && value.every(item => typeof item === "string" && item.length <= 200);

/** API success is not enough: reject another symbol/window, incompatible units or malformed geometry. */
export function validateFineProfile(value: unknown, request: FineProfileRequest, now = Date.now() / 1000): FineVolumeProfileResponse {
  const fail = () => { throw new Error("细周期分布响应与当前区间不一致或数据无效"); };
  if (!object(value)) return fail();
  const p = value.profile, step = FINE_PROFILE_STEPS[String(value.source_timeframe)];
  if (value.version !== "fine-ohlcv-profile-1" || value.instrument_id !== request.instrument_id || value.timeframe !== request.timeframe
    || value.from_time !== request.from_time || value.to_time !== request.to_time || !step || step !== value.source_step_seconds || step > FINE_PROFILE_STEPS[request.timeframe]
    || !count(value.expected_bars) || value.expected_bars > 50000 || !count(value.used_bars) || value.used_bars > value.expected_bars
    || !finite(value.coverage) || Math.abs(value.coverage - (value.expected_bars ? value.used_bars / value.expected_bars : 0)) > 1e-9
    || !["AVAILABLE", "PARTIAL", "NO_DATA", "ZERO_VOLUME"].includes(String(value.status)) || !strings(value.quality_flags)
    || !count(value.generated_at) || value.generated_at > now + 5 || value.generated_at + 30 <= now || !count(value.expires_at) || value.expires_at <= value.generated_at || value.expires_at <= now
    || !object(p) || p.version !== value.version || p.method !== "LOWER_TIMEFRAME_OHLCV_ESTIMATE"
    || p.requestedBins !== request.rows || !finite(p.valueArea) || Math.abs(p.valueArea - request.value_area / 100) > 1e-12
    || !count(p.includedCount) || p.includedCount !== value.used_bars || !count(p.effectiveBins) || p.effectiveBins > request.rows
    || !Array.isArray(p.bins) || p.bins.length !== p.effectiveBins || !strings(p.qualityFlags)
    || !["AVAILABLE", "NO_VISIBLE_BARS", "NO_VALID_BARS", "ZERO_VOLUME"].includes(String(p.status))) return fail();
  if ((value.status === "AVAILABLE" && (value.coverage !== 1 || p.status !== "AVAILABLE"))
    || (value.status === "PARTIAL" && !(value.coverage > 0 && value.coverage <= 1))
    || (value.status === "NO_DATA" && (value.used_bars !== 0 || p.status === "AVAILABLE" || p.status === "ZERO_VOLUME"))
    || (value.status === "ZERO_VOLUME" && (value.used_bars === 0 || p.status !== "ZERO_VOLUME"))) return fail();
  let total = 0, previousHigh: number | undefined;
  for (const bin of p.bins) {
    if (!object(bin) || !finite(bin.low) || bin.low <= 0 || !finite(bin.high) || bin.high < bin.low
      || !finite(bin.price) || bin.price < bin.low || bin.price > bin.high || !finite(bin.volumeFraction) || bin.volumeFraction < 0 || bin.volumeFraction > 1
      || (bin.volume !== null && (!finite(bin.volume) || bin.volume < 0)) || typeof bin.isPoc !== "boolean" || typeof bin.inValueArea !== "boolean"
      || (previousHigh !== undefined && bin.low !== previousHigh)) return fail();
    previousHigh = bin.high; total += bin.volumeFraction;
  }
  if (p.status === "AVAILABLE" && (!value.used_bars || !p.bins.length || Math.abs(total - 1) > 1e-8 || !count(p.pocIndex) || p.pocIndex >= p.bins.length
    || !finite(p.poc) || !finite(p.val) || !finite(p.vah) || p.val > p.poc || p.poc > p.vah
    || (p.bins[p.pocIndex] as { price: number }).price !== p.poc)) return fail();
  if (p.status !== "AVAILABLE" && (p.poc !== null || p.val !== null || p.vah !== null || p.pocIndex !== null || total !== 0)) return fail();
  if (value.used_bars > 0) {
    const first = Date.parse(String(p.firstTime)) / 1000, last = Date.parse(String(p.lastTime)) / 1000;
    if (!Number.isFinite(first) || !Number.isFinite(last) || first < request.from_time || last < first || last + step > request.to_time || last + step > value.generated_at) return fail();
  }
  return value as unknown as FineVolumeProfileResponse;
}

/** Tiny per-mounted-chart cache; never persists user-bound API responses globally. */
export class FineVolumeProfileCache {
  private entries = new Map<string, { value: FineVolumeProfileResponse; expires: number }>();
  constructor(private maximum = 8) {}
  get(key: string, now = Date.now()) {
    const item = this.entries.get(key);
    if (!item || item.expires <= now) { this.entries.delete(key); return undefined; }
    this.entries.delete(key); this.entries.set(key, item); return item.value;
  }
  put(key: string, value: FineVolumeProfileResponse, now = Date.now()) {
    this.entries.delete(key); this.entries.set(key, { value, expires: Math.min(now + 30000, (value.generated_at + 30) * 1000, value.expires_at * 1000) });
    while (this.entries.size > this.maximum) this.entries.delete(this.entries.keys().next().value!);
  }
  clear() { this.entries.clear(); }
}
export function fineRetryAt(header: string | null, now = Date.now()) {
  const seconds = header !== null && /^\d+(?:\.\d+)?$/.test(header.trim()) ? Number(header) : NaN;
  const date = header === null ? NaN : Date.parse(header);
  const at = Number.isFinite(seconds) ? now + Math.ceil(seconds * 1000) : date;
  return Number.isFinite(at) && at > now ? at : now + 30000;
}
export function fineCoverageLabel(coverage: number) {
  if (!Number.isFinite(coverage) || coverage < 0 || coverage > 1) return "—";
  if (coverage === 1) return "100%";
  if (coverage === 0) return "0%";
  const value = (coverage * 100).toFixed(3);
  return Number(value) >= 100 ? "<100%" : `${value}%`;
}
export function fineProfileIsPartial(value: FineVolumeProfileResponse) {
  return value.status === "PARTIAL" || value.coverage < 1 || value.quality_flags.some(flag => ["PARTIAL_SOURCE_BOUNDARIES", "FUTURE_RANGE_NOT_CLOSED"].includes(flag));
}
