import type { CoinglassChartMetricId } from "./curatedCoinglass";

export type ChartStudyId = CoinglassChartMetricId | "vpvr";
export type ChartStudiesCategory = "all" | "coinglass" | "volume";
export type ChartStudiesSettings = {
  version: 1;
  coinglassMetrics: CoinglassChartMetricId[];
  vpvr: { enabled: boolean; rows: 24 | 48 | 96; valueAreaPercent: number; widthPercent: number };
};

export const CHART_COINGLASS_METRICS: readonly CoinglassChartMetricId[] = [
  "oi", "funding", "lsr", "liquidation_long", "liquidation_short", "oi_change",
];
export const VPVR_LIMITS = { valueArea: { min: 50, max: 95 }, width: { min: 10, max: 40 } } as const;

export function defaultChartStudiesSettings(): ChartStudiesSettings {
  return { version: 1, coinglassMetrics: [], vpvr: { enabled: false, rows: 48, valueAreaPercent: 70, widthPercent: 25 } };
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}
function boundedInteger(value: unknown, min: number, max: number, fallback: number) {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max ? value : fallback;
}

/** Layout input is untrusted. Only this version and the seven known studies survive. */
export function normalizeChartStudiesSettings(value: unknown): ChartStudiesSettings {
  const defaults = defaultChartStudiesSettings(), source = record(value);
  if (!source || source.version !== 1) return defaults;
  const metrics = Array.isArray(source.coinglassMetrics) ? source.coinglassMetrics.slice(0, 64) : [];
  const coinglassMetrics = [...new Set(metrics.filter((item): item is CoinglassChartMetricId =>
    typeof item === "string" && CHART_COINGLASS_METRICS.includes(item as CoinglassChartMetricId)))];
  const vpvr = record(source.vpvr);
  return {
    version: 1, coinglassMetrics,
    vpvr: {
      enabled: vpvr?.enabled === true,
      rows: vpvr?.rows === 24 || vpvr?.rows === 48 || vpvr?.rows === 96 ? vpvr.rows : defaults.vpvr.rows,
      valueAreaPercent: boundedInteger(vpvr?.valueAreaPercent, VPVR_LIMITS.valueArea.min, VPVR_LIMITS.valueArea.max, defaults.vpvr.valueAreaPercent),
      widthPercent: boundedInteger(vpvr?.widthPercent, VPVR_LIMITS.width.min, VPVR_LIMITS.width.max, defaults.vpvr.widthPercent),
    },
  };
}

export function selectedChartStudies(settings: ChartStudiesSettings): ChartStudyId[] {
  const normalized = normalizeChartStudiesSettings(settings);
  return [...normalized.coinglassMetrics, ...(normalized.vpvr.enabled ? ["vpvr" as const] : [])];
}

export function setChartStudyEnabled(settings: ChartStudiesSettings, id: ChartStudyId, enabled: boolean): ChartStudiesSettings {
  const normalized = normalizeChartStudiesSettings(settings);
  if (id === "vpvr") return { ...normalized, vpvr: { ...normalized.vpvr, enabled } };
  if (!CHART_COINGLASS_METRICS.includes(id)) return normalized;
  const others = normalized.coinglassMetrics.filter(metric => metric !== id);
  return { ...normalized, coinglassMetrics: enabled
    ? normalized.coinglassMetrics.includes(id) ? normalized.coinglassMetrics : [...others, id]
    : others };
}

export function toggleChartStudy(settings: ChartStudiesSettings, id: ChartStudyId): ChartStudiesSettings {
  return setChartStudyEnabled(settings, id, !selectedChartStudies(settings).includes(id));
}

/** Removing studies does not discard the user's preferred VPVR rendering options. */
export function clearChartStudies(settings: ChartStudiesSettings): ChartStudiesSettings {
  const normalized = normalizeChartStudiesSettings(settings);
  return { ...normalized, coinglassMetrics: [], vpvr: { ...normalized.vpvr, enabled: false } };
}

export type ChartStudySearchItem = { id: ChartStudyId; title: string; description: string; category: Exclude<ChartStudiesCategory, "all">; keywords?: string };
export function filterChartStudies<T extends ChartStudySearchItem>(items: readonly T[], query: string, category: ChartStudiesCategory): T[] {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return items.filter(item => (category === "all" || item.category === category)
    && terms.every(term => `${item.id} ${item.title} ${item.description} ${item.keywords ?? ""}`.toLocaleLowerCase().includes(term)));
}
