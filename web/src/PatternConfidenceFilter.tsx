import { useEffect, useState } from "react";

export type ConfidenceFilter = { min?: number; max?: number; unscoredOnly?: boolean };

export function parseConfidenceRange(lower: string, upper: string): ConfidenceFilter {
  const parse = (value: string) => {
    if (!value.trim()) return undefined;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0 || number > 100) throw new Error("评分须在 0–100 分之间");
    return number / 100;
  };
  const min = parse(lower), max = parse(upper);
  if (min != null && max != null && min > max) throw new Error("最低评分不能高于最高评分");
  return { min, max };
}

export function confidenceParams(filter: ConfidenceFilter) {
  const params = new URLSearchParams();
  if (filter.unscoredOnly) params.set("unscored_only", "true");
  else {
    if (filter.min != null) params.set("confidence_min", String(filter.min));
    if (filter.max != null) params.set("confidence_max", String(filter.max));
  }
  return params.toString();
}

export function confidenceLabel(filter: ConfidenceFilter) {
  if (filter.unscoredOnly) return "仅未评分";
  if (filter.min == null && filter.max == null) return "全部评分（含未评分）";
  const score = (value: number) => Number((value * 100).toFixed(8));
  return `${score(filter.min ?? 0)}–${score(filter.max ?? 1)} 分`;
}

export function PatternConfidenceFilter({ value, busy, onApply, onReset }: {
  value: ConfidenceFilter; busy: boolean; onApply: (value: ConfidenceFilter) => void; onReset: () => void;
}) {
  const [lower, setLower] = useState(""), [upper, setUpper] = useState(""), [error, setError] = useState("");
  useEffect(() => {
    setLower(value.min == null ? "" : String(Number((value.min * 100).toFixed(8))));
    setUpper(value.max == null ? "" : String(Number((value.max * 100).toFixed(8))));
    setError("");
  }, [value]);
  function preset(next: ConfidenceFilter) { setError(""); onApply(next); }
  return <form className="pattern-confidence-filter" aria-label="置信度筛选" noValidate onSubmit={event => {
    event.preventDefault();
    if (busy) return;
    try { const next = parseConfidenceRange(lower, upper); setError(""); onApply(next); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "评分区间无效"); }
  }}>
    <div className="pattern-confidence-heading"><strong>置信度筛选 <small>几何评分 · 非胜率</small></strong><div className="pattern-confidence-presets">
      <button type="button" disabled={busy} onClick={() => preset({})}>全部评分</button>
      <button type="button" disabled={busy} onClick={() => preset({ min: .7 })}>70 分以上</button>
      <button type="button" disabled={busy} onClick={() => preset({ min: .85 })}>85 分以上</button>
      <button type="button" disabled={busy} onClick={() => preset({ unscoredOnly: true })}>仅未评分</button>
    </div></div>
    <div className="pattern-confidence-fields"><label>最低评分<input type="number" min="0" max="100" step="any" inputMode="decimal" aria-label="最低评分" value={lower} placeholder="0" onChange={event => { setLower(event.target.value); setError(""); }} /></label>
      <span aria-hidden="true">—</span><label>最高评分<input type="number" min="0" max="100" step="any" inputMode="decimal" aria-label="最高评分" value={upper} placeholder="100" onChange={event => { setUpper(event.target.value); setError(""); }} /></label>
      <button type="submit" disabled={busy}>{busy ? "筛选中…" : "应用筛选"}</button><button type="button" disabled={busy} onClick={() => { setError(""); onReset(); }}>重置筛选</button>
    </div>
    {error && <div className="pattern-filter-error" role="alert">{error}</div>}
    <small className="pattern-filter-applied" role="status">{busy ? "正在读取筛选结果…" : `已应用：${confidenceLabel(value)}`}</small>
  </form>;
}
