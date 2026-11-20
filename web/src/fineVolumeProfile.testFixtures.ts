import { computeVolumeProfile, type FineVolumeProfileResponse } from "./volumeProfile";
import type { Bar } from "./types";
import type { FineProfileRequest } from "./fineVolumeProfileModel";

export const FINE_START = 180000;
export function fineChartBars(step = 900, count = 2): Bar[] {
  return Array.from({ length: count }, (_, index) => ({ open_time: new Date((FINE_START + index * step) * 1000).toISOString(),
    close_time: new Date((FINE_START + (index + 1) * step) * 1000).toISOString(),
    available_at: new Date((FINE_START + (index + 1) * step) * 1000).toISOString(), is_final: true, revision: 1,
    open: "100", high: "124", low: "100", close: "120", volume: "240", quality_flags: [] }));
}
export const fineRequest: FineProfileRequest = { instrument_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", timeframe: "15m",
  from_time: FINE_START, to_time: FINE_START + 1800, rows: 48, value_area: 70 };
export function fineFixture(request = fineRequest, changes: Partial<FineVolumeProfileResponse> = {}, now = FINE_START + 1802): FineVolumeProfileResponse {
  const expected = Math.floor((request.to_time - request.from_time) / 60), used = changes.used_bars ?? expected;
  const bars = fineChartBars(60, used).map((bar, index) => ({ ...bar,
    open_time: new Date((request.from_time + index * 60) * 1000).toISOString() }));
  const local = computeVolumeProfile(bars, { from: -.5, to: bars.length - .5 }, { bins: request.rows, valueArea: request.value_area / 100 });
  return { version: "fine-ohlcv-profile-1", instrument_id: request.instrument_id, timeframe: request.timeframe,
    from_time: request.from_time, to_time: request.to_time, source_timeframe: "1m", source_step_seconds: 60,
    expected_bars: expected, used_bars: used, coverage: expected ? used / expected : 0,
    status: !used ? "NO_DATA" : used < expected ? "PARTIAL" : "AVAILABLE", quality_flags: [], generated_at: now, expires_at: now + 30,
    profile: { ...local, version: "fine-ohlcv-profile-1", method: "LOWER_TIMEFRAME_OHLCV_ESTIMATE" }, ...changes };
}
