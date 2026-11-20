import { useEffect, useState, type FormEvent } from "react";
import { api, jsonBody } from "./api";
import { Dialog, PageState, useAction } from "./ui";
import { ProbabilitySummary } from "./ReportDimensions";
import { fmt, activeRun, stateLabel, type Run, type Spec, type Strategy } from "./researchModel";
import type { Instrument } from "./types";
import { ReplayMode, ResearchCoverage } from "./ResearchAvailability";
import { ResearchDiagnostics, RuleResults } from "./ResearchRuleResults";

export function ChartStatistics({ instrument, timeframe, canCreate, onClose, onApplyIndicators }: { instrument: Instrument; timeframe: string; canCreate: boolean; onClose: () => void; onApplyIndicators: (spec: Spec) => void }) {
  const [catalog, setCatalog] = useState<Strategy[]>([]), [spec, setSpec] = useState<Spec>();
  const [runs, setRuns] = useState<Run[]>([]), [selected, setSelected] = useState("");
  const [run, setRun] = useState<Run>(), [error, setError] = useState(""), [loading, setLoading] = useState(true), [retry, setRetry] = useState(0);
  const [detailError, setDetailError] = useState(""), [detailLoading, setDetailLoading] = useState(false);
  const action = useAction(setError);
  const [barLimit, setBarLimit] = useState(1500);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ items: Strategy[]; defaults: Spec }>("/api/v2/research/strategies", { signal: controller.signal }).then(c => { setCatalog(c.items); setSpec(old => old ?? c.defaults); }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    async function load() {
      try {
        const page = await api<{ items: Run[] }>(`/api/v2/research/backtests?instrument_id=${encodeURIComponent(instrument.id)}&timeframe=${timeframe}&limit=20`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        // Also guard on the client: a report for another chart must never be labelled as this chart's statistics.
        const rows = page.items.filter(r => r.instrument_id === instrument.id && r.timeframe === timeframe);
        setRuns(rows); setSelected(old => old || rows[0]?.id || "");
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "读取失败"); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void load(); const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [instrument.id, timeframe, retry]);
  const selectedState = runs.find(r => r.id === selected)?.state;
  useEffect(() => {
    setRun(undefined); setDetailError("");
    if (!selected) return;
    const controller = new AbortController(); setDetailLoading(true);
    void api<Run>(`/api/v2/research/backtests/${selected}`, { signal: controller.signal }).then(r => {
      if (controller.signal.aborted) return;
      if (r.instrument_id !== instrument.id || r.timeframe !== timeframe) throw new Error("报告标的或周期与当前图表不一致");
      setRun(r);
    }).catch(e => { if (!controller.signal.aborted) setDetailError(e.message); }).finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selected, selectedState, instrument.id, timeframe, retry]);
  function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (!spec) return;
    const form = new FormData(e.currentTarget); setError("");
    const payload = { ...spec, strategy: String(form.get("strategy")), direction: String(form.get("direction")), fast_period: Number(form.get("fast")), slow_period: Number(form.get("slow")), stop_atr: Number(form.get("stop")), reward_risk: Number(form.get("rr")), fee_bps: Number(form.get("fee")), slippage_bps: Number(form.get("slip")) };
    if (payload.fast_period >= payload.slow_period) { setError("EMA 快线必须小于慢线"); return; }
    void action.run("提交计算中…", async () => {
      const created = await api<Run>("/api/v2/research/backtests", { method: "POST", body: jsonBody({ instrument_id: instrument.id, timeframe, spec: payload, bar_limit: Number(form.get("limit")) }) });
      setSpec(payload); setRuns(old => [created, ...old]); setSelected(created.id);
    });
  }
  return <Dialog title="历史胜率与概率" onClose={onClose} busy={!!action.busy}><div className="chart-stat-identity"><strong>{instrument.symbol} · {timeframe}</strong><small>已终结 K 线 · 固定交易规则 · 扣成本统计</small></div><PageState loading={loading} error={error} onRetry={() => { setError(""); setRetry(n => n + 1); }} />{spec && <form className="research-form" onSubmit={create}>
    <label>统计策略<select name="strategy" defaultValue={spec.strategy}>{catalog.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label><label>样本 K 线<input name="limit" type="number" min={300} max={5000} required value={barLimit} onChange={e => setBarLimit(Number(e.target.value))} /></label>
    <ReplayMode value={spec.availability_mode} onChange={availability_mode => setSpec({ ...spec, availability_mode })} /><ResearchCoverage instrument={instrument.id} timeframe={timeframe} limit={barLimit} />
    <label>方向<select name="direction" defaultValue={spec.direction}><option value="LONG">只做多</option>{instrument.market.toLowerCase() !== "spot" && <><option value="SHORT">只做空</option><option value="BOTH">双向</option></>}</select></label><label>止盈 / 止损比<input name="rr" type="number" min={.5} max={5} step={.1} defaultValue={spec.reward_risk} required /></label>
    <label>EMA 快线<input name="fast" type="number" min={3} max={100} defaultValue={spec.fast_period} required /></label><label>EMA 慢线<input name="slow" type="number" min={10} max={200} defaultValue={spec.slow_period} required /></label><label>止损 ATR<input name="stop" type="number" min={.5} max={6} step={.1} defaultValue={spec.stop_atr} required /></label><label>单边手续费 bp<input name="fee" type="number" min={0} max={100} step={.1} defaultValue={spec.fee_bps} required /></label><label>单边滑点 bp<input name="slip" type="number" min={0} max={100} step={.1} defaultValue={spec.slippage_bps} required /></label>
    <div className="research-wide"><button className="primary" disabled={!canCreate || !!action.busy || runs.some(activeRun)}>{action.busy || (runs.some(activeRun) ? "后台计算中…" : canCreate ? "计算并保存历史统计" : "观察者只读")}</button><small className="stats-rule-note">独立统计所选交易规则，不将任意叠加指标当作概率；完整参数可在回测页面调整。</small></div>
  </form>}
  {!!runs.length && <label className="stats-history">本标的 / 周期最近 20 次统计<select value={selected} onChange={e => setSelected(e.target.value)}>{runs.map(r => <option key={r.id} value={r.id}>{new Date(r.created_at).toLocaleString()} · {r.spec.strategy} · {stateLabel[r.state]}</option>)}</select></label>}
  <PageState loading={detailLoading} error={detailError} onRetry={() => setRetry(n => n + 1)} />
  {run && activeRun(run) && <p role="status">任务已保存，后台{stateLabel[run.state]}，完成后自动展示。关闭弹窗不会取消计算。</p>}
  {run?.state === "FAILED" && <PageState error={run.error || "计算失败"} />}
  {run?.report && <><div className="stats-report-range">{new Date(run.report.range.from).toLocaleString()} — {new Date(run.report.range.to).toLocaleString()} · {run.report.bar_count} 根 · 净损益 {fmt(run.report.full.metrics.net_pnl)}</div><ResearchDiagnostics report={run.report} requested={run.bar_limit} /><RuleResults report={run.report} /><ProbabilitySummary report={run.report} /><div className="button-row"><button onClick={() => onApplyIndicators(run.report!.spec)}>应用基础指标参数到图表</button><a href={`/strategies/backtests?run=${run.id}`}>查看逐笔交易与联动图表 →</a></div></>}
  </Dialog>;
}
