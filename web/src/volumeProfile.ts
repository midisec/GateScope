/** Visible-range OHLCV estimate, NOT trade prints, bid/ask flow, or quote value.
 * Each candle's total volume is spread uniformly over its LINEAR high-low price
 * interval. Logarithmic chart rendering does not change that calculation.
 */
export const VOLUME_PROFILE_VERSION = "visible-ohlcv-profile-1";
export type VolumeProfileRows = 24 | 48 | 96;
export type VolumeProfileRange = { from: number; to: number };
export type VolumeProfileBar = {
  open_time: string; open: string | number; high: string | number; low: string | number;
  close: string | number; volume: string | number; is_final?: boolean; close_time?: string; available_at?: string;
};
export type VolumeProfileOptions = { bins?: VolumeProfileRows; valueArea?: number; widthPercent?: number };
export type ResolvedVolumeProfileOptions = { bins: VolumeProfileRows; valueArea: number; widthPercent: number };
export type VolumeProfileBin = {
  low: number; high: number; price: number; volume: number | null; volumeFraction: number;
  isPoc: boolean; inValueArea: boolean;
};
export type VolumeProfileResult = {
  version: typeof VOLUME_PROFILE_VERSION;
  method: "UNIFORM_LINEAR_PRICE_OHLCV_ESTIMATE";
  status: "AVAILABLE" | "NO_VISIBLE_BARS" | "NO_VALID_BARS" | "ZERO_VOLUME";
  bins: VolumeProfileBin[]; requestedBins: VolumeProfileRows; effectiveBins: number;
  valueArea: number; actualValueArea: number | null;
  poc: number | null; vah: number | null; val: number | null; pocIndex: number | null;
  /** Debug only; unit is unspecified in the supplied candle schema. */
  totalVolume: number | null;
  firstIndex: number | null; lastIndex: number | null; firstTime: string | null; lastTime: string | null;
  visibleCount: number; requestedVisibleCount: number; includedCount: number; positiveVolumeCount: number;
  excludedUnclosedCount: number; excludedInvalidCount: number; unknownFinalityCount: number;
  /** Valid closed/legacy candles among loaded visible candles, NOT a volume fraction. */
  coverage: number | null;
  /** Loaded visible slots / requested visible slots; whitespace is not invented data. */
  rangeCoverage: number | null;
  qualityFlags: string[];
};

/** Remote detail aggregates share drawing geometry, not the local data-source claim. */
export type FineVolumeProfileResult = Omit<VolumeProfileResult, "version" | "method"> & {
  version: "fine-ohlcv-profile-1";
  method: "LOWER_TIMEFRAME_OHLCV_ESTIMATE";
};
export type RenderableVolumeProfileResult = VolumeProfileResult | FineVolumeProfileResult;
export type FineVolumeProfileResponse = {
  version: "fine-ohlcv-profile-1"; instrument_id: string; timeframe: string;
  from_time: number; to_time: number; source_timeframe: string; source_step_seconds: number;
  expected_bars: number; used_bars: number; coverage: number;
  status: "AVAILABLE" | "PARTIAL" | "NO_DATA" | "ZERO_VOLUME";
  quality_flags: string[]; generated_at: number; expires_at: number;
  profile: FineVolumeProfileResult;
};

export function resolveVolumeProfileOptions(options: VolumeProfileOptions = {}): ResolvedVolumeProfileOptions {
  return {
    bins: options.bins === 24 || options.bins === 96 ? options.bins : 48,
    valueArea: Number.isFinite(options.valueArea) && options.valueArea! > 0 && options.valueArea! <= 1 ? options.valueArea! : .7,
    widthPercent: Number.isFinite(options.widthPercent) ? Math.min(40, Math.max(10, options.widthPercent!)) : 25,
  };
}

function numeric(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) return null;
  const result = Number(value);
  if (!Number.isFinite(result)) return null;
  // A nonzero decimal below IEEE-754 precision is not a zero-volume candle.
  if (result === 0 && typeof value === "string" && /[1-9]/.test(value.trim().split(/[eE]/)[0])) return null;
  return result;
}

function validCandle(bar: VolumeProfileBar) {
  if (bar.is_final != null && typeof bar.is_final !== "boolean") return null;
  const open = numeric(bar.open), high = numeric(bar.high), low = numeric(bar.low);
  const close = numeric(bar.close), volume = numeric(bar.volume);
  if (open == null || high == null || low == null || close == null || volume == null
    || low <= 0 || high < low || open < low || open > high || close < low || close > high || volume < 0) return null;
  return { high, low, volume };
}

/** Compensated accumulation; signed values are needed by the interior-bin sweep. */
function add(values: Float64Array, errors: Float64Array, index: number, amount: number) {
  const corrected = amount-errors[index], next = values[index]+corrected;
  errors[index] = (next-values[index])-corrected; values[index] = next;
}

function bound(edges: number[], value: number, strict: boolean) {
  let left = 0, right = edges.length;
  while (left < right) {
    const middle = (left+right) >>> 1;
    if (strict ? edges[middle] <= value : edges[middle] < value) left = middle+1;
    else right = middle;
  }
  return left;
}

/** Each bar occupies [index-.5,index+.5]; zero-width edge touches are excluded.
 * bars MUST have the same chronological index order as the candlestick series.
 * Unknown finality is counted and disclosed for legacy rows; explicit false is
 * always excluded. No missing candles or volume units are inferred.
 */
export function computeVolumeProfile(bars: readonly VolumeProfileBar[], visibleRange: VolumeProfileRange | null,
                                     options: VolumeProfileOptions = {}): VolumeProfileResult {
  const config = resolveVolumeProfileOptions(options);
  const result: VolumeProfileResult = {
    version: VOLUME_PROFILE_VERSION, method: "UNIFORM_LINEAR_PRICE_OHLCV_ESTIMATE", status: "NO_VISIBLE_BARS",
    bins: [], requestedBins: config.bins, effectiveBins: 0, valueArea: config.valueArea, actualValueArea: null,
    poc: null, vah: null, val: null, pocIndex: null, totalVolume: 0,
    firstIndex: null, lastIndex: null, firstTime: null, lastTime: null,
    visibleCount: 0, requestedVisibleCount: 0, includedCount: 0, positiveVolumeCount: 0,
    excludedUnclosedCount: 0, excludedInvalidCount: 0, unknownFinalityCount: 0,
    coverage: null, rangeCoverage: null, qualityFlags: [],
  };
  if (!visibleRange || !Number.isFinite(visibleRange.from) || !Number.isFinite(visibleRange.to)
    || visibleRange.from > visibleRange.to) return result;
  const firstSlot = Math.floor(visibleRange.from-.5)+1, lastSlot = Math.ceil(visibleRange.to+.5)-1;
  if (!Number.isSafeInteger(firstSlot) || !Number.isSafeInteger(lastSlot)) {
    result.qualityFlags.push("LOGICAL_RANGE_EXCEEDS_SAFE_COUNT"); return result;
  }
  if (lastSlot < firstSlot) return result;
  result.requestedVisibleCount = Math.min(Number.MAX_SAFE_INTEGER, lastSlot-firstSlot+1);
  const first = Math.max(0, firstSlot), last = Math.min(bars.length-1, lastSlot);
  if (firstSlot < 0 || lastSlot >= bars.length) result.qualityFlags.push("VISIBLE_RANGE_OUTSIDE_LOADED_DATA");
  if (first > last) { result.rangeCoverage = 0; return result; }
  result.firstIndex = first; result.lastIndex = last;
  result.firstTime = bars[first].open_time; result.lastTime = bars[last].open_time;
  result.visibleCount = last-first+1;
  result.rangeCoverage = result.visibleCount/result.requestedVisibleCount;
  let minimum = Infinity, maximum = -Infinity, maxVolume = 0;
  // Two bounded-memory passes, never copy a year of visible candles into another array.
  for (let index = first; index <= last; index++) {
    const bar = bars[index];
    if (bar.is_final === false) { result.excludedUnclosedCount++; continue; }
    const candle = validCandle(bar);
    if (!candle) { result.excludedInvalidCount++; continue; }
    result.includedCount++;
    if (bar.is_final == null) result.unknownFinalityCount++;
    if (candle.volume > 0) result.positiveVolumeCount++;
    minimum = Math.min(minimum, candle.low); maximum = Math.max(maximum, candle.high);
    maxVolume = Math.max(maxVolume, candle.volume);
  }
  result.coverage = result.includedCount/result.visibleCount;
  if (result.unknownFinalityCount) result.qualityFlags.push("LEGACY_FINALITY_NOT_PROVIDED");
  if (!result.includedCount) { result.status = "NO_VALID_BARS"; return result; }
  const span = maximum-minimum;
  const edges = [minimum];
  if (span > 0) {
    for (let index = 1; index <= config.bins; index++) {
      const edge = index === config.bins ? maximum : minimum+span*(index/config.bins);
      if (edge > edges[edges.length-1]) edges.push(edge);
    }
    if (edges.length !== config.bins+1) result.qualityFlags.push("PRICE_BINS_COLLAPSED_AT_FLOAT_PRECISION");
  } else edges.push(maximum); // One true flat-price bin; no invented tick or price range.
  const count = edges.length-1;
  result.effectiveBins = count;
  result.bins = Array.from({ length: count }, (_, index) => ({ low: edges[index], high: edges[index+1],
    price: edges[index]+(edges[index+1]-edges[index])/2, volume: 0, volumeFraction: 0, isPoc: false, inValueArea: false }));
  if (maxVolume === 0) { result.status = "ZERO_VOLUME"; return result; }

  const mass = new Float64Array(count), massError = new Float64Array(count);
  const delta = new Float64Array(count+1), deltaError = new Float64Array(count+1);
  const totals = new Float64Array(1), totalError = new Float64Array(1);
  let limitedVolumePrecision = false;
  for (let index = first; index <= last; index++) {
    if (bars[index].is_final === false) continue;
    const candle = validCandle(bars[index]);
    if (!candle || candle.volume === 0) continue;
    const weight = candle.volume/maxVolume;
    if (weight === 0) { limitedVolumePrecision = true; continue; }
    add(totals, totalError, 0, weight);
    const from = Math.max(0, Math.min(count-1, bound(edges, candle.low, true)-1));
    const to = Math.max(from, Math.min(count-1, bound(edges, candle.high, false)-1));
    if (from === to || candle.high === candle.low) { add(mass, massError, from, weight); continue; }
    const width = candle.high-candle.low;
    const head = Math.min(weight, Math.max(0, weight*((edges[from+1]-candle.low)/width)));
    add(mass, massError, from, head);
    if (to === from+1) { add(mass, massError, to, weight-head); continue; }
    const tail = Math.min(weight-head, Math.max(0, weight*((candle.high-edges[to])/width)));
    add(mass, massError, to, tail);
    // Complete interior bins receive density * linear price width. The sweep
    // avoids O(candles*bins) work while preserving fractional edge allocations.
    const remaining = Math.max(0, weight-head-tail);
    const density = remaining/((edges[to]-edges[from+1])/span);
    if (Number.isFinite(density)) {
      add(delta, deltaError, from+1, density); add(delta, deltaError, to, -density);
    } else {
      limitedVolumePrecision = true;
      for (let bin = from+1; bin < to; bin++)
        add(mass, massError, bin, remaining*((edges[bin+1]-edges[bin])/(edges[to]-edges[from+1])));
    }
  }
  let density = 0, densityError = 0, accumulated = 0, accumulatedError = 0;
  for (let index = 0; index < count; index++) {
    const corrected = delta[index]-densityError, next = density+corrected;
    densityError = (next-density)-corrected; density = next;
    const interior = span > 0 ? Math.max(0, density)*((edges[index+1]-edges[index])/span) : 0;
    add(mass, massError, index, interior);
    const amount = mass[index]-accumulatedError, sum = accumulated+amount;
    accumulatedError = (sum-accumulated)-amount; accumulated = sum;
  }
  if (limitedVolumePrecision || Math.abs(accumulated-totals[0]) > totals[0]*1e-10)
    result.qualityFlags.push("VOLUME_FLOAT_PRECISION_LIMIT");
  const totalVolume = totals[0]*maxVolume;
  result.totalVolume = Number.isFinite(totalVolume) ? totalVolume : null;
  if (result.totalVolume == null) result.qualityFlags.push("TOTAL_VOLUME_EXCEEDS_NUMBER_RANGE");
  // Fractions remain finite even when the total cannot be represented. Debug
  // bin amounts are null in that case, never Infinity or mislabeled as USD.
  let binUnderflow = false;
  for (let index = 0; index < count; index++) {
    const fraction = mass[index]/accumulated;
    result.bins[index].volumeFraction = fraction;
    const amount = result.totalVolume == null ? null : fraction*result.totalVolume;
    if (amount === 0 && fraction > 0) { result.bins[index].volume = null; binUnderflow = true; }
    else result.bins[index].volume = amount;
  }
  if (binUnderflow) result.qualityFlags.push("BIN_VOLUME_BELOW_NUMBER_RANGE");
  let poc = 0;
  for (let index = 1; index < count; index++) {
    const previous = result.bins[poc].volumeFraction, candidate = result.bins[index].volumeFraction;
    if (candidate > previous+Math.max(previous, candidate)*1e-12) poc = index;
  }
  // A documented deterministic value area: start at the POC and add the larger
  // adjacent bin, lower-price side on ties, until the requested fraction is met.
  let lower = poc, upper = poc, covered = result.bins[poc].volumeFraction;
  while (covered+1e-12 < config.valueArea && (lower > 0 || upper < count-1)) {
    const below = lower > 0 ? result.bins[lower-1].volumeFraction : -1;
    const above = upper < count-1 ? result.bins[upper+1].volumeFraction : -1;
    if (below >= above-Math.max(Math.abs(below), Math.abs(above))*1e-12) { lower--; covered += below; }
    else { upper++; covered += above; }
  }
  result.status = "AVAILABLE"; result.pocIndex = poc; result.poc = result.bins[poc].price;
  result.val = result.bins[lower].low; result.vah = result.bins[upper].high;
  result.actualValueArea = Math.min(1, covered);
  result.bins[poc].isPoc = true;
  for (let index = lower; index <= upper; index++) result.bins[index].inValueArea = true;
  return result;
}
