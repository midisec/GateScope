import {
  coinglassMetricReadiness, coinglassSeriesUsable,
  type CoinglassBundle, type CoinglassMetricId, type CoinglassPoint, type CoinglassSeries,
} from "./coinglassModel";
import type { Bar } from "./types";

export type CoinglassChartMetricId = CoinglassMetricId | "oi_change";
export type CoinglassChartStatus = "READY" | "HISTORICAL" | "UNAVAILABLE";
export type CoinglassChartMetricMetadata = {
  id: CoinglassChartMetricId; title: string; description: string; unit: "USD" | "PERCENT" | "RATIO";
  color: string; type: "line" | "histogram"; paneId: string; baseline?: number;
};
export type CoinglassChartDatum = { time: number; value?: number; actualValue?: number; color?: string };
export type CoinglassChartStudy = CoinglassChartMetricMetadata & {
  status: CoinglassChartStatus; statusReason: string; data: CoinglassChartDatum[];
  /** Render each line segment separately: chart libraries may bridge whitespace. */
  segments: CoinglassChartDatum[][];
  latest: { time: number; closeTime: number; availableAt: number; value: number; plotValue: number } | null;
  validPoints: number; closedBars: number; instrumentId: string | null;
};

export const COINGLASS_CHART_CATALOG: readonly CoinglassChartMetricMetadata[] = [
  { id: "oi", title: "OI 美元估值", description: "未平仓合约的美元估值，不代表净流入资金。", unit: "USD", color: "#a78bfa", type: "line", paneId: "coinglass:oi" },
  { id: "oi_change", title: "OI美元估值变化率", description: "相邻原生已收盘周期的 OI 美元估值变化率；受价格与持仓变化影响，不是真实资金流。", unit: "PERCENT", color: "#38bdf8", type: "histogram", paneId: "coinglass:oi_change", baseline: 0 },
  { id: "funding", title: "资金费率", description: "使用数据源核实的百分数口径，不额外乘以 100。", unit: "PERCENT", color: "#fbbf24", type: "line", paneId: "coinglass:funding", baseline: 0 },
  { id: "lsr", title: "全账户多空比", description: "账户数量比值，1 为多空数量相等；不是仓位金额之比。", unit: "RATIO", color: "#60a5fa", type: "line", paneId: "coinglass:lsr", baseline: 1 },
  { id: "liquidation_long", title: "多单实际爆仓（向上）", description: "实际爆仓金额非负，与空单爆仓共用美元坐标。", unit: "USD", color: "#f0526e", type: "histogram", paneId: "coinglass:liquidations", baseline: 0 },
  { id: "liquidation_short", title: "空单实际爆仓（向下镜像）", description: "向下柱仅用于区分方向；实际爆仓金额非负，不是负资金流。", unit: "USD", color: "#22c997", type: "histogram", paneId: "coinglass:liquidations", baseline: 0 },
];

const STEPS: Record<string, number> = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "1w": 604800 };
const nativeId = (id: CoinglassChartMetricId): CoinglassMetricId => id === "oi_change" ? "oi" : id;
const validEpoch = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value > 0;
const epoch = (value: string | undefined) => value === undefined ? NaN : Date.parse(value) / 1000;
const metadata = (id: CoinglassChartMetricId) => COINGLASS_CHART_CATALOG.find(item => item.id === id);

function sourceFor(bundle: CoinglassBundle | undefined, id: CoinglassChartMetricId) {
  // Duplicated metric IDs are ambiguous, not an invitation to pick a winner.
  const sources = bundle?.series?.filter(series => series.id === nativeId(id));
  return sources?.length === 1 ? sources[0] : undefined;
}

function nativePoints(source: CoinglassSeries, step: number, now: number) {
  const points = new Map<number, CoinglassPoint>();
  const seen = new Set<number>();
  for (const point of source.points) {
    if (!validEpoch(point.time)) continue;
    if (seen.has(point.time)) { points.delete(point.time); continue; }
    seen.add(point.time);
    if (!Number.isInteger(point.time) || !validEpoch(point.close_time) || point.close_time !== point.time + step
        || !validEpoch(point.available_at) || point.available_at < point.close_time || point.available_at > now
        || point.close_time > now || !Number.isFinite(point.value)
        || (source.id !== "funding" && point.value < 0)) continue;
    points.set(point.time, point);
  }
  return points;
}

export function coinglassChartMetricStatus(bundle: CoinglassBundle | undefined, id: CoinglassChartMetricId,
  now = Date.now() / 1000): { status: CoinglassChartStatus; reason: string } {
  if (!bundle) return { status: "UNAVAILABLE", reason: "按需读取" };
  if (!validEpoch(now)) return { status: "UNAVAILABLE", reason: "当前时间不可核实" };
  if (["NO_CONFIG", "NOT_CONFIGURED", "DISABLED"].includes(bundle.status)) return { status: "UNAVAILABLE", reason: "请先配置 Coinglass" };
  if (bundle.match?.status !== "EXACT") return { status: "UNAVAILABLE", reason: "合约维度未精确匹配" };
  const source = sourceFor(bundle, id), step = STEPS[bundle.timeframe];
  if (!source) return { status: "UNAVAILABLE", reason: ["QUEUED", "PENDING", "WARMING"].includes(bundle.status) ? "等待后台采集" : "暂无该指标数据" };
  if (!step || !coinglassSeriesUsable(source, bundle.timeframe)) return { status: "UNAVAILABLE", reason: "周期或数据单位未核实" };
  const points = nativePoints(source, step, now);
  if (!points.size) return { status: "UNAVAILABLE", reason: "暂无已收盘且已可用的数据" };
  if (id === "oi_change" && ![...points.values()].some(point => {
    const prior = points.get(point.time - step);
    return prior && prior.value > 0 && Number.isFinite((point.value - prior.value) / prior.value * 100);
  })) return { status: "UNAVAILABLE", reason: "需要相邻已收盘 OI 样本且前值大于零" };
  // Aggregate STALE/cache_retained can describe a different metric. Explicit
  // per-series collection clocks take precedence, while legacy bundles still
  // require the aggregate freshness contract. Never revive a stale series.
  const ownClock = validEpoch(source.fetched_at) && validEpoch(source.expires_at);
  const readiness = coinglassMetricReadiness(ownClock ? { ...bundle, status: "PARTIAL" } : bundle, nativeId(id), now);
  const fetched = source.fetched_at ?? bundle.fetched_at, expires = source.expires_at ?? bundle.expires_at;
  let latestClose = 0, latestAvailable = 0;
  for (const point of points.values()) {
    latestClose = Math.max(latestClose, point.close_time);
    latestAvailable = Math.max(latestAvailable, point.available_at);
  }
  if (readiness === "HISTORICAL" || !ownClock && bundle.cache_retained || expires != null && expires <= now)
    return { status: "HISTORICAL", reason: "过期缓存，仅供历史参考" };
  if (!validEpoch(fetched) || fetched > now || !validEpoch(expires) || expires <= fetched || fetched < latestAvailable)
    return { status: "HISTORICAL", reason: "新鲜度时钟未核实，仅供历史参考" };
  if (now - latestClose >= step * 2) return { status: "HISTORICAL", reason: "最新原生周期数据滞后" };
  return { status: "READY", reason: "已收盘数据可用" };
}

type ChartBar = Pick<Bar, "open_time" | "close_time" | "available_at" | "is_final">;

/** Values use exact candle-open timestamps; blanks are never substituted by zero. */
export function buildCoinglassChartStudies(bundle: CoinglassBundle | undefined,
  selected: readonly CoinglassChartMetricId[], bars: readonly ChartBar[], timeframe: string,
  now = Date.now() / 1000, expectedInstrumentId?: string): CoinglassChartStudy[] {
  const step = STEPS[timeframe];
  const byTime = new Map<number, ChartBar>();
  const duplicates = new Set<number>();
  for (const bar of bars) {
    const time = epoch(bar.open_time);
    if (!validEpoch(time) || !Number.isInteger(time)) continue;
    if (byTime.has(time)) duplicates.add(time);
    byTime.set(time, bar);
  }
  const times = [...byTime.keys()].sort((a, b) => a - b);
  const closed = new Set(times.filter(time => {
    const bar = byTime.get(time)!;
    const close = epoch(bar.close_time), available = bar.available_at === undefined ? close : epoch(bar.available_at);
    return !!step && !duplicates.has(time) && bar.is_final === true && close === time + step
      && available >= close && available <= now && close <= now;
  }));
  return [...new Set(selected)].flatMap(id => {
    const info = metadata(id);
    if (!info) return [];
    let state = coinglassChartMetricStatus(bundle, id, now);
    if (!step || bundle && bundle.timeframe !== timeframe) state = { status: "UNAVAILABLE", reason: "原生周期与图表周期不一致" };
    if (expectedInstrumentId !== undefined && bundle && bundle.instrument_id !== expectedInstrumentId)
      state = { status: "UNAVAILABLE", reason: "数据与当前图表合约不一致" };
    const source = sourceFor(bundle, id);
    const points = source && state.status !== "UNAVAILABLE" ? nativePoints(source, step, now) : new Map<number, CoinglassPoint>();
    const data: CoinglassChartDatum[] = [];
    const segments: CoinglassChartDatum[][] = [];
    let segment: CoinglassChartDatum[] = [];
    let latest: CoinglassChartStudy["latest"] = null, validPoints = 0;
    for (let index = 0; index < times.length; index++) {
      const time = times[index], previousTime = times[index - 1];
      // Never add synthetic times: they would shift other overlays' chart indices.
      if (step && index && time !== previousTime + step) segment = [];
      const point = closed.has(time) ? points.get(time) : undefined;
      const prior = id === "oi_change" && closed.has(time - step) ? points.get(time - step) : undefined;
      const actual = point && (id !== "oi_change" ? point.value : prior && prior.value > 0
        ? (point.value - prior.value) / prior.value * 100 : undefined);
      if (actual === undefined || !Number.isFinite(actual) || !point) { data.push({ time }); segment = []; continue; }
      const value = id === "liquidation_short" && actual !== 0 ? -actual : actual;
      const datum = { time, value, actualValue: actual,
        ...(id === "oi_change" ? { color: value >= 0 ? "#22c997" : "#f0526e" } : {}) };
      data.push(datum);
      if (!segment.length) segments.push(segment);
      segment.push(datum);
      latest = { time, closeTime: point.close_time, availableAt: Math.max(point.available_at, prior?.available_at ?? 0), value: actual, plotValue: value };
      validPoints++;
    }
    if (!validPoints && state.status !== "UNAVAILABLE") state = { status: "UNAVAILABLE", reason: "当前图表区间暂无精确对齐的已收盘数据" };
    return [{ ...info, status: state.status, statusReason: state.reason, data, segments, latest,
      validPoints, closedBars: closed.size, instrumentId: bundle?.instrument_id ?? null }];
  });
}
