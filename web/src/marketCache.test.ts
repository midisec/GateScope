import { describe, expect, it } from "vitest";
import { MarketPageCache, chartBars, mergeMarketPages, stableMarketItems, fullMarketRefreshNeeded, type MarketPage } from "./marketCache";
import type { Bar } from "./types";

describe("bounded public market cache", () => {
  it("uses incremental polling even for a shorter-than-1500 daily history", () => {
    expect(fullMarketRefreshNeeded(true, 1000, 6000)).toBe(false);
    expect(fullMarketRefreshNeeded(true, 1000, 61000)).toBe(true);
    expect(fullMarketRefreshNeeded(false, 1000, 6000)).toBe(true);
    expect(fullMarketRefreshNeeded(true, 1000, 6000, true)).toBe(true);
  });
  it("retains unchanged candle identity but applies revisions and quality corrections", () => {
    const bars: Bar[] = [{ open_time: "2026-09-09T12:00:00Z", open:"99", high:"101", low:"98", close: "100", volume:"10", revision: 1, quality_flags: [] }];
    expect(stableMarketItems(bars, structuredClone(bars))).toBe(bars);
    expect(stableMarketItems(bars, [{ ...bars[0], revision: 2 }])).not.toBe(bars);
    expect(stableMarketItems(bars, [{ ...bars[0], quality_flags: ["CORRECTED"] }])).not.toBe(bars);
    expect(stableMarketItems(bars, [])).not.toBe(bars);
  });
  it("isolates symbol/period and evicts the least recently used entry", () => {
    const cache = new MarketPageCache(2), page = { items: [] } as unknown as MarketPage;
    cache.set("btc:1h", page); cache.set("eth:1h", page); cache.get("btc:1h"); cache.set("btc:4h", page);
    expect(cache.get("eth:1h")).toBeUndefined(); expect(cache.get("btc:1h")).toBe(page);
  });
  it("appends forming candles only for display, never mutates frozen final input", () => {
    const final = [{ open_time: "2026-09-09T12:00:00Z", close: "100" }] as Bar[];
    const live = { open_time: "2026-09-09T12:01:00Z", close: "101", is_final: false } as Bar;
    expect(chartBars(final, live)).toHaveLength(2); expect(final).toHaveLength(1);
    expect(chartBars(final, { ...live, open_time: final[0].open_time })).toBe(final);
    expect(chartBars(final, { ...live, is_final: true })).toBe(final);
  });
  it("merges bounded increments without rolling a candle back to an older revision", () => {
    const previous = { items: Array.from({ length: 1500 }, (_, i) => ({ open_time: new Date(i * 60000).toISOString(), revision: 2, close: "100" })) } as MarketPage;
    const next = { items: [{ ...previous.items.at(-1)!, revision: 1, close: "99" }, { open_time: new Date(1500 * 60000).toISOString(), revision: 1, close: "101" }] } as MarketPage;
    const merged = mergeMarketPages(previous, next);
    expect(merged.items).toHaveLength(1500);
    expect(merged.items.at(-2)!.close).toBe("100"); expect(merged.items.at(-1)!.close).toBe("101");
    expect(previous.items[0].open_time).toBe(new Date(0).toISOString());
  });
});
