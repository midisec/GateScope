import type { Report, Trade } from "./researchModel";
import type { Bar } from "./types";

function sameCandle(a: Bar, b: Bar) {
  return a.open_time === b.open_time && a.open === b.open && a.high === b.high && a.low === b.low && a.close === b.close && a.volume === b.volume;
}

/** Only tail updates may use series.update; historical corrections always replace. */
export function candleUpdateKind(previous: Bar[], next: Bar[]): "none" | "tail" | "replace" {
  if (!previous.length || next.length < previous.length || next.length > previous.length + 1) return "replace";
  if (previous.some((bar, i) => i < previous.length - 1 && !sameCandle(bar, next[i]))) return "replace";
  if (previous.at(-1)!.open_time !== next[previous.length - 1].open_time) return "replace";
  return next.length === previous.length && sameCandle(previous.at(-1)!, next.at(-1)!) ? "none" : "tail";
}

export const timeNumber = (value: string) => Math.floor(new Date(value).getTime() / 1000);
/** Display precision, not an exchange order tick size. Keep low-price coins visible. */
export function chartPriceFormat(values: number[]) {
  const smallest = values.reduce((min, v) => Number.isFinite(v) && v !== 0 ? Math.min(min, Math.abs(v)) : min, Infinity);
  const precision = Number.isFinite(smallest) ? Math.max(2, Math.min(12, 4 - Math.floor(Math.log10(smallest)))) : 2;
  return { type: "price" as const, precision, minMove: 10 ** -precision };
}
export function priceText(value: number | undefined) {
  return value === undefined || !Number.isFinite(value) ? "—" : value.toLocaleString("en-US", { maximumFractionDigits: chartPriceFormat([value]).precision });
}
export function drawdownPoints(curve: Report["full"]["equity_curve"], initial: number) {
  let peak = initial;
  return curve.map(p => { const recorded = (p as typeof p & { peak_equity?: number }).peak_equity; peak = Math.max(peak, p.equity, typeof recorded === "number" && Number.isFinite(recorded) ? recorded : 0); return { ...p, peak, drawdown: peak > 0 ? 100 * (p.equity / peak - 1) : 0, amount: peak - p.equity }; });
}
export function drawdownSummary(report: Report) {
  const points = drawdownPoints(report.full.equity_curve, report.spec.initial_equity);
  if (!points.length) return undefined;
  const trough = points.reduce((worst, p, i) => p.drawdown < points[worst].drawdown ? i : worst, 0);
  let peak = 0;
  for (let i = 0; i <= trough; i++) if (points[i].equity >= points[peak].equity) peak = i;
  const recovered = points.findIndex((p, i) => i > trough && p.equity >= points[peak].equity);
  return { peak: points[peak], trough: points[trough], recovery: recovered < 0 ? undefined : points[recovered], seconds: timeNumber(points[recovered < 0 ? points.length - 1 : recovered].time) - timeNumber(points[peak].time) };
}
export function holdingLabel(trade: Trade) {
  return durationLabel(trade.holding_seconds ?? timeNumber(trade.exit_time) - timeNumber(trade.entry_time));
}
export function durationLabel(seconds: number) {
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`;
  return `${(seconds / 86400).toFixed(1)} 天`;
}
/** A bar is drawn at its open timestamp; accounting points may denote its close. */
export function reportTimeIndex(report: Report) {
  const candles = report.candles, opens = new Set(candles.map(b => timeNumber(b.time)));
  const closeToOpen = new Map<number, number>();
  if (report.input_bars?.length) for (const bar of report.input_bars) closeToOpen.set(timeNumber(bar.close_time), timeNumber(bar.open_time));
  else candles.forEach((bar, i) => closeToOpen.set(timeNumber(candles[i + 1]?.time ?? report.range.to), timeNumber(bar.time)));
  return { opens, closeToOpen };
}
export function tradeBarTimes(report: Report, trade: Trade, index = reportTimeIndex(report)) {
  const { opens, closeToOpen } = index, entry = timeNumber(trade.entry_time);
  const exit = trade.exit_bar_open_time ? timeNumber(trade.exit_bar_open_time) : closeToOpen.get(timeNumber(trade.exit_time));
  return { entry: opens.has(entry) ? entry : undefined, exit: exit !== undefined && opens.has(exit) ? exit : undefined };
}
export function alignedCurve(report: Report) {
  const { opens, closeToOpen } = reportTimeIndex(report), points = new Map<number, ReturnType<typeof drawdownPoints>[number] & { chartTime: number }>();
  for (const p of drawdownPoints(report.full.equity_curve, report.spec.initial_equity)) {
    const time = p.bar_open_time ? timeNumber(p.bar_open_time) : closeToOpen.get(timeNumber(p.time));
    if (time !== undefined && opens.has(time)) points.set(time, { ...p, chartTime: time });
  }
  return [...points.values()].sort((a, b) => a.chartTime - b.chartTime);
}
/** Never shift an indicator to the nearest bar or connect across a missing point. */
export function alignedIndicator<T extends { open_time: string; ready: boolean; values: Record<string, number | null> }>(bars: Array<{ open_time: string }>, points: T[], output: string) {
  const byTime = new Map(points.map(p => [timeNumber(p.open_time), p]));
  return bars.map(b => { const time = timeNumber(b.open_time), p = byTime.get(time), value = p?.values[output]; return p?.ready && typeof value === "number" && Number.isFinite(value) ? { time, value } : { time }; });
}
