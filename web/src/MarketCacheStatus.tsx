import { historyState, type MarketCacheStatus as Cache } from "./marketCache";

export function MarketCacheStatus({ cache, error, refreshing, onRefresh }: { cache?: Cache | null; error?: string; refreshing?: boolean; onRefresh: () => void }) {
  if (!cache) return null;
  const stale = cache.stale || !!error;
  return <section className={`market-cache-status ${stale ? "stale" : "fresh"}`} aria-label="行情缓存状态"><div><strong>{!cache.active ? "未持续订阅" : stale ? "行情延迟 · 显示缓存" : "后台持续同步"}</strong><span>{historyState[cache.history_status] ?? cache.history_status} · 最近缓存 {cache.cached_bars ?? 0} / {cache.target_bars} 根</span></div><div><small>{cache.last_live_at ? `行情更新 ${new Date(cache.last_live_at).toLocaleTimeString()}` : "等待首次同步"}{cache.missing_bars ? ` · 待补 ${cache.missing_bars} 根` : ""}</small><button disabled={refreshing} onClick={onRefresh}>{refreshing ? "刷新中…" : "刷新状态"}</button></div>{cache.history_status === "SOURCE_LIMIT" && <small>上游本次未返回更早数据，后台会定期复查；不会补造历史。</small>}</section>;
}
