import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { coinglassPollSeconds, coinglassRetrySeconds, type CoinglassBundle } from "./coinglassModel";

type State = { key: string; bundle?: CoinglassBundle; error: string; loading: boolean };
export function useCoinglassChart(instrumentId: string | undefined, timeframe: string, enabled: boolean, userKey: string) {
  const key = `${userKey}:${instrumentId}:${timeframe}`;
  const [state, setState] = useState<State>({ key, error: "", loading: false });
  const latest = useRef<{ key: string; bundle: CoinglassBundle } | undefined>(undefined);
  const active = useRef<{ key: string; load: () => void } | undefined>(undefined);
  useEffect(() => {
    if (!enabled || !instrumentId) return;
    const abort = new AbortController(); let busy = false, timer: number | undefined;
    const currentBundle = () => latest.current?.key === key ? latest.current.bundle : undefined;
    function schedule(passive = false) {
      if (abort.signal.aborted) return;
      window.clearTimeout(timer);
      const suggested = coinglassPollSeconds(currentBundle());
      if (suggested === 0) return; // Explicit idle: manual/visibility/online checks still work.
      const interval = Math.max(passive ? 30 : 0, suggested);
      timer = window.setTimeout(() => { void load(); }, document.hidden ? 30000 : Math.min(interval * 1000, 86400000));
    }
    async function load() {
      if (abort.signal.aborted || busy) return;
      if (document.hidden || coinglassRetrySeconds(currentBundle()) > 0) { schedule(); return; }
      busy = true; window.clearTimeout(timer);
      let failed = false;
      setState(previous => ({ key, bundle: previous.key === key ? previous.bundle : undefined, error: "", loading: true }));
      try {
        const bundle = await api<CoinglassBundle>(`/api/v2/coinglass/chart?instrument_id=${encodeURIComponent(instrumentId!)}&timeframe=${encodeURIComponent(timeframe)}`, { signal: abort.signal });
        if (abort.signal.aborted) return;
        if (bundle.instrument_id !== instrumentId || bundle.timeframe !== timeframe) throw new Error("数据与当前币种或周期不一致，请重试");
        // Retained data must come from the server's scoped cache contract;
        // never merge points or invent collection times in the browser.
        latest.current = { key, bundle };
        setState({ key, bundle, error: "", loading: false });
      } catch (error) {
        failed = true;
        if (!abort.signal.aborted) setState(previous => ({ key, bundle: previous.key === key ? previous.bundle : undefined,
          error: error instanceof Error ? error.message : "Coinglass 暂未更新", loading: false }));
      } finally { busy = false; schedule(failed); }
    }
    active.current = { key, load: () => { void load(); } };
    void load(); document.addEventListener("visibilitychange", load); window.addEventListener("online", load);
    return () => {
      abort.abort(); window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", load); window.removeEventListener("online", load);
      if (active.current?.key === key) active.current = undefined;
    };
  }, [key, enabled]);
  const current = state.key === key && enabled;
  return { bundle: current ? state.bundle : undefined, error: current ? state.error : "", loading: enabled && !!instrumentId && (!current || state.loading),
    reload: () => { if (enabled && active.current?.key === key) active.current.load(); } };
}
