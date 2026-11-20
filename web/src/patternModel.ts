import type { Bar } from "./types";
import type { PatternScreeningSummaryData } from "./patternScreeningModel";
import type { ProbabilityReliabilityData } from "./ProbabilityReliability";

export type Phase = "FORMING" | "FORMED" | "CONFIRMED" | "INVALIDATED" | "EXPIRED" | "CENSORED";
export const phaseLabel: Record<Phase, string> = { FORMING: "形成中", FORMED: "已形成", CONFIRMED: "已确认", INVALIDATED: "已失效", EXPIRED: "到期未完成", CENSORED: "观测中断" };
export type PatternPoint = { time: number; price: number; label: string; basis?: "HIGH" | "LOW" | "CLOSE" };
export type PatternEdge = { from: PatternPoint; to: PatternPoint; style: string; role?: string; curve?:{vertex:PatternPoint} };
export type CompletionProbability = { sample_count?: number | null; successes: number | null; rate: number | null; interval: { lower: number; upper: number } | null; score_bucket?: string; partial?: boolean; report_as_of?: number;
  reference_phase?: "FORMING" | "FORMED" | null; reference_as_of?: number | null; reference_score_bucket?: string | null;
  reference_state?: "READY" | "AVAILABLE" | "NO_MATCHING_SAMPLES" | "HISTORY_NOT_READY" | "STAGE_UNAVAILABLE"; reference_basis?: string; reference_reason?: string; reference_label?: string; reference_is_live_prediction?: boolean; as_of?: number;
  probability?:number|null; formation?:CompletionProbability|null; precision?:"INSUFFICIENT"|"WIDE"|"MODERATE"|"NARROW"; method?:string;
  recorded_count?:number|null;replay_count?:number|null;invalidated?:number|null;expired?:number|null;
  failure_examples?:{context_id:string;id:string;phase:Phase;observed_time:number;source:string}[] };
export type CompletionRow = CompletionProbability & { pattern: string; phase: Phase; score_bucket: string; total: number; pending: number; overlap_excluded: number;
  FORMED: number; FORMING: number; CONFIRMED: number; INVALIDATED: number; EXPIRED: number; CENSORED: number; formed_count: number; calibration_count: number; brier: number | null; reliability?: ProbabilityReliabilityData };
export type Forecast = { id:string; kind:string; issued_at:number; deadline_time:number; source:string; history_count:number; status:string; condition:string;
  version?:string; guideline_fit?:"STRUCTURAL_FALLBACK"|"REFERENCE_RANGE"; confirmation_basis_time?:number;
  unavailable_reason?:string; confirmation_closes:number; confirmation:{side:number;price:number}[];
  target:{price:number;lower:number;upper:number;theory_price:number;label:string}|null };
export type PatternEvent = {
  id: string; evidence_id: string; pattern: string; name: string; group: string;
  phase: Phase; experimental?: boolean; confidence: number | null;
  direction: string; signal_direction: string; start_time: number; end_time: number;
  points: PatternPoint[]; lines: PatternEdge[];
  context_id?: string; first_observed_time?: number; observed_time?: number; deadline_time?: number; historical_reference?: CompletionProbability;
  initial_prediction?:CompletionProbability|null;
  recognition?:{checks:string[];warnings:string[];rules_version:string;prior_trend?:{status:string}};
  forecast?:Forecast; initial_forecast?:Forecast; forecast_outcome?:{status:string;observed_time?:number};
  pre_formation_forecast?:Forecast|null; structure_formed_at?:{price:number;pivot_time:number;known_at:number}|null;
  formation_observation?:{price:number;pivot_time:number;known_at:number;predicted_before_pivot:boolean}|null;
  levels?: { invalid_price?: number; invalid_edge?: PatternEdge; buffer: number; atr: number; hard?: boolean; invalid_side?: number; trigger?: PatternEdge };
  stage: string; note: string;
  quality?: "STANDARD" | "LOW_FIT" | "UNSCORED";
};
export type PatternMeta = { id: string; name: string; family: "chart" | "wave"; group: string; supports_forming?: boolean };
export type PatternCurrent = { items: PatternEvent[]; catalog: PatternMeta[]; as_of: number | null; bars_scanned: number; version: string; tracking_status?: string;
  history_status?: { state: string; cursor: number | null; total: number | null; through_time?: number | null; version?: string };
  total:number; tracked_total:number; offset:number; limit:number; phase_counts:Record<Phase,number>; summary?:PatternScreeningSummaryData };
export type StudyRow = { pattern: string; phase: Phase; horizon: number; detected: number; pending: number; overlap_excluded: number; neutral_excluded: number;
  sample_count: number; win_rate: number | null; interval: { lower: number; upper: number } | null; mean_net_return: number | null; mean_mfe: number | null; mean_mae: number | null;
  recent_samples: { observed_time: number; entry_time: number; exit_time: number; side: number; net_return: number; evidence_id: string }[] };
export type PatternStudy = { state: "NOT_REQUESTED" | "QUEUED" | "RUNNING" | "READY" | "FAILED"; cursor: number; total: number; requested_at?: string; updated_at?: string; error?: string;
  report?: { version: string; catalog?: PatternMeta[]; completion_rows?: CompletionRow[]; candidate_count?: number; forecast_outcomes?:Record<string,number>; partial?: boolean; bar_count: number; from_time: number; to_time: number; round_trip_cost: number; metadata: { snapshot_at: string; quality_flags: string[]; input_digest: string;period_days?:number;target_bars?:number;missing_bars?:number;coverage_ratio?:number;mode?:string;range_policy?:string;listing_time?:number|null }; rows: StudyRow[] } | null };
export const overlayColors = ["#e6b765", "#b39bff", "#63cfdd"];
export const patternPercent = (v: number | null | undefined) => v == null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(1)}%`;
export const patternTime = (v: number | null | undefined) => v == null ? "—" : new Date(v * 1000).toLocaleString();
export const sampleCountLabel = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? "—" : value.toLocaleString();
export function studySampleSummary(report: PatternStudy["report"], phase: Phase, horizon: number) {
  const rows = report?.rows.filter(row => row.phase === phase && row.horizon === horizon) ?? [];
  const sum = (field: "sample_count" | "overlap_excluded" | "pending") => rows.length ? rows.reduce((total, row) => total + row[field], 0) : null;
  return { detected: report?.candidate_count, effective: sum("sample_count"), overlap: sum("overlap_excluded"), pending: sum("pending") };
}
export function overlayGeometry(event: PatternEvent, bars: Bar[]) {
  type DrawEdge = PatternEdge & { points:PatternPoint[] };
  const empty = (status:string) => ({points:[] as PatternPoint[],lines:[] as DrawEdge[],status,hiddenAnchors:event.points.length});
  const ordered = bars.map(b => Date.parse(b.open_time)/1000), times = new Set(ordered);
  const first = ordered[0], last = ordered.at(-1);
  if (first == null || last == null || !event.points.length) return empty("OUTSIDE_RANGE");
  const valid = (p:PatternPoint) => Number.isFinite(p.time) && Number.isFinite(p.price) && p.price>0;
  const anchors = [...event.points,...event.lines.flatMap(e=>[e.from,e.to])];
  if (anchors.some(p=>!valid(p))) return empty("INVALID");
  const byTime=new Map(bars.map(b=>[Date.parse(b.open_time)/1000,b]));
  if(event.points.some(p=>{
    const bar=byTime.get(p.time), value=bar && p.basis ? Number(bar[p.basis.toLowerCase() as "high"|"low"|"close"]) : NaN;
    return Number.isFinite(value) && Math.abs(value-p.price)>Math.max(1e-12,Math.abs(p.price)*1e-10);
  })) return empty("REVISED_CANDLE");
  // Missing INTERNAL candles are not range clipping. Never snap or invent one.
  if (anchors.some(p=>p.time>=first && p.time<=last && !times.has(p.time))) return empty("MISSING_CANDLE");
  const lines:DrawEdge[] = [];
  for (const e of event.lines) {
    const [a,b] = [e.from,e.to].sort((x,y)=>x.time-y.time);
    if (a.time===b.time || b.time<=first || a.time>=last) continue;
    const clip = (t:number):PatternPoint => ({time:t,price:guideValue(e,t),label:""});
    const from = a.time<first ? clip(first) : a, to = b.time>last ? clip(last) : b;
    // Clipped intersections belong to guides only; never put them in pivots.
    lines.push({...e,from,to,points:[from,to]});
  }
  const points = event.points.filter(p=>times.has(p.time)).sort((a,b)=>a.time-b.time);
  const hiddenAnchors = event.points.length-points.length;
  return {points,lines,hiddenAnchors,status:!lines.length && !points.length ? "OUTSIDE_RANGE" : anchors.some(p=>p.time<first || p.time>last) ? "CLIPPED" : "COMPLETE"};
}

// Sample arithmetic-price guides at real candle timestamps, including on log axes.
export function sampledGuide(edge: PatternEdge, bars: Bar[]) {
  const start = Math.min(edge.from.time, edge.to.time), end = Math.max(edge.from.time, edge.to.time);
  if (start === end) return [];
  return bars.map(b => Date.parse(b.open_time)/1000).filter(t => t>=start && t<=end).map(time => ({ time,
    value: guideValue(edge,time) }));
}

export function guideValue(edge:PatternEdge,time:number) {
  if(edge.curve){
    const v=edge.curve.vertex, rim=Math.abs(edge.from.time-v.time)>Math.abs(edge.to.time-v.time)?edge.from:edge.to;
    if(rim.time!==v.time)return v.price+(rim.price-v.price)*((time-v.time)/(rim.time-v.time))**2;
  }
  return edge.from.price+(edge.to.price-edge.from.price)*(time-edge.from.time)/(edge.to.time-edge.from.time);
}
