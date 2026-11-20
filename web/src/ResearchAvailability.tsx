import { useEffect, useState } from "react";
import { api } from "./api";
import type { Spec } from "./researchModel";
import { PageState } from "./ui";
import { historyState, type MarketCacheStatus } from "./marketCache";

export function ReplayMode({ value, onChange }: { value: Spec["availability_mode"]; onChange: (value: Spec["availability_mode"]) => void }) {
  return <div className="research-wide replay-mode"><label>数据回放模式<select aria-label="数据回放模式" value={value ?? "AS_RECORDED"} onChange={e => onChange(e.target.value as Spec["availability_mode"])}><option value="HISTORICAL_CLOSE">历史收盘重建 · 研究历史策略</option><option value="AS_RECORDED">严格接收时间 · 检查真实数据延迟</option></select></label><small>{value === "HISTORICAL_CLOSE" ? "假设历史 K 线收盘时可用，按当前保存版本重建；不代表当时真实可成交，不用于实盘资格认证。" : "按系统记录的数据可用时间回放；事后补录的 K 线可能让入场信号过期。"}</small></div>;
}
type Coverage = { available_bars: number; delayed_bars: number; missing_bars?: number; internal_missing_bars?: number; from_time?: string; to_time?: string; cache?: MarketCacheStatus };
export function ResearchCoverage({ instrument, timeframe, limit, start, end }: { instrument: string; timeframe: string; limit: number; start?: string; end?: string }) {
  const [coverage, setCoverage] = useState<Coverage>(), [error, setError] = useState(""), [retry, setRetry] = useState(0);
  useEffect(() => {
    setCoverage(undefined); setError(""); if (!instrument || limit < 300 || limit > 550000 || !Number.isInteger(limit)) return;
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending || document.hidden || controller.signal.aborted) return;
      pending = true;
      try {
        const query = new URLSearchParams({ instrument_id: instrument, timeframe, limit: String(limit) });
        if (start) query.set("from_time", new Date(start).toISOString());
        if (end) query.set("to_time", new Date(end).toISOString());
        const r = await api<Coverage>(`/api/v2/research/coverage?${query}`, { signal: controller.signal });
        if (!controller.signal.aborted) { setCoverage(r); setError(""); }
      } catch(e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "覆盖查询失败"); }
      finally { pending = false; }
    }
    void load(); const timer = setInterval(() => void load(), 10000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [instrument, timeframe, limit, start, end, retry]);
  if (!instrument) return null;
  return <div className="research-wide research-coverage"><PageState error={error} onRetry={() => setRetry(n => n + 1)} />{coverage ? <><strong>所选区间可用 {coverage.available_bars.toLocaleString()} 根 · 本次上限 {limit.toLocaleString()} 根</strong>{coverage.from_time && coverage.to_time && <span>{new Date(coverage.from_time).toLocaleString()} — {new Date(coverage.to_time).toLocaleString()}</span>}{coverage.cache && <span>{historyState[coverage.cache.history_status] ?? "同步中"} · 后台缓存 {coverage.cache.cached_bars ?? 0} / {coverage.cache.target_bars} 根</span>}{!!coverage.missing_bars && <span>请求范围缺少 {coverage.missing_bars.toLocaleString()} 根{coverage.internal_missing_bars ? `，其中区间内部缺口 ${coverage.internal_missing_bars} 根；需补齐或调整日期` : "；结果会标明实际覆盖，不补造数据"}</span>}<small>所选区间有 {coverage.delayed_bars} 根延迟超过一个周期；K 线数量不等于成交笔数。</small></> : !error && <span>查询历史覆盖…</span>}</div>;
}
