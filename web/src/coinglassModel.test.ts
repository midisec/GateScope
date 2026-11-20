import { describe, expect, it } from "vitest";
import { coinglassBundleStatus, coinglassMetricSummary, coinglassNumber, coinglassPollSeconds, coinglassQueueAge,
  coinglassRefreshState, coinglassRetrySeconds, coinglassSeriesUsable, parseCoinglassScenario, usableCoinglassSeries, type CoinglassBundle, type CoinglassSeries } from "./coinglassModel";

const series: CoinglassSeries = { id: "oi", label: "OI 持仓量", unit: "USD", unit_status: "VERIFIED", population: "OPEN_INTEREST_MARK_VALUE", native_timeframe: "1m", status: "READY", quality_flags: [], points: [{ time: 60, close_time: 120, available_at: 121, value: 100 }] };
const bundle: CoinglassBundle = { status: "PARTIAL", instrument_id: "btc", timeframe: "1m", scope: null, match: { status: "EXACT" }, as_of: 120, fetched_at: 121, expires_at: 241, series: [series] };

describe("Coinglass chart units and scenario validation", () => {
  it("displays percentage-point fee values without multiplying by 100", () => {
    expect(coinglassNumber(.01, "PERCENT")).toBe("0.01%");
    expect(coinglassNumber(-.01, "PERCENT")).toBe("-0.01%");
    expect(coinglassNumber(0, "USD")).toBe("$0");
  });
  it("keeps missing and nonfinite values missing", () => {
    for (const value of [null, undefined, Infinity, NaN]) expect(coinglassNumber(value)).toBe("—");
  });
  it("quarantines unverified units and coarse data", () => {
    expect(coinglassSeriesUsable(series, "1m")).toBe(true);
    expect(coinglassSeriesUsable({ ...series, unit_status: "UNKNOWN" })).toBe(false);
    expect(coinglassSeriesUsable({ ...series, unit: "COIN" })).toBe(false);
    expect(coinglassSeriesUsable(series, "5m")).toBe(false);
  });
  it("only offers exact-contract series", () => {
    expect(usableCoinglassSeries(bundle)).toHaveLength(1);
    expect(usableCoinglassSeries({ ...bundle, match: { status: "AMBIGUOUS" } })).toEqual([]);
    expect(usableCoinglassSeries(undefined)).toEqual([]);
  });
  it("marks cached data stale without inventing an update", () => {
    expect(coinglassBundleStatus(bundle, 240)).toBe("PARTIAL");
    expect(coinglassBundleStatus(bundle, 241)).toBe("STALE");
    expect(coinglassBundleStatus({ ...bundle, status: "RATE_LIMITED" }, 500)).toBe("RATE_LIMITED");
  });
  it("counts down an absolute retry timestamp without restarting a cached relative hint", () => {
    expect(coinglassRetrySeconds({ retry_at: 150 }, 100)).toBe(50);
    expect(coinglassRetrySeconds({ retry_at: 150 }, 149.1)).toBe(1);
    expect(coinglassRetrySeconds({ retry_at: 150 }, 150)).toBe(0);
    expect(coinglassRetrySeconds({ retry_at: Infinity }, 100)).toBe(0);
    expect(coinglassRetrySeconds(undefined, 100)).toBe(0);
    expect(coinglassBundleStatus({ ...bundle, status: "RATE_LIMITED", retry_at: 150, refresh_requested: true }, 151)).toBe("QUEUED");
    expect(coinglassBundleStatus({ ...bundle, status: "RATE_LIMITED", retry_at: 150 }, 151)).toBe("DEFERRED");
    expect(coinglassBundleStatus({ ...bundle, status: "RATE_LIMITED", retry_at: 150, cache_retained: true }, 151)).toBe("STALE");
  });
  it("keeps background refresh state separate from data readiness", () => {
    for (const state of ["QUEUED", "COLLECTING", "COOLDOWN", "SCHEDULED", "IDLE", "NOT_REQUESTED"] as const) {
      const pending = { ...bundle, refresh_state: state };
      expect(coinglassRefreshState(pending, 200)).toBe(state);
      expect(coinglassBundleStatus(pending, 200)).toBe("PARTIAL");
      expect(usableCoinglassSeries(pending)).toHaveLength(1);
      expect(coinglassMetricSummary(pending, 200)).toEqual({ ready: 1, historical: 0, total: 5 });
    }
    expect(coinglassRefreshState({ ...bundle, status: "PENDING" }, 200)).toBe("QUEUED");
    expect(coinglassRefreshState({ ...bundle, refresh_state: "COLLECTING", retry_at: 300 }, 200)).toBe("COOLDOWN");
  });
  it("counts old data only as historical reference and unknown units never as ready", () => {
    expect(coinglassMetricSummary({ ...bundle, refresh_state: "QUEUED" }, 300)).toEqual({ ready: 0, historical: 1, total: 5 });
    expect(coinglassMetricSummary({ ...bundle, series: [{ ...series, status: "STALE" }] }, 200)).toEqual({ ready: 0, historical: 1, total: 5 });
    expect(coinglassMetricSummary({ ...bundle, series: [{ ...series, unit_status: "UNKNOWN" }] }, 200)).toEqual({ ready: 0, historical: 0, total: 5 });
    expect(coinglassMetricSummary({ ...bundle, match: { status: "MISMATCH" } }, 200).ready).toBe(0);
  });
  it("uses bounded server polling hints and never shortens actual provider cooldown", () => {
    expect(coinglassPollSeconds({ refresh_state: "COLLECTING", next_poll_after_seconds: 3 }, 200)).toBe(3);
    expect(coinglassPollSeconds({ refresh_state: "QUEUED", next_poll_after_seconds: 0.1 }, 200)).toBe(2);
    expect(coinglassPollSeconds({ refresh_state: "QUEUED", next_poll_after_seconds: 120 }, 200)).toBe(30);
    expect(coinglassPollSeconds({ refresh_state: "QUEUED", next_poll_after_seconds: NaN }, 200)).toBe(5);
    expect(coinglassPollSeconds({ refresh_state: "IDLE", next_poll_after_seconds: 0 }, 200)).toBe(0);
    expect(coinglassPollSeconds({ refresh_state: "NOT_REQUESTED", next_poll_after_seconds: 0 }, 200)).toBe(0);
    expect(coinglassPollSeconds({ refresh_state: "IDLE" }, 200)).toBe(30);
    expect(coinglassPollSeconds({ refresh_state: "COLLECTING", next_poll_after_seconds: 2, retry_at: 900 }, 200)).toBe(700);
  });
  it("uses real queue timestamps or reported ages, never cache fetch time as a queue start", () => {
    expect(coinglassQueueAge({ queued_at: 100, queue_age_seconds: 40 }, 200)).toBe(100);
    expect(coinglassQueueAge({ queue_age_seconds: 40 }, 200)).toBe(40);
    expect(coinglassQueueAge({ fetched_at: 100 }, 200)).toBeNull();
    expect(coinglassQueueAge({ queued_at: 300, queue_age_seconds: -5 }, 200)).toBeNull();
  });
  it.each([["", "1", "2"], ["NaN", "1", "2"], ["0", "1", "2"], ["1", "3", "2"], ["1", "-1", "2"], ["1", "1", "Infinity"]])("rejects invalid observation prices %s %s %s", (entry, lower, upper) => {
    expect(parseCoinglassScenario(entry, lower, upper).error).toBeTruthy();
  });
  it("preserves exact user observation prices and does not emit an order or a probability", () => {
    expect(parseCoinglassScenario("100", "110", "120")).toEqual({ scenario: { entry: 100, target_lower: 110, target_upper: 120 } });
  });
});
