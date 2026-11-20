import { describe, expect, it } from "vitest";
import { FineVolumeProfileCache, fineProfileKey, fineProfilePath, fineRetryAt, fineVolumeProfileWindow, validateFineProfile, fineCoverageLabel, fineProfileIsPartial } from "./fineVolumeProfileModel";
import { FINE_START, fineChartBars, fineFixture, fineRequest } from "./fineVolumeProfile.testFixtures";

describe("fine VPVR visible UTC window", () => {
  const now = FINE_START + 9000;
  it("selects actual visible closed boundaries, not logical index times the timeframe", () => {
    const bars = fineChartBars(900, 6); bars.splice(2, 2);
    expect(fineVolumeProfileWindow(bars, { from: 1, to: 2 }, "15m", now)).toEqual({ from_time: FINE_START + 900, to_time: FINE_START + 4500 });
    expect(fineVolumeProfileWindow(bars, { from: .5, to: 1.5 }, "15m", now)).toEqual({ from_time: FINE_START + 900, to_time: FINE_START + 1800 });
  });
  it("excludes unfinished and unverifiable finality without treating all history as visible", () => {
    const bars = fineChartBars(900, 4); bars[1].is_final = false; bars[2].is_final = undefined;
    expect(fineVolumeProfileWindow(bars, { from: 0, to: 2 }, "15m", now)).toEqual({ from_time: FINE_START, to_time: FINE_START + 900 });
    expect(fineVolumeProfileWindow(bars, { from: 1, to: 2 }, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow(bars, null, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow(bars, { from: 10, to: 20 }, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow(bars, { from: 2, to: 1 }, "15m", now)).toBeNull();
  });
  it("rejects future, invalid-duration and unavailable chart candles", () => {
    const bars = fineChartBars();
    expect(fineVolumeProfileWindow(bars, { from: 0, to: 1 }, "15m", FINE_START + 1000)).toEqual({ from_time: FINE_START, to_time: FINE_START + 900 });
    expect(fineVolumeProfileWindow([{ ...bars[0], close_time: bars[1].close_time }], { from: 0, to: 0 }, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow([{ ...bars[0], available_at: "invalid" }], { from: 0, to: 0 }, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow(bars, { from: -Infinity, to: 1 }, "15m", now)).toBeNull();
    expect(fineVolumeProfileWindow(bars, { from: -1e30, to: 1e30 }, "15m", now)).toBeNull();
  });
});

describe("fine VPVR response binding and cache", () => {
  const now = FINE_START + 1802;
  it("accepts native detail geometry and labels time coverage independently of profile coverage", () => {
    const response = fineFixture(fineRequest, { used_bars: 15 });
    expect(response.profile.coverage).toBe(1); expect(response.coverage).toBe(.5);
    expect(validateFineProfile(response, fineRequest, now)).toBe(response);
    expect(validateFineProfile(fineFixture(fineRequest, { status: "PARTIAL" }), fineRequest, now).coverage).toBe(1);
  });
  it.each([
    { instrument_id: "another" }, { timeframe: "1m" }, { from_time: FINE_START + 1 }, { to_time: FINE_START + 1801 },
    { source_step_seconds: 300 }, { coverage: .8 }, { expected_bars: 50001 }, { generated_at: FINE_START + 1900 },
    { generated_at: FINE_START + 1700 }, { expires_at: FINE_START + 1801 },
  ])("rejects mismatched requests, clocks and source metadata: %j", changes => {
    expect(() => validateFineProfile(fineFixture(fineRequest, changes), fineRequest, now)).toThrow();
  });
  it("rejects wrong bins/value area and non-finite geometry", () => {
    const response = fineFixture();
    for (const profile of [{ ...response.profile, requestedBins: 24 as const }, { ...response.profile, valueArea: .8 },
      { ...response.profile, includedCount: 29 }, { ...response.profile, bins: response.profile.bins.map((bin, index) => index ? bin : { ...bin, volumeFraction: Infinity }) }])
      expect(() => validateFineProfile({ ...response, profile }, fineRequest, now)).toThrow();
  });
  it("binds identity and options, not presentation width", () => {
    const key = fineProfileKey(fineRequest, "owner");
    for (const request of [{ ...fineRequest, rows: 24 as const }, { ...fineRequest, value_area: 85 }, { ...fineRequest, instrument_id: "other" }, { ...fineRequest, to_time: FINE_START + 900 }])
      expect(fineProfileKey(request, "owner")).not.toBe(key);
    expect(fineProfileKey(fineRequest, "other-owner")).not.toBe(key);
    expect(fineProfilePath(fineRequest)).toContain("/api/v2/market/volume-profile?instrument_id=");
    expect(fineProfilePath(fineRequest)).not.toContain("width");
  });
  it("evicts by LRU and preserves absolute expiry instead of extending it on reads", () => {
    const cache = new FineVolumeProfileCache(2), response = fineFixture(), ms = now * 1000;
    cache.put("a", response, ms); cache.put("b", response, ms); expect(cache.get("a", ms + 1000)).toBe(response);
    cache.put("c", response, ms + 2000); expect(cache.get("b", ms + 2000)).toBeUndefined();
    expect(cache.get("a", ms + 29999)).toBe(response); expect(cache.get("a", ms + 30000)).toBeUndefined();
    cache.clear(); expect(cache.get("c", ms + 2500)).toBeUndefined();
  });
  it("parses Retry-After without shortening long provider cooldowns", () => {
    const ms = now * 1000;
    expect(fineRetryAt("2", ms)).toBe(ms + 2000);
    expect(fineRetryAt("7200", ms)).toBe(ms + 7200000);
    expect(fineRetryAt(new Date(ms + 5000).toUTCString(), ms)).toBe(ms + 5000);
    expect(fineRetryAt(null, ms)).toBe(ms + 30000);
  });
  it("never rounds incomplete coverage up to 100 percent, even one missing slot", () => {
    expect(fineCoverageLabel(17279 / 17280)).toBe("99.994%");
    expect(fineCoverageLabel(49999 / 50000)).toBe("99.998%");
    expect(fineCoverageLabel(1)).toBe("100%"); expect(fineCoverageLabel(1 - 1e-9)).toBe("<100%");
    expect(fineCoverageLabel(0)).toBe("0%");
  });
  it("recognizes partial source boundaries even for zero-volume full counted slots", () => {
    expect(fineProfileIsPartial(fineFixture(fineRequest, { status: "ZERO_VOLUME", quality_flags: ["PARTIAL_SOURCE_BOUNDARIES"] }))).toBe(true);
    expect(fineProfileIsPartial(fineFixture(fineRequest, { quality_flags: ["FUTURE_RANGE_NOT_CLOSED"] }))).toBe(true);
  });
});
