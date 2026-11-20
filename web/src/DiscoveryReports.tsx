import { useEffect, useState } from "react";
import { api } from "./api";
import { fmt, type Metrics, type Screening } from "./researchModel";
import { Dialog, PageState, Pagination } from "./ui";
import { DirectionalDiscovery, type DirectionalStudy } from "./DirectionalDiscovery";
import { observationNotes, screeningLabel } from "./PerformanceScreen";

type HedgeTrade = { entry_time: string; exit_time: string; spot_entry: number; perp_entry: number; spot_exit: number; perp_exit: number; quantity: number; net_pnl: number; fees: number; funding_income: number; exit_reason: string };
type Choice = { key: string; name: string; symbol: string; family: string; metrics: Metrics; screening: Screening; stress_metrics: Metrics; stress_screening: Screening; annualized_return: number; holding_days: number; funding_mark_proxy_count: number; trades: HedgeTrade[]; annual: { year: number; return_fraction: number }[] };
type Discovery = { kind?: "CARRY"; title: string; from_time: string; to_time: string; venue: string; development_trials: number; development_finalists: number; carry_runs: number; candidates: Choice[]; notes: string[]; artifact_digest: string; carry_results: { symbol: string; family: string; cost_multiplier: number; metrics: Metrics; screening: Screening }[] };
type Study = Discovery | DirectionalStudy;
type Entry = { id: string; name: string; trial_count: number; created_at: string };
const familyName: Record<string, string> = { calendar: "定期对冲", payback: "成本过滤", basis: "基差触发" };

export function DiscoveryReports({ refreshToken = 0 }: { refreshToken?: number }) {
  const [items, setItems] = useState<Entry[]>([]), [selected, setSelected] = useState("");
  const [report, setReport] = useState<Study>(), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [retry, setRetry] = useState(0), [detail, setDetail] = useState<Choice>(), [page, setPage] = useState(0);
  const [historyPage, setHistoryPage] = useState(0), [total, setTotal] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    void api<{ items: Entry[]; total: number }>(`/api/v2/research/discoveries?offset=${historyPage * 10}`, { signal: controller.signal }).then(r => {
      if (controller.signal.aborted) return;
      setItems(r.items); setTotal(r.total); setSelected(current => r.items.some(i => i.id === current) ? current : r.items[0]?.id ?? "");
      if (!r.items.length) { setReport(undefined); setLoading(false); }
    }).catch(e => { if (!controller.signal.aborted) { setError(e.message); setLoading(false); } });
    return () => controller.abort();
  }, [retry, historyPage, refreshToken]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController(); setLoading(true); setError(""); setReport(undefined);
    void api<Study>(`/api/v2/research/discoveries/${encodeURIComponent(selected)}`, { signal: controller.signal }).then(r => { if (!controller.signal.aborted) setReport(r); }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selected, retry, refreshToken]);
  async function download() {
    if (!report) return;
    setDownloading(true);
    try {
    const full = report.kind === "DIRECTIONAL" ? await api<Study>(`/api/v2/research/discoveries/${encodeURIComponent(selected)}?include_details=true`) : report;
    const url = URL.createObjectURL(new Blob([JSON.stringify(full, null, 2)], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = `discovery-${selected}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(e instanceof Error ? e.message : "下载失败，请重试"); } finally { setDownloading(false); }
  }
  return <section className="discovery-reports">
    <div className="button-row"><label>研究批次 <select aria-label="研究批次" value={selected} onChange={e => setSelected(e.target.value)}>{items.map(i => <option key={i.id} value={i.id}>{i.name}</option>)}</select></label>{report && <button disabled={downloading} onClick={() => void download()}>{downloading ? "正在准备完整报告…" : "下载挖掘报告"}</button>}</div>
    {total > 10 && <Pagination label="研究批次" page={historyPage} pages={Math.ceil(total / 10)} total={total} onChange={setHistoryPage} busy={loading} />}
    <PageState loading={loading} error={error} onRetry={() => setRetry(n => n + 1)} />
    {!loading && !error && !items.length && <section className="panel research-empty">还没有已完成的策略挖掘报告。</section>}
    {report?.kind === "DIRECTIONAL" ? <DirectionalDiscovery identifier={selected} report={report} /> : report && <>
      <section className="panel research-report"><h2>{report.title}</h2><p>{report.venue} · {report.from_time} — {report.to_time}</p><strong>{report.candidates.length} 套报告收录方案 · 绩效观察</strong><p>绩效指标和成本压力结果仅供参考，不限制部署；账户权限、执行认证和安全检查仍适用。历史批次的收录范围保持不变。</p><p>资金费率结果属于全样本研究，尚无独立留出样本验证。</p><p className="muted-copy">方向策略开发 {report.development_trials} 组 → 冻结 {report.development_finalists} 组验证；资金费率研究 {report.carry_runs} 次。全部测试记录保留。</p></section>
      <section className="panel research-report"><h2>基础成本绩效</h2><div className="research-table-scroll"><table><thead><tr><th>方案</th><th>夏普</th><th>回撤</th><th>命中率</th><th>t 值</th><th>交易</th><th>年化收益</th><th>详情</th></tr></thead><tbody>{report.candidates.map(c => <tr key={c.key}><td>{c.symbol}<br />{c.name}</td><td>{fmt(c.metrics.sharpe_ratio)}</td><td>{fmt(c.metrics.max_drawdown, true)}</td><td>{fmt(c.metrics.win_rate, true)}</td><td>{fmt(c.metrics.t_statistic)}</td><td>{c.metrics.trade_count}</td><td>{fmt(c.annualized_return, true)}</td><td><button onClick={() => { setDetail(c); setPage(0); }}>查看对冲交易</button></td></tr>)}</tbody></table></div><p className="muted-copy">按整个账户计收益：20%资金买现货，其余作现金保证金；等币数做空永续。高夏普不代表高收益。</p></section>
      <section className="panel research-report"><h2>成本压力对照</h2><div className="research-table-scroll"><table><thead><tr><th>方案</th><th>基础夏普 → 双倍成本</th><th>基础命中率 → 双倍成本</th><th>压力观察</th></tr></thead><tbody>{report.candidates.map(c => <tr key={c.key}><td>{c.symbol} · {c.name}</td><td>{fmt(c.metrics.sharpe_ratio)} → {fmt(c.stress_metrics.sharpe_ratio)}</td><td>{fmt(c.metrics.win_rate, true)} → {fmt(c.stress_metrics.win_rate, true)}</td><td>{observationNotes(c.stress_screening)}</td></tr>)}</tbody></table></div><p>基础单边费率：现货10bp、合约5bp、每腿滑点3bp。压力组全部加倍；结果不作为部署限制。</p></section>
      <details className="panel research-report"><summary>全部资金费率测试</summary><div className="research-table-scroll"><table><thead><tr><th>方案</th><th>成本</th><th>夏普</th><th>回撤</th><th>命中率</th><th>t 值</th><th>交易</th><th>记录类型</th><th>观察备注</th></tr></thead><tbody>{report.carry_results.map((c, i) => <tr key={i}><td>{c.symbol} · {familyName[c.family]}</td><td>{c.cost_multiplier}×</td><td>{fmt(c.metrics.sharpe_ratio)}</td><td>{fmt(c.metrics.max_drawdown, true)}</td><td>{fmt(c.metrics.win_rate, true)}</td><td>{fmt(c.metrics.t_statistic)}</td><td>{c.metrics.trade_count}</td><td>{screeningLabel(c.screening)}</td><td>{observationNotes(c.screening)}</td></tr>)}</tbody></table></div></details>
      <details className="panel research-report"><summary>假设、来源与适用限制</summary>{report.notes.map(n => <p key={n}>{n}</p>)}<code className="research-hash">{report.artifact_digest}</code></details>
    </>}
    {detail && <Dialog title={`${detail.symbol} · ${detail.name}`} onClose={() => setDetail(undefined)}><p>持仓周期 {detail.holding_days} 天 · 缺失资金费结算标记价使用8h开盘代理 {detail.funding_mark_proxy_count} 次</p><div className="research-table-scroll"><table><thead><tr><th>年度盯市</th><th>净收益率</th></tr></thead><tbody>{detail.annual.map(y => <tr key={y.year}><td>{y.year}</td><td>{fmt(y.return_fraction, true)}</td></tr>)}</tbody></table></div><div className="research-table-scroll"><table><thead><tr><th>开仓 → 平仓</th><th>现货买入 → 卖出</th><th>合约卖出 → 买回</th><th>币数</th><th>资金费收入</th><th>手续费</th><th>净收益</th></tr></thead><tbody>{detail.trades.slice(page * 10, page * 10 + 10).map(t => <tr key={t.entry_time}><td>{new Date(t.entry_time).toLocaleDateString()} → {new Date(t.exit_time).toLocaleDateString()}</td><td>{fmt(t.spot_entry)} → {fmt(t.spot_exit)}</td><td>{fmt(t.perp_entry)} → {fmt(t.perp_exit)}</td><td>{t.quantity.toPrecision(5)}</td><td>{fmt(t.funding_income)}</td><td>{fmt(t.fees)}</td><td className={t.net_pnl < 0 ? "research-negative" : "research-positive"}>{fmt(t.net_pnl)}</td></tr>)}</tbody></table></div><Pagination label="对冲交易" page={page} pages={Math.ceil(detail.trades.length / 10)} total={detail.trades.length} onChange={setPage} /></Dialog>}
  </section>;
}
