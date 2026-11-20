import { describe, expect, it } from "vitest";
import { computeVolumeProfile, resolveVolumeProfileOptions, type VolumeProfileBar, type VolumeProfileRows } from "./volumeProfile";

function bar(low = 100, high = 124, volume: number | string = 240, index = 0): VolumeProfileBar {
  return { open_time: new Date(index*60_000).toISOString(), low, high, open: low+(high-low)/2,
    close: low+(high-low)/2, volume, is_final: true };
}
const full = (bars: readonly VolumeProfileBar[], bins: VolumeProfileRows = 48, valueArea = .7) =>
  computeVolumeProfile(bars, { from: -.5, to: bars.length-.5 }, { bins, valueArea });
const fractionSum = (result: ReturnType<typeof full>) => result.bins.reduce((sum, item) => sum+item.volumeFraction, 0);
const volumeSum = (result: ReturnType<typeof full>) => result.bins.reduce((sum, item) => sum+(item.volume ?? 0), 0);

describe("visible OHLCV volume profile", () => {
  it("defaults to 48 linear-price bins and a deterministic 70% POC-connected value area", () => {
    const result = full([bar(100, 148, 480)]);
    expect(result.status).toBe("AVAILABLE"); expect(result.effectiveBins).toBe(48);
    for (const bin of result.bins) expect(bin.volume).toBeCloseTo(10, 10);
    expect(result.poc).toBe(100.5); expect(result.pocIndex).toBe(0);
    expect(result.val).toBe(100); expect(result.vah).toBe(134);
    expect(result.actualValueArea).toBeCloseTo(34/48, 12);
    expect(result.totalVolume).toBe(480); expect(volumeSum(result)).toBeCloseTo(480, 10);
    expect(fractionSum(result)).toBeCloseTo(1, 12);
  });

  it("allocates partial candle overlaps proportionally, rather than assigning all volume to its close", () => {
    const result = full([bar(100, 124, 0), bar(100, 104, 80), bar(100.5, 101.5, 20)], 24);
    for (const [index, expected] of [30, 30, 20, 20].entries())
      expect(result.bins[index].volume).toBeCloseTo(expected, 12);
    expect(volumeSum(result)).toBeCloseTo(100, 12); expect(result.positiveVolumeCount).toBe(2);
  });

  it("grows value area contiguously from the POC, with lower-price ties first", () => {
    const result = full([bar(100, 124, 0), bar(110.5, 110.5, 100), bar(109.5, 109.5, 80), bar(111.5, 111.5, 80)], 24, .6);
    expect(result.poc).toBe(110.5);
    expect(result.val).toBe(109); expect(result.vah).toBe(111);
    expect(result.bins[9].inValueArea).toBe(true); expect(result.bins[11].inValueArea).toBe(false);
    expect(result.actualValueArea).toBeCloseTo(180/260, 12);
  });

  it("counts the actual visible logical slots, including partly visible edge candles only", () => {
    const rows = Array.from({ length: 8 }, (_, index) => bar(100+index, 100+index, 1, index));
    const middle = computeVolumeProfile(rows, { from: .8, to: 3.2 });
    expect([middle.firstIndex, middle.lastIndex, middle.visibleCount]).toEqual([1, 3, 3]);
    expect(middle.firstTime).toBe(rows[1].open_time); expect(middle.lastTime).toBe(rows[3].open_time);
    expect(computeVolumeProfile(rows, { from: .5, to: 2.5 }).includedCount).toBe(2);
    const clipped = computeVolumeProfile(rows, { from: -2, to: 3 });
    expect([clipped.visibleCount, clipped.requestedVisibleCount]).toEqual([4, 6]);
    expect(clipped.rangeCoverage).toBeCloseTo(2/3); expect(clipped.coverage).toBe(1);
    expect(clipped.qualityFlags).toContain("VISIBLE_RANGE_OUTSIDE_LOADED_DATA");
    expect(computeVolumeProfile(rows, { from: 20, to: 30 }).status).toBe("NO_VISIBLE_BARS");
    expect(computeVolumeProfile(rows, null).bins).toEqual([]);
    expect(computeVolumeProfile(rows, { from: 3, to: 2 }).bins).toEqual([]);
    expect(computeVolumeProfile(rows, { from: -Infinity, to: 2 }).bins).toEqual([]);
    expect(computeVolumeProfile(rows, { from: -1e30, to: 1e30 }).qualityFlags).toContain("LOGICAL_RANGE_EXCEEDS_SAFE_COUNT");
  });

  it("excludes explicit live candles and invalid OHLCV, and discloses legacy missing finality", () => {
    const rows = [bar(), { ...bar(), is_final: false }, { ...bar(), high: 90 }, { ...bar(), open: 130 },
      { ...bar(), close: 0 }, { ...bar(), volume: -1 }, { ...bar(), volume: "" },
      { ...bar(), volume: "1e-999" }, { ...bar(), volume: "Infinity" }, { ...bar(), is_final: undefined }];
    const result = full(rows);
    expect(result.includedCount).toBe(2); expect(result.excludedUnclosedCount).toBe(1);
    expect(result.excludedInvalidCount).toBe(7); expect(result.unknownFinalityCount).toBe(1);
    expect(result.coverage).toBe(.2); expect(result.totalVolume).toBe(480);
    expect(result.qualityFlags).toContain("LEGACY_FINALITY_NOT_PROVIDED");
    expect(full([{ ...bar(), is_final: false }]).status).toBe("NO_VALID_BARS");
  });

  it("does not invent a POC for zero volume or widen flat-price data", () => {
    const empty = full([bar(100, 100, 0)]);
    expect(empty.status).toBe("ZERO_VOLUME"); expect(empty.totalVolume).toBe(0);
    expect([empty.poc, empty.vah, empty.val]).toEqual([null, null, null]);
    expect(empty.bins.every(item => !item.isPoc && !item.inValueArea)).toBe(true);
    const flat = full([bar(100, 100, 5), bar(100, 100, 7)]);
    expect(flat.effectiveBins).toBe(1);
    expect(flat.bins[0]).toMatchObject({ low: 100, high: 100, price: 100, volume: 12, volumeFraction: 1 });
    expect([flat.poc, flat.vah, flat.val]).toEqual([100, 100, 100]);
  });

  it.each([24, 48, 96] as const)("conserves volume and agrees with a direct overlap oracle at %s bins", bins => {
    let seed = 19;
    const random = () => { seed = (Math.imul(seed, 1664525)+1013904223) >>> 0; return seed/2**32; };
    const rows = Array.from({ length: 320 }, (_, index) => {
      const low = 10+random()*100, high = low+random()*100;
      return bar(low, high, random()*1e6, index);
    });
    const result = full(rows, bins), total = rows.reduce((sum, row) => sum+Number(row.volume), 0);
    for (const bin of result.bins) {
      const expected = rows.reduce((sum, row) => sum+Number(row.volume)
        *Math.max(0, Math.min(bin.high, Number(row.high))-Math.max(bin.low, Number(row.low)))/(Number(row.high)-Number(row.low)), 0);
      expect(bin.volumeFraction).toBeCloseTo(expected/total, 12);
    }
    expect(result.totalVolume!/total).toBeCloseTo(1, 12);
    expect(volumeSum(result)/total).toBeCloseTo(1, 12);
    expect(fractionSum(result)).toBeCloseTo(1, 12);
  });

  it("keeps tiny prices meaningful and collapses bins only at representable price precision", () => {
    const tiny = full([bar(1e-20, 1.48e-20, 480)]);
    expect(tiny.effectiveBins).toBe(48); expect(tiny.poc!).toBeGreaterThan(0);
    expect(tiny.bins.every(item => Number.isFinite(item.price) && item.high > item.low)).toBe(true);
    expect(volumeSum(tiny)).toBeCloseTo(480, 9);
    const close = full([bar(1, 1+Number.EPSILON, 1)]);
    expect(close.effectiveBins).toBe(1);
    expect(close.qualityFlags).toContain("PRICE_BINS_COLLAPSED_AT_FLOAT_PRECISION");
    expect(close.bins[0].low).toBe(1); expect(close.bins[0].high).toBe(1+Number.EPSILON);
    const subnormal = full([bar(Number.MIN_VALUE, Number.MIN_VALUE*2, 1)]);
    expect(subnormal.poc!).toBeGreaterThan(0); expect(fractionSum(subnormal)).toBe(1);
  });

  it("retains normalized fractions when totals overflow, without Infinity or invented zeros", () => {
    const huge = full([bar(Number.MAX_VALUE/2, Number.MAX_VALUE, 1e308), bar(Number.MAX_VALUE/2, Number.MAX_VALUE, 1e308)]);
    expect(huge.status).toBe("AVAILABLE"); expect(huge.totalVolume).toBeNull();
    expect(huge.qualityFlags).toContain("TOTAL_VOLUME_EXCEEDS_NUMBER_RANGE");
    expect(huge.bins.every(item => item.volume === null && Number.isFinite(item.price) && Number.isFinite(item.volumeFraction))).toBe(true);
    expect(fractionSum(huge)).toBeCloseTo(1, 12);
    const subnormal = full([bar(100, 124, Number.MIN_VALUE)], 24);
    expect(subnormal.totalVolume).toBe(Number.MIN_VALUE);
    expect(subnormal.qualityFlags).toContain("BIN_VOLUME_BELOW_NUMBER_RANGE");
    expect(subnormal.bins.every(item => item.volume === null && item.volumeFraction > 0)).toBe(true);
    expect(fractionSum(subnormal)).toBeCloseTo(1, 12);
  });

  it("does not mutate inputs and uses bounded presentation settings", () => {
    const rows = Object.freeze([Object.freeze(bar())]);
    expect(full(rows).status).toBe("AVAILABLE");
    expect(resolveVolumeProfileOptions()).toEqual({ bins: 48, valueArea: .7, widthPercent: 25 });
    expect(resolveVolumeProfileOptions({ bins: 96, valueArea: .8, widthPercent: 4 })).toEqual({ bins: 96, valueArea: .8, widthPercent: 10 });
    expect(resolveVolumeProfileOptions({ valueArea: 70, widthPercent: 100 })).toEqual({ bins: 48, valueArea: .7, widthPercent: 40 });
  });
});
