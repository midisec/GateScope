import { useEffect, useState } from "react";
import { api } from "./api";
import { fmt, stateLabel, strategyName, type Run } from "./researchModel";
import { PageState, Pagination } from "./ui";
import { screeningLabel } from "./PerformanceScreen";

export function ResearchHistory({ selectedId, onSelect }: { selectedId: string; onSelect: (run: Run) => void }) {
  const [page, setPage] = useState(0), [items, setItems] = useState<Run[]>([]), [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true), [error, setError] = useState(""), [attempt, setAttempt] = useState(0);
  const [screening, setScreening] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(""); setItems([]);
    void api<{ items: Run[]; total: number }>(`/api/v2/research/backtests?engine_mode=INDICATOR&limit=10&offset=${page * 10}&screening=${screening}`, { signal: controller.signal }).then(r => {
      if (!controller.signal.aborted) { setItems(r.items); setTotal(r.total ?? r.items.length); }
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [page, attempt, screening]);
  return <section className="panel research-history"><header><h2>历史回测</h2><select aria-label="绩效记录筛选" value={screening} onChange={e => { setScreening(e.target.value); setPage(0); }}><option value="">全部记录</option><option value="OBSERVE_ONLY">绩效观察</option><option value="PASS">历史参考达标</option><option value="REJECTED">历史参考未达标</option><option value="UNASSESSED">尚未计算指标</option></select></header>
    <PageState loading={loading} error={error} onRetry={() => setAttempt(n => n + 1)} />
    <div className="research-history-list">{items.map(r => <button key={r.id} className={r.id === selectedId ? "selected" : ""} onClick={() => onSelect(r)}><span><strong>{r.symbol} <small>{r.timeframe} · {strategyName[r.spec.strategy]}</small></strong><small>{new Date(r.created_at).toLocaleString()} · {stateLabel[r.state]} · {screeningLabel(r.screening)}</small><small>验证夏普 {fmt(r.validation?.sharpe_ratio)} · t {fmt(r.validation?.t_statistic)} · 命中率 {fmt(r.validation?.win_rate, true)}</small></span><span className={(r.metrics?.net_pnl ?? 0) < 0 ? "research-negative" : "research-positive"}><strong>{fmt(r.metrics?.total_return, true)}</strong><small>{r.metrics ? `${r.metrics.trade_count} 笔交易` : r.error ?? "等待结果"}　查看 →</small></span></button>)}</div>
    {!items.length && !loading && !error && <p>暂无匹配记录。历史参考评估不再限制部署。</p>}
    <Pagination label="回测历史" page={page} pages={Math.max(1, Math.ceil(total / 10))} total={total} onChange={setPage} busy={loading} />
  </section>;
}
