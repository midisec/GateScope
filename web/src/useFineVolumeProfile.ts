import { useEffect, useRef, useState } from "react";
import type { Bar } from "./types";
import type { FineVolumeProfileResponse, VolumeProfileRange, VolumeProfileRows } from "./volumeProfile";
import { FineVolumeProfileCache, fineProfileKey, fineProfilePath, fineRetryAt, fineVolumeProfileWindow,
  validateFineProfile, type FineProfileRequest } from "./fineVolumeProfileModel";

type Phase = "WAITING" | "LOADING" | "READY" | "ERROR" | "COOLDOWN" | "PAUSED";
type State = { key: string; phase: Phase; bundle?: FineVolumeProfileResponse; error?: string; retryAt?: number; validUntil?: number };
export type FineVolumeProfileView = Omit<State, "key" | "validUntil"> & { key: string; reload: () => void };
export type FineVolumeProfileInput = { instrumentId?: string; timeframe: string; bars: readonly Bar[];
  range: VolumeProfileRange | null; enabled: boolean; rows: VolumeProfileRows; valueAreaPercent: number; scope?: string };
const message = (status: number) => status === 404 ? "细周期分布接口尚不可用" : status === 401 || status === 403
  ? "请重新登录或检查数据读取权限" : [400, 413, 422].includes(status) ? "区间或参数超出限制，请缩小图表范围后重试"
    : status === 408 ? "细周期计算超时，请重试" : "细周期数据暂不可用，请重试";

/** Read-only detail requests are scoped, debounced and cancelled; renderer never fetches. */
export function useFineVolumeProfile(input: FineVolumeProfileInput): FineVolumeProfileView {
  const visibleWindow = fineVolumeProfileWindow(input.bars, input.range, input.timeframe);
  const request: FineProfileRequest | undefined = input.enabled && input.instrumentId && visibleWindow
    ? { instrument_id: input.instrumentId, timeframe: input.timeframe, ...visibleWindow, rows: input.rows, value_area: input.valueAreaPercent } : undefined;
  const key = request ? fineProfileKey(request, input.scope) : "";
  const cache = useRef(new FineVolumeProfileCache());
  const cooldown = useRef(0), previousScope = useRef(input.scope);
  const [state, setState] = useState<State>({ key: "", phase: "WAITING" });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (previousScope.current !== input.scope) { cache.current.clear(); cooldown.current = 0; previousScope.current = input.scope; }
    if (!request) { setState({ key: "", phase: "WAITING" }); return; }
    let disposed = false, pending = false, autoRetries = 0;
    let controller: AbortController | undefined, timeout: ReturnType<typeof setTimeout> | undefined;
    let scheduled: ReturnType<typeof setTimeout> | undefined;
    const clearSchedule = () => { clearTimeout(scheduled); scheduled = undefined; };
    function waitForCooldown() {
      const remaining = cooldown.current - Date.now();
      if (remaining <= 0) return false;
      setState({ key, phase: "COOLDOWN", retryAt: cooldown.current, error: "请求过于频繁，请等待后重试" });
      // One bounded automatic retry. Long Retry-After values need a deliberate retry.
      if (remaining <= 60000) scheduled = setTimeout(() => {
        if (disposed) return;
        if (autoRetries <= 1) void load(); else setState({ key, phase: "ERROR", error: "限流等待结束，可手动重试" });
      }, remaining);
      return true;
    }
    function schedule(delay = 350) {
      clearSchedule();
      if (disposed) return;
      if (document.hidden) { setState({ key, phase: "PAUSED" }); return; }
      if (waitForCooldown()) return;
      setState({ key, phase: "LOADING" });
      scheduled = setTimeout(() => { void load(); }, delay);
    }
    function accept(bundle: FineVolumeProfileResponse) {
      const validUntil = Math.min(Date.now() + 30000, (bundle.generated_at + 30) * 1000, bundle.expires_at * 1000);
      setState({ key, phase: "READY", bundle, validUntil });
      clearSchedule(); scheduled = setTimeout(() => schedule(0), Math.max(1, validUntil - Date.now()));
    }
    async function load() {
      if (disposed || pending || document.hidden) return;
      clearSchedule(); if (waitForCooldown()) return;
      pending = true; controller = new AbortController(); const current = controller; let timedOut = false;
      setState({ key, phase: "LOADING" });
      const currentTimeout = setTimeout(() => { timedOut = true; current.abort(); }, 30000); timeout = currentTimeout;
      try {
        const response = await fetch(fineProfilePath(request!), { method: "GET", credentials: "same-origin", signal: current.signal });
        if (disposed || current.signal.aborted) return;
        if (response.status === 429) {
          cooldown.current = fineRetryAt(response.headers.get("Retry-After")); autoRetries++;
          waitForCooldown(); return;
        }
        if (!response.ok) {
          if (response.status === 401) globalThis.window.dispatchEvent(new Event("gatescope:session-expired"));
          throw new Error(message(response.status));
        }
        const payload: unknown = await response.json();
        if (disposed || current.signal.aborted) return;
        const bundle = validateFineProfile(payload, request!);
        cache.current.put(key, bundle); accept(bundle);
      } catch (error) {
        if (disposed || current.signal.aborted && !timedOut) return;
        setState({ key, phase: "ERROR", error: timedOut ? message(408) : error instanceof Error && !(error instanceof TypeError) && !(error instanceof SyntaxError) ? error.message : message(0) });
      } finally { clearTimeout(currentTimeout); if (controller === current) pending = false; }
    }
    function visibility() {
      if (document.hidden) {
        clearSchedule(); controller?.abort(); controller = undefined; pending = false; clearTimeout(timeout); setState({ key, phase: "PAUSED" });
      } else schedule();
    }
    const saved = cache.current.get(key);
    if (document.hidden) setState({ key, phase: "PAUSED" });
    else if (saved) accept(saved); else schedule();
    document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; clearSchedule(); clearTimeout(timeout); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
  }, [key, revision]);
  const current = input.enabled && key !== "" && state.key === key;
  const expired = current && state.validUntil !== undefined && state.validUntil <= Date.now();
  const view: State = current && !expired ? state : { key, phase: key ? "LOADING" : "WAITING" };
  return { ...view, reload: () => {
    if (!request || cooldown.current > Date.now()) return;
    cache.current.clear(); setRevision(value => value + 1);
  } };
}
