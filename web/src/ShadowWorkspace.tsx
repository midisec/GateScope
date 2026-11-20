import { useEffect, useState } from "react";
import { Dialog, PageState, Pagination } from "./ui";
import { InstrumentPicker } from "./InstrumentPicker";
import { useResearchResource } from "./useResearchResource";
import { cohortLabels, directionLabel, reasonLabel, researchDate, researchNumber, shadowEventLabels, shadowQuery, shadowStates, type ShadowDetail, type ShadowSummary, type ShadowTrade } from "./researchV2Model";
import { environmentDimensions, environmentValue, structureCaseStates } from "./structureResearchModel";
import "./research.css";
import "./researchV2.css";

const initial = () => {
  const query = new URLSearchParams(location.search), requested = Number(query.get("page") ?? 0);
  return { cohort: ["SIGNAL", "DELIVERED"].includes(query.get("cohort") ?? "") ? query.get("cohort")! : "", state: query.get("state") ?? "", instrument: query.get("instrument") ?? "", page: Number.isSafeInteger(requested) && requested >= 0 ? requested : 0, selected: query.get("trade") ?? "" };
};

export function ShadowWorkspace() {
  const [filters, setFilters] = useState(initial), [refresh, setRefresh] = useState(0);
  const list = useResearchResource<{ items: ShadowTrade[]; total: number; has_more: boolean; enabled_at: string | null }>(`/api/v2/research/shadow-trades?${shadowQuery(filters, filters.page)}`, refresh);
  const summary = useResearchResource<ShadowSummary>(`/api/v2/research/shadow-trades/summary?${shadowQuery({ cohort: filters.cohort, instrument: filters.instrument })}`, refresh);
  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden) setRefresh(n => n + 1); }, 30000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => { const back = () => setFilters(initial()); window.addEventListener("popstate", back); return () => window.removeEventListener("popstate", back); }, []);
  useEffect(() => {
    const url = new URL(location.href);
    for (const [key, value] of Object.entries({ cohort: filters.cohort, state: filters.state, instrument: filters.instrument, page: filters.page ? String(filters.page) : "", trade: filters.selected })) {
      if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    history.replaceState({}, "", url);
  }, [filters]);
  useEffect(() => { if (list.data && filters.page > 0 && filters.page * 20 >= list.data.total) setFilters(f => ({ ...f, page: Math.max(0, Math.ceil(list.data!.total / 20) - 1) })); }, [list.data, filters.page]);
  const change = (values: Partial<typeof filters>) => setFilters(current => ({ ...current, ...values, page: 0, selected: "" }));
  return <main className="settings-page research-page shadow-page">
    <section className="page-title"><div><h1>通知影子账本</h1><span className="simulation-badge">仅模拟 · 不下单</span></div><button disabled={list.loading || summary.loading} onClick={() => setRefresh(n => n + 1)}>刷新记录</button></section>
    <p className="research-context-note">分别观察信号出现后、通知成功送达后的表现；两组可能来自同一信号，不合并计算胜率。仅记录启用后的事件。</p>
    <p className="research-context-note">K 线开盘模拟；净收益已扣滑点、手续费和资金费。止损按冻结价格触及处理，具体成本参数保存在每条记录。</p>
    <section className="panel research-v2-filters" aria-label="影子记录筛选">
      <label>观察组<select aria-label="观察组" value={filters.cohort} onChange={e => change({ cohort: e.target.value })}><option value="">两组分别查看</option><option value="SIGNAL">全部信号观察组</option><option value="DELIVERED">通知送达观察组</option></select></label>
      <label>记录状态<select aria-label="记录状态" value={filters.state} onChange={e => change({ state: e.target.value })}><option value="">全部状态</option>{Object.entries(shadowStates).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <InstrumentPicker value={filters.instrument} onChange={instrument => change({ instrument: instrument.id })} />
      {filters.instrument && <button onClick={() => change({ instrument: "" })}>全部币种</button>}
    </section>
    {summary.data?.worker && <ShadowWorkerStatus worker={summary.data.worker} />}
    <PageState loading={summary.loading} error={summary.error} onRetry={summary.retry} />
    {summary.data && <><div className="shadow-cohort-grid">{summary.data.cohorts.map(group => <ShadowCohortCard key={group.cohort} group={group} />)}</div><small className="research-context-note">统计按观察组与币种筛选，不随下方状态筛选变化。启用时间：{researchDate(summary.data.enabled_at)}</small></>}
    <section className="panel research-report"><header><h2>观察记录</h2><span className="muted-copy">保留未入场、过期及失败事件</span></header><PageState loading={list.loading} error={list.error} onRetry={list.retry} />
      {list.data && <><div className="research-table-scroll"><table className="shadow-table"><thead><tr><th>信号 / 形态</th><th>观察组</th><th>状态</th><th>模拟方向</th><th>入场 → 退出</th><th>模拟净收益</th><th>详情</th></tr></thead><tbody>{list.data.items.map(trade => <tr key={trade.id}><td><strong>{trade.symbol} · {trade.timeframe}</strong><small>{trade.pattern_name} · {researchDate(trade.signal_at)}</small></td><td>{cohortLabels[trade.cohort]}</td><td>{shadowStates[trade.state]}{trade.reason_code && <small>{reasonLabel(trade.reason_code)}</small>}</td><td>{directionLabel(trade.side)}</td><td>{researchNumber(trade.entry_price)} → {researchNumber(trade.exit_price)}</td><td className={(trade.net_return ?? 0) < 0 ? "research-negative" : ""}>{researchNumber(trade.net_return, true)}</td><td><button onClick={() => setFilters(f => ({ ...f, selected: trade.id }))} aria-label={`查看 ${trade.symbol} ${trade.pattern_name} ${cohortLabels[trade.cohort]}`}>查看记录</button></td></tr>)}</tbody></table></div>
        {!list.data.items.length && <div className="research-empty"><h3>暂无匹配的影子记录</h3><p>仅记录启用后的事件；暂无记录不等于没有信号，请同时查看后台观察状态。</p><a href="/strategies/alerts">查看形态通知配置</a></div>}
        <Pagination label="影子记录" page={filters.page} pages={Math.max(1, Math.ceil(list.data.total / 20))} total={list.data.total} busy={list.loading} onChange={page => setFilters(f => ({ ...f, page }))} /></>}
    </section>
    {summary.data && <ShadowEnvironment summary={summary.data} />}
    {filters.selected && <ShadowDetailDialog key={filters.selected} id={filters.selected} onClose={() => setFilters(f => ({ ...f, selected: "" }))} />}
  </main>;
}

export function ShadowWorkerStatus({ worker }: { worker: NonNullable<ShadowSummary["worker"]> }) {
  const delayed = ["DEGRADED", "STALE", "STOPPED"].includes(worker.state);
  return <p className={delayed ? "research-warning" : "research-context-note"} role="status">后台观察：{({ NOT_STARTED: "尚未启动", HEALTHY: "运行中", DEGRADED: "部分异常", STALE: "更新延迟", STOPPED: "已停止" } as Record<string, string>)[worker.state] ?? "状态未知"} · 最近运行 {researchDate(worker.last_cycle_at)}{delayed ? " · 当前记录可能尚未更新" : ""}</p>;
}

export function ShadowCohortCard({ group }: { group: ShadowSummary["cohorts"][number] }) {
  return <section className="panel shadow-cohort"><header><h2>{cohortLabels[group.cohort]}</h2><span>{group.total} 条观察记录</span></header><dl className="research-facts"><div><dt>模拟净胜率</dt><dd>{group.win_rate === null ? group.closed ? "样本不足" : "等待已结束样本" : researchNumber(group.win_rate, true)}</dd></div><div><dt>已结束 / 盈利</dt><dd>{group.closed} / {group.wins}</dd></div><div><dt>平均净收益</dt><dd>{researchNumber(group.mean_net_return, true)}</dd></div><div><dt>平均成本</dt><dd>{researchNumber(group.mean_costs_return, true)}</dd></div></dl><small className="research-context-note">{group.closed < 20 ? "结算样本不足 20；仅展示已有记录，尚不足以判断稳定性。" : "历史模拟描述；同一行情可能产生重叠观察，不代表独立胜率。"}</small></section>;
}

export function ShadowEnvironment({ summary }: { summary: ShadowSummary }) {
  const [dimension, setDimension] = useState("trend");
  const groups = (summary.by_context ?? []).filter(group => group.dimension === dimension);
  return <section className="panel research-report"><header><h2>信号发生时的市场环境</h2><select aria-label="影子环境维度" value={dimension} onChange={event => setDimension(event.target.value)}>{Object.entries(environmentDimensions).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></header><div className="research-table-scroll"><table><thead><tr><th>环境</th><th>观察组</th><th>已结束 / 记录</th><th>模拟净胜率</th><th>平均净收益</th></tr></thead><tbody>{groups.map(group => <tr key={`${group.cohort}:${group.bucket}`}><td>{environmentValue(group.bucket)}</td><td>{cohortLabels[group.cohort]}</td><td>{group.closed} / {group.total}</td><td>{group.win_rate === null ? "样本未就绪" : researchNumber(group.win_rate, true)}</td><td>{researchNumber(group.mean_net_return, true)}</td></tr>)}</tbody></table></div>{!groups.length && <p className="muted-copy">该维度尚无记录。</p>}<small className="research-context-note">仅描述已有模拟记录；两组与不同维度不可加总，不代表独立样本。未知环境同样保留。</small></section>;
}

export function ShadowDetailDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const [page, setPage] = useState(0);
  const detail = useResearchResource<ShadowDetail>(`/api/v2/research/shadow-trades/${encodeURIComponent(id)}?event_limit=20&event_offset=${page * 20}`);
  return <Dialog title="模拟观察记录 · 原始计划" onClose={onClose}><div className="button-row"><button disabled={detail.loading} onClick={detail.retry}>刷新最新状态</button></div><PageState loading={detail.loading} error={detail.error} onRetry={detail.retry} />{detail.data && <><ShadowDetailView trade={detail.data} /><Pagination label="影子事件" page={page} pages={Math.max(1, Math.ceil(detail.data.events_total / 20))} total={detail.data.events_total} busy={detail.loading} onChange={setPage} /></>}</Dialog>;
}

export function ShadowDetailView({ trade }: { trade: ShadowDetail }) {
  return <div className="shadow-detail"><div className="research-result-heading"><div><h2>{trade.symbol} · {trade.timeframe} · {trade.pattern_name}</h2><p>{cohortLabels[trade.cohort]} · {shadowStates[trade.state]} · {directionLabel(trade.side)}</p></div><span className="simulation-badge">模拟记录，不是真实订单</span></div>
    <dl className="research-facts"><div><dt>信号时间</dt><dd>{researchDate(trade.signal_at)}</dd></div><div><dt>通知送达</dt><dd>{researchDate(trade.delivered_at)}</dd></div><div><dt>模拟入场时间</dt><dd>{researchDate(trade.entry_at)}</dd></div><div><dt>模拟结束时间</dt><dd>{researchDate(trade.exit_at)}</dd></div><div><dt>模拟入场 → 退出</dt><dd>{researchNumber(trade.entry_price)} → {researchNumber(trade.exit_price)}</dd></div><div><dt>冻结止损 / 目标</dt><dd>{researchNumber(trade.stop_price)} / {researchNumber(trade.target_price)}</dd></div><div><dt>价格毛收益</dt><dd>{researchNumber(trade.raw_gross_return, true)}</dd></div><div><dt>总成本（含滑点）</dt><dd>{researchNumber(trade.costs_return, true)}</dd></div><div><dt>模拟净收益</dt><dd>{researchNumber(trade.net_return, true)}</dd></div><div><dt>滑点成本</dt><dd>{researchNumber(trade.slippage_return, true)}</dd></div><div><dt>手续费 / 资金费</dt><dd>{researchNumber(trade.result?.fees, true)} / {researchNumber(trade.result?.funding, true)}</dd></div><div><dt>扣滑点收益</dt><dd>{researchNumber(trade.gross_return, true)}</dd></div></dl>
    {trade.reason_code && <p className="research-context-note">当前结果：{reasonLabel(trade.reason_code)}</p>}
    <section><h3>冻结入场计划</h3><dl className="research-facts"><div><dt>事件参考价</dt><dd>{researchNumber(trade.plan.reference_price)}</dd></div><div><dt>原始事件时间</dt><dd>{researchDate(trade.plan.signal_time)}</dd></div><div><dt>原始计划截止</dt><dd>{researchDate(trade.plan.deadline_time)}</dd></div><div><dt>执行观察周期</dt><dd>{trade.execution_timeframe ?? trade.timeframe}</dd></div></dl></section>
    <section><h3>事件时间线 · 共 {trade.events_total} 条</h3>{trade.events.length ? <ol className="research-timeline">{trade.events.map(event => <li key={event.sequence}><span>{researchDate(event.observed_at)}</span><strong>{shadowEventLabels[event.event_type] ?? reasonLabel(event.event_type)}{event.event_type === "STATE_CHANGED" && typeof event.payload.state === "string" ? ` · ${structureCaseStates[event.payload.state] ?? event.payload.state}` : ""}</strong><details><summary>事件依据</summary><JsonEvidence value={event.payload} /></details></li>)}</ol> : <p className="muted-copy">暂无生命周期事件</p>}</section>
    <section><h3>模拟成交与成本假设</h3><dl className="research-facts"><div><dt>单边手续费</dt><dd>{researchNumber(trade.policy.fee_bps)} bp</dd></div><div><dt>单边滑点</dt><dd>{researchNumber(trade.policy.slippage_bps)} bp</dd></div><div><dt>每 8 小时资金费估算</dt><dd>{researchNumber(trade.policy.funding_bps_per_8h)} bp</dd></div><div><dt>最长持有</dt><dd>{researchNumber(trade.policy.max_holding_bars)} 根</dd></div><div><dt>止损模型</dt><dd>{trade.policy.stop_mode === "PRICE_TOUCH" ? "触价止损" : String(trade.policy.stop_mode ?? "—")}</dd></div><div><dt>同根触及止盈止损</dt><dd>{trade.result?.ambiguous === true ? "存在顺序歧义，保守处理" : "—"}</dd></div></dl><small className="research-context-note">K 线开盘模拟成交；使用固定滑点与资金费估算。收益和成本均为比例，不是美元金额。</small></section>
    <details><summary>查看冻结计划</summary><JsonEvidence value={trade.plan} /></details><details><summary>查看原始形态证据</summary><JsonEvidence value={trade.source_event} /></details><details><summary>完整成本参数与结算依据</summary><JsonEvidence value={{ policy: trade.policy, result: trade.result }} /></details>
    <p className="research-context-note">历史计划不会随走势改写；止损与目标为模拟假设，不代表已在交易所挂单。</p><code className="research-hash">{trade.engine_version} · {trade.alert_event_id}</code>
  </div>;
}

export function JsonEvidence({ value }: { value: unknown }) { return <pre className="research-json">{JSON.stringify(value, null, 2)}</pre>; }
