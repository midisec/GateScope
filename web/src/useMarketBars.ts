import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { MarketPageCache, mergeMarketPages, timeframeSeconds, stableMarketItems, fullMarketRefreshNeeded, type MarketPage } from "./marketCache";
import { readMarketPreview, saveMarketPreview } from "./marketPreview";

export function useMarketBars(instrument: string | undefined, timeframe: string, scope="public") {
  const cache = useRef(new MarketPageCache());
  const [result, setResult] = useState<{ key: string; page?: MarketPage }>({ key: "" });
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [refreshing, setRefreshing] = useState(false);
  const [lastChecked, setLastChecked] = useState<number>();
  const refresh = useRef<() => Promise<void>>(async () => {});
  const reload = useCallback(() => refresh.current(), []);
  const key = `${scope}:${instrument ?? ""}:${timeframe}`;
  useEffect(() => {
    if (!instrument) { setResult({ key }); setLoading(false); return; }
    let disposed = false, pending = false, timer: ReturnType<typeof setTimeout> | undefined, failures = 0;
    let controller: AbortController | undefined;
    const saved = cache.current.get(key) ?? readMarketPreview(scope,key);
    let persistedAt=0;
    let current = saved, lastFullAt = 0;
    setResult({ key, page: saved }); setLoading(!saved); setError(""); setLastChecked(undefined);
    async function load(forceFull = false) {
      if (disposed || pending || document.hidden) return;
      clearTimeout(timer); pending = true; controller = new AbortController(); setRefreshing(true);
      try {
        let full = fullMarketRefreshNeeded(!!current, lastFullAt, Date.now(), forceFull);
        const fetchPage = (limit: number) => api<MarketPage>(`/api/v2/bars?instrument_id=${encodeURIComponent(instrument!)}&timeframe=${timeframe}&limit=${limit}&latest=true`, { signal: controller!.signal });
        let page = await fetchPage(full ? 1500 : 8);
        // An outage longer than the incremental window requires a full repair before merging.
        if (!full && current?.items.length && page.items.length && Date.parse(page.items[0].open_time) > Date.parse(current.items.at(-1)!.open_time) + timeframeSeconds[timeframe] * 1000) {
          page = await fetchPage(1500); full = true;
        }
        if (disposed) return;
        if (full) lastFullAt = Date.now();
        else if (current) page = mergeMarketPages(current, page);
        page = { ...page, items: stableMarketItems(current?.items, page.items) };
        current = page;
        cache.current.set(key, page); setResult({ key, page }); setError(""); failures = 0; setLastChecked(Date.now());
        if(Date.now()-persistedAt>=30000){saveMarketPreview(scope,key,page);persistedAt=Date.now()}
      } catch (e) {
        if (!disposed) { failures += 1; setError(`${e instanceof Error ? e.message : "行情读取失败"} · 已保留缓存，恢复连接后自动更新`); }
      } finally {
        pending = false;
        if (!disposed) { setLoading(false); setRefreshing(false); timer = setTimeout(() => void load(), Math.min(30000, 5000 * 2 ** Math.min(failures, 3))); }
      }
    }
    refresh.current = () => load(true);
    const resume = () => { if (!document.hidden) void load(true); };
    void load(); window.addEventListener("online", resume); window.addEventListener("focus", resume); document.addEventListener("visibilitychange", resume);
    return () => { disposed = true; controller?.abort(); clearTimeout(timer); window.removeEventListener("online", resume); window.removeEventListener("focus", resume); document.removeEventListener("visibilitychange", resume); };
  }, [key, instrument, timeframe,scope]);
  // Never render the previous symbol under the new symbol's heading, even before effects run.
  return { page: result.key === key ? result.page : undefined, error, loading, refreshing, lastChecked, reload };
}
