import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, jsonBody } from "./api";
import type { Session } from "./types";
import { Dialog, PageState, Pagination } from "./ui";
import { useResearchResource } from "./useResearchResource";
import { directionLabel, researchDate, researchNumber } from "./researchV2Model";
import { stateLabel } from "./researchModel";
import { portfolioActive, portfolioCurveSegments, portfolioReason, portfolioRequest, type PortfolioCurve, type PortfolioDecision, type PortfolioPage, type PortfolioReport, type PortfolioRun, type PortfolioSource, type PortfolioTrade } from "./portfolioResearchModel";
import "./portfolioResearch.css";

const selectedFromUrl = () => new URLSearchParams(location.search).get("portfolio_run") ?? "";
export function PortfolioResearchWorkspace({ session }: { session: Session }) {
  const [runId, setRunId] = useState(selectedFromUrl), [page, setPage] = useState(0), [refresh, setRefresh] = useState(0);
  const [builder, setBuilder] = useState(false), [busy, setBusy] = useState(false);
  const list = useResearchResource<PortfolioPage<PortfolioRun>>(`/api/v2/research/portfolios?limit=20&offset=${page*20}`, refresh);
  const run = useResearchResource<PortfolioRun>(runId ? `/api/v2/research/portfolios/${encodeURIComponent(runId)}` : null, refresh);
  const active = portfolioActive(run.data?.state) || list.data?.items.some(item => portfolioActive(item.state));
  useEffect(() => { if (!runId && list.data?.items[0]) setRunId(list.data.items[0].id); }, [list.data, runId]);
  useEffect(() => { if (!active) return; const timer = window.setInterval(() => { if (!document.hidden) setRefresh(value => value+1); }, 4000); return () => window.clearInterval(timer); }, [active]);
  useEffect(() => { const change = () => setRunId(selectedFromUrl()); window.addEventListener("popstate", change); return () => window.removeEventListener("popstate", change); }, []);
  useEffect(() => { if (runId) { const url = new URL(location.href); url.searchParams.set("portfolio_run", runId); history.replaceState({}, "", url); } }, [runId]);
  return <section className="portfolio-workspace" aria-label="共享资金组合回测">
    <div className="structure-toolbar"><div><h2>共享资金组合回测</h2><small className="research-context-note">多币种共享一份有限资金 · 不改写单形态统计</small></div><div className="button-row"><button disabled={list.loading || run.loading} onClick={() => setRefresh(value => value+1)}>刷新任务</button><button className="primary" disabled={session.user.role === "VIEWER"} onClick={() => setBuilder(true)}>＋ 新建组合回测</button></div></div>
    <section className="panel research-report"><PageState loading={list.loading} error={list.error} onRetry={list.retry} />
      {list.data && <><label className="portfolio-run-selector">研究记录<select value={list.data.items.some(item => item.id === runId) ? runId : ""} onChange={event => setRunId(event.target.value)}><option value="">选择当前页报告</option>{list.data.items.map(item => <option key={item.id} value={item.id}>{stateLabel[item.state] ?? item.state} · {item.source_run_ids.length} 个来源 · {researchDate(item.created_at)}</option>)}</select></label>
        {!list.data.items.length && <p className="research-context-note">暂无组合研究。选择已完成的同源形态回测，比较同时出现的信号在共享资金约束下的实际分配。</p>}
        <Pagination label="组合研究" page={page} total={list.data.total} hasNext={list.data.has_more} busy={list.loading} onChange={setPage} /></>}
    </section>
    <PageState loading={run.loading && !run.data} error={run.error} onRetry={run.retry} />
    {run.data && <>
      {portfolioActive(run.data.state) && <section className="panel" role="status"><h3>{stateLabel[run.data.state] ?? "后台处理中"}</h3><p>处理 {run.data.progress?.processed ?? 0} / {run.data.progress?.total ?? "待统计"} · {run.data.progress?.phase ?? "等待调度"}</p><small>任务已保存，离开页面不会停止计算。</small></section>}
      {run.data.state === "FAILED" && <div role="alert" className="error-banner">{run.data.error ?? "组合研究未完成，来源回测记录仍保留。"}</div>}
      {run.data.report && <PortfolioReportView key={run.data.id} run={run.data} report={run.data.report} />}
      {run.data.state === "SUCCEEDED" && !run.data.report && <PageState error="报告暂不可读取，请重新加载。" onRetry={run.retry} />}
    </>}
    {builder && <Dialog title="新建共享资金回测" busy={busy} onClose={() => setBuilder(false)}><PortfolioBuilder onBusy={setBusy} onCreated={created => { setBuilder(false); setRunId(created.id); setPage(0); setRefresh(value => value+1); }} /></Dialog>}
  </section>;
}

export function PortfolioBuilder({ onBusy, onCreated }: { onBusy: (value: boolean) => void; onCreated: (run: PortfolioRun) => void }) {
  const [page, setPage] = useState(0), [selected, setSelected] = useState<PortfolioSource[]>([]), [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [uncertain, setUncertain] = useState(false);
  const lock = useRef(false);
  const source = useResearchResource<PortfolioPage<PortfolioSource>>(`/api/v2/research/backtests?engine_mode=STRUCTURE&limit=20&offset=${page*20}`);
  const available = (source.data?.items ?? []).filter(item => item.state === "SUCCEEDED" && `${item.symbol} ${item.timeframe}`.toLowerCase().includes(query.toLowerCase()));
  function toggle(item: PortfolioSource) { setSelected(values => values.some(value => value.id === item.id) ? values.filter(value => value.id !== item.id) : values.length < 8 ? [...values, item] : values); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (lock.current || uncertain) return;
    let body; try { body = portfolioRequest(new FormData(event.currentTarget), selected); } catch (cause) { setError(cause instanceof Error ? cause.message : "请检查参数"); return; }
    lock.current = true; setBusy(true); onBusy(true); setError("");
    try { onCreated(await api<PortfolioRun>("/api/v2/research/portfolios", { method: "POST", body: jsonBody(body) })); }
    catch (cause) { const unknown = !(cause instanceof ApiError) || cause.status >= 500 || cause.status === 0 || cause.code === "REQUEST_TIMEOUT"; setUncertain(unknown); setError(unknown ? "提交结果暂未确认，请关闭弹窗并刷新任务列表核对，避免重复创建计算任务。" : cause.message); }
    finally { lock.current = false; setBusy(false); onBusy(false); }
  }
  return <form className="portfolio-builder" onSubmit={submit}><fieldset disabled={busy || uncertain}>
    <h3>选择历史来源 · {selected.length} / 8</h3><p className="research-context-note">按币种、周期和研究时间选择；不要只挑盈利报告。来源保持原规则，不重新优化已完成结果。</p>
    <PageState loading={source.loading} error={source.error} onRetry={source.retry} />
    <label>筛选当前页币种 / 周期<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="例如 SOL 或 1h" /></label>
    <div className="portfolio-source-list">{available.map(item => <label key={item.id}><input type="checkbox" checked={selected.some(value => value.id === item.id)} disabled={selected.length >= 8 && !selected.some(value => value.id === item.id)} onChange={() => toggle(item)} /><span><strong>{item.symbol} · {item.timeframe}</strong><small>{researchDate(item.created_at)}</small></span></label>)}</div>
    {!available.length && !source.loading && <p className="research-context-note">当前页没有匹配的已完成报告；可以翻页或先创建同源形态回测。</p>}
    {source.data && <Pagination label="来源回测" page={page} total={source.data.total} hasNext={source.data.has_more ?? (page+1)*20 < source.data.total} busy={source.loading} onChange={setPage} />}
    {!!selected.length && <div className="portfolio-selection" aria-label="已选来源">{selected.map(item => <button type="button" key={item.id} onClick={() => toggle(item)} aria-label={`移除 ${item.symbol} ${item.timeframe}`}>{item.symbol} {item.timeframe} ×</button>)}</div>}
    <div className="portfolio-form-grid">
      <label>初始资金（USDT）<input name="initial_equity" type="number" min="100" max="100000000" step="any" defaultValue="10000" required /></label>
      <label>单笔含成本风险预算（%）<input name="risk_percent" type="number" min="0.1" max="3" step="any" defaultValue="1" required /></label>
      <label>同时持仓上限<input name="max_positions" type="number" min="1" max="20" step="1" defaultValue="5" required /></label>
      <label>总名义敞口上限（%）<input name="max_exposure_percent" type="number" min="5" max="100" step="any" defaultValue="100" required /></label>
      <label>单仓名义金额上限（%）<input name="per_position_percent" type="number" min="1" max="100" step="any" defaultValue="20" required /></label>
      <label>开始时间（选填，本地时间）<input name="from_time" type="datetime-local" /></label><label>结束时间（选填，本地时间）<input name="to_time" type="datetime-local" /></label>
    </div>
    <p className="research-context-note">1× 名义资金模型，使用冻结的历史 OHLC 成交规则。预算计入配置的费用、止损滑点和持仓期资金成本；跳空与额外延迟仍可能超出预算。</p>
    <button className="primary" disabled={!selected.length || busy || uncertain}>{busy ? "提交中…" : "创建后台组合回测"}</button>
  </fieldset>{error && <p className="error-banner" role="alert">{error}</p>}</form>;
}

export function PortfolioReportView({ run, report }: { run: PortfolioRun; report: PortfolioReport }) {
  const [tab, setTab] = useState<"trades" | "decisions" | "curve">("trades"), [page, setPage] = useState(0);
  const records = useResearchResource<PortfolioPage<PortfolioTrade | PortfolioDecision | PortfolioCurve>>(`/api/v2/research/portfolios/${encodeURIComponent(run.id)}/${tab}?limit=25&offset=${page*25}`);
  const metrics = report.metrics;
  return <>
    <div className="research-result-heading"><div><h2>组合账户报告</h2><p>{researchDate(report.range.from)} — {researchDate(report.range.to)}</p></div><small>{report.engine_version}</small></div>
    <div className="research-metrics">{[
      ["账户净收益", researchNumber(metrics.net_pnl)], ["收益率", researchNumber(metrics.total_return, true)], ["最大回撤", researchNumber(metrics.max_drawdown, true)],
      ["交易胜率", researchNumber(metrics.win_rate, true)], ["夏普", researchNumber(metrics.sharpe_ratio)], ["t 统计量", researchNumber(metrics.t_statistic)],
      ["已完成交易", metrics.trade_count], ["期末现金 / 占用", `${researchNumber(report.final_cash)} / ${researchNumber(report.final_reserved)}`],
    ].map(([label, value]) => <div className="panel" key={String(label)}><span>{label}</span><b>{value}</b></div>)}</div>
    {!report.metrics_available && <p role="status" className="research-warning">历史估值数据不足；缺少依据的整体指标显示未知，不按零盈亏结算。</p>}
    <section className="panel research-report"><h3>资金分配与未成交原因</h3><p>来源候选 {report.source_case_count} · 接受 {report.accepted_count} · 未纳入 {report.rejected_count} · 期末未结算仓位 {report.open_position_count}</p>
      <div className="portfolio-rejections">{Object.entries(report.rejection_reasons).map(([reason, count]) => <span key={reason}>{portfolioReason(reason)}：{count}</span>)}</div>
      <small className="research-context-note">初始资金 {researchNumber(report.initial_equity)} USDT · 手续费 {researchNumber(metrics.fees)} · 资金成本 {researchNumber(metrics.funding)}；被资金或执行条件拒绝的候选仍保留。</small>
    </section>
    {!!report.open_positions?.length && <details className="panel research-report"><summary>期末未平仓 · {report.open_positions.length} 个</summary><p className="research-context-note">保留未结束事实，不补写平仓时间或已实现盈亏。</p><div className="research-table-scroll"><table><thead><tr>{["币种 / 形态", "入场 → 退出", "方向 / 状态", "名义金额 / 数量", "入场 → 出场价", "净损益", "费用 / 资金费"].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{report.open_positions.map((trade, index) => <PortfolioTradeRow key={index} trade={trade} />)}</tbody></table></div></details>}
    <section className="panel research-report"><nav className="research-tabs" aria-label="组合回测明细">{([["trades", "交易与资金分配"], ["decisions", "全部接受 / 拒绝决策"], ["curve", "资金与回撤切片"]] as const).map(([kind, label]) => <button type="button" key={kind} aria-pressed={tab === kind} onClick={() => { setTab(kind); setPage(0); }}>{label}</button>)}</nav>
      <PageState loading={records.loading} error={records.error} onRetry={records.retry} />
      {records.data && <>
        {tab === "curve" && <PortfolioCurvePanel rows={records.data.items as PortfolioCurve[]} />}
        <div className="research-table-scroll"><table><thead><tr>{(tab === "trades" ? ["币种 / 形态", "入场 → 退出", "方向 / 状态", "名义金额 / 数量", "入场 → 出场价", "净损益", "费用 / 资金费"] : tab === "decisions" ? ["观察时间", "币种 / 形态", "决策", "原因", "来源 / 候选"] : ["时间", "现金", "资金占用", "净值", "回撤", "持仓数"]).map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>
          {records.data.items.map((item, index) => tab === "trades" ? <PortfolioTradeRow key={index} trade={item as PortfolioTrade} /> : tab === "decisions" ? <PortfolioDecisionRow key={index} decision={item as PortfolioDecision} /> : <PortfolioCurveRow key={index} row={item as PortfolioCurve} />)}
        </tbody></table></div>
        {!records.data.items.length && <p className="research-context-note">当前分类下暂无记录。</p>}
        <Pagination label="组合回测明细" page={page} total={records.data.total} hasNext={records.data.has_more} busy={records.loading} onChange={setPage} />
      </>}
    </section>
    <details className="panel research-report"><summary>冻结参数、来源与模型假设</summary><p>风险 {researchNumber(run.policy.risk_fraction, true)} · 最多 {run.policy.max_positions} 仓 · 总敞口 {researchNumber(run.policy.max_exposure_fraction, true)} · 单仓上限 {researchNumber(run.policy.per_position_fraction, true)}</p>
      <ul>{run.source_run_ids.map(id => <li key={id}><a href={`/strategies/backtests?view=structure&structure_run=${encodeURIComponent(id)}`}>查看来源 {id}</a></li>)}</ul><ul>{report.assumptions.map((assumption, index) => <li key={index}>{assumption}</li>)}</ul><code className="research-hash">{report.artifact_digest}</code>
    </details>
  </>;
}

export function PortfolioTradeRow({ trade }: { trade: PortfolioTrade }) {
  return <tr><td>{trade.symbol}<small>{trade.pattern}</small></td><td>{researchDate(trade.entry_time)}<br />{researchDate(trade.exit_time)}{trade.settled_at && trade.settled_at !== trade.exit_time && <small>资金可用：{researchDate(trade.settled_at)}</small>}</td><td>{directionLabel(trade.side)} · {trade.state}</td><td>{researchNumber(trade.notional)} / {researchNumber(trade.quantity)}</td><td>{researchNumber(trade.entry_price)} → {researchNumber(trade.exit_price)}</td><td>{researchNumber(trade.net_pnl)}</td><td>{researchNumber(trade.fees)} / {researchNumber(trade.funding)}</td></tr>;
}
function PortfolioDecisionRow({ decision }: { decision: PortfolioDecision }) { return <tr><td>{researchDate(decision.observed_time)}</td><td>{decision.symbol}<small>{decision.pattern}</small></td><td>{decision.status === "ACCEPTED" ? "接受" : "拒绝并保留"}</td><td>{portfolioReason(decision.reason)}</td><td><a href={`/strategies/backtests?view=structure&structure_run=${encodeURIComponent(decision.source_run_id)}`}>来源回测</a><small>{decision.candidate_id}</small></td></tr>; }
function PortfolioCurveRow({ row }: { row: PortfolioCurve }) { return <tr><td>{researchDate(row.time)}</td><td>{researchNumber(row.cash)}</td><td>{researchNumber(row.reserved)}</td><td>{researchNumber(row.equity)}</td><td>{researchNumber(row.drawdown, true)}</td><td>{row.positions}</td></tr>; }
export function PortfolioCurvePanel({ rows }: { rows: PortfolioCurve[] }) { return <div className="portfolio-curve-grid">{(["equity", "drawdown"] as const).map(kind => <figure key={kind}><figcaption>{kind === "equity" ? "资金净值" : "峰值回撤"} · 当前页切片</figcaption><svg viewBox="0 0 800 180" role="img" aria-label={kind === "equity" ? "当前页净值曲线" : "当前页回撤曲线"}><line x1="12" x2="788" y1="168" y2="168" stroke="#354258" />{portfolioCurveSegments(rows, kind).map((points, index) => <polyline key={index} fill="none" stroke={kind === "equity" ? "#6c9dff" : "#ef5c75"} strokeWidth="2" points={points} />)}</svg><small>{researchDate(rows[0]?.time)} — {researchDate(rows.at(-1)?.time)} · 缺失估值处不连线</small></figure>)}<p className="research-context-note">图只展示当前页时间片，不是完整资金曲线；上方收益与回撤指标按全量记录计算。</p></div>; }
