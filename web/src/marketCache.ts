import type { Bar, Page } from "./types";

export type MarketCacheStatus = {
  active: boolean; target_bars: number; cached_bars?: number; missing_bars?: number;
  history_status: string; history_error_code?: string | null; live_error_code?: string | null;
  last_live_at?: string | null; last_history_at?: string | null; next_history_at?: string;
  stale: boolean; server_time?: string; live_bar?: Bar | null;
  period_days?:number;coverage_ratio?:number;target_from?:string;target_to?:string;
};
export type MarketPage = Page<Bar> & { quality_flags: string[]; data_watermark: string | null; cache?: MarketCacheStatus | null };
export type WatchQuote = { instrument_id: string; timeframe: string; price: string | null; observed_at: string | null; stale: boolean; history_status: string; cached_bars: number; target_bars: number };
export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"];
export const timeframeSeconds: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400 };
export const historyState: Record<string, string> = { PENDING: "等待补齐", BACKFILLING: "目标历史补齐中", READY: "目标历史已就绪", SOURCE_LIMIT: "上游部分历史缺失", GAP: "正在修复缺口", RETRYING: "稍后自动重试", NOT_SUBSCRIBED: "加入自选后持续采集" };

// Public market data only. Bounded per-workspace memory; server PostgreSQL is the durable cache.
export class MarketPageCache {
  private entries = new Map<string, MarketPage>();
  constructor(private maximum = 8) {}
  get(key: string) { const value = this.entries.get(key); if (value) { this.entries.delete(key); this.entries.set(key, value); } return value; }
  set(key: string, value: MarketPage) { this.entries.delete(key); this.entries.set(key, value); while (this.entries.size > this.maximum) this.entries.delete(this.entries.keys().next().value!); }
}

export function chartBars(final: Bar[], live?: Bar | null): Bar[] {
  if (!live || live.is_final !== false || (final.length && Date.parse(live.open_time) <= Date.parse(final.at(-1)!.open_time))) return final;
  return [...final, live];
}

// Preserve array identity on unchanged polls; cache metadata can still advance.
export function stableMarketItems(previous: Bar[] | undefined, next: Bar[]): Bar[] {
  return previous?.length === next.length && previous.every((bar, index) => JSON.stringify(bar) === JSON.stringify(next[index])) ? previous : next;
}

export function fullMarketRefreshNeeded(hasPage: boolean, lastFullAt: number, now: number, force = false): boolean {
  // A daily / newly listed market can legitimately have fewer than 1500 bars.
  return force || !hasPage || now - lastFullAt >= 60000;
}

export function mergeMarketPages(previous: MarketPage, next: MarketPage): MarketPage {
  const merged = new Map(previous.items.map(bar => [bar.open_time, bar]));
  for (const bar of next.items) {
    const old = merged.get(bar.open_time);
    if (!old || (bar.revision ?? 1) >= (old.revision ?? 1)) merged.set(bar.open_time, bar);
  }
  const items = [...merged.values()].sort((a, b) => Date.parse(a.open_time) - Date.parse(b.open_time)).slice(-1500);
  return { ...next, items, quality_flags: [...new Set(items.flatMap(bar => bar.quality_flags ?? []))].sort() };
}
