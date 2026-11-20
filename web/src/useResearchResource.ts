import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export function useResearchResource<T>(url: string | null, refresh = 0) {
  const [data, setData] = useState<T>(), [error, setError] = useState(""), [loading, setLoading] = useState(!!url);
  const [retry, setRetry] = useState(0);
  const previousUrl = useRef<string | null>(null);
  useEffect(() => {
    if (previousUrl.current !== url) setData(undefined);
    previousUrl.current = url; setError(""); setLoading(!!url);
    if (!url) return;
    const controller = new AbortController();
    void api<T>(url, { signal: controller.signal }).then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "请求失败，请重试"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [url, refresh, retry]);
  const sameRequest = previousUrl.current === url;
  return { data: sameRequest ? data : undefined, error: sameRequest ? error : "", loading: sameRequest ? loading : !!url, retry: () => setRetry(n => n + 1) };
}
