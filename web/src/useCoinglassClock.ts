import { useEffect, useState } from "react";

/** UI clock only; ticking never starts a provider request. */
export function useCoinglassClock(retryAt?: number | null, active = false) {
  // State schedules idle/countdown re-renders; it is not the observation clock.
  // A newly fetched bundle can arrive between ticks. Comparing it against the
  // previous tick would incorrectly make its real fetched_at look future-dated.
  const [, setNow] = useState(() => Date.now() / 1000);
  const now = Date.now() / 1000;
  const cooling = retryAt != null && Number.isFinite(retryAt) && retryAt > now;
  useEffect(() => {
    setNow(Date.now() / 1000);
    const timer = window.setInterval(() => setNow(Date.now() / 1000), cooling || active ? 1000 : 30000);
    return () => window.clearInterval(timer);
  }, [retryAt, cooling, active]);
  return now;
}
