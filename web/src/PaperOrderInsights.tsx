import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { PageState } from "./ui";
import { paperNumber, paperReason, paperStatus, paperTime, type PaperFill, type PaperOrderEvent, type PaperOrder, type PaperPage, type PaperPreview } from "./paperOrdersModel";

export function PaperPreviewPanel({ preview }: { preview: PaperPreview }) {
  const { market, estimate, sizing, checks } = preview;
  return <section className="paper-preview" aria-label="模拟委托预览">
    <header><h3>委托预览 · {market.spec.symbol}</h3><time>盘口 {paperTime(market.book.source_at)}</time></header>
    <dl className="paper-form-grid">
      <div><dt>委托张数 → 币数量</dt><dd>{estimate.requested_contracts} 张 → {paperNumber(sizing.requested_base_quantity)}</dd></div>
      <div><dt>参考名义金额</dt><dd>{paperNumber(sizing.reference_notional)} USDT</dd></div>
      <div><dt>当前盘口预计成交</dt><dd>{estimate.filled_contracts} / {estimate.requested_contracts} 张 · 均价 {paperNumber(estimate.vwap)}</dd></div>
      <div><dt>预计成交手续费</dt><dd>{paperNumber(sizing.estimated_fill_fees)} USDT</dd></div>
      <div><dt>全量委托预占资金 / 手续费</dt><dd>{paperNumber(sizing.required_margin)} / {paperNumber(sizing.required_fees)} USDT</dd></div>
      <div><dt>可用余额 / 当前预算最多张数</dt><dd>{paperNumber(preview.account.balance.available_balance)} USDT / {sizing.max_contracts_by_budget} 张</dd></div>
      <div><dt>预计已成交部分止损损失（含费用）</dt><dd>{paperNumber(sizing.estimated_stop_loss)} USDT</dd></div>
      <div><dt>全量委托风险预占</dt><dd>{paperNumber(sizing.reserved_risk)} USDT</dd></div>
      <div><dt>合约乘数 / 价格步长</dt><dd>{market.spec.multiplier} / {market.spec.price_tick}</dd></div>
      <div><dt>交易所最小 / 最大张数</dt><dd>{market.spec.min_contracts} / {market.spec.max_contracts}</dd></div>
    </dl>
    {!checks.budget_available && <p role="alert" className="error-banner">{checks.reasons.map(paperReason).join("；")}</p>}
    <p className="paper-reason">{paperReason(estimate.reason)}{estimate.remaining_contracts ? ` · 余量 ${estimate.remaining_contracts} 张${estimate.resting ? "将继续挂单" : estimate.remainder_cancelled ? "将取消" : "暂不能成交"}` : ""}</p>
    <p className="paper-muted">仅预估，未锁定资金或盘口；共享盘口可能已被其他模拟委托消耗。提交时重新取数校验。止损损失不是最大亏损承诺，跳空或断网可能导致更差退出。</p>
  </section>;
}

export function PaperOrderTimeline({ order }: { order: PaperOrder }) {
  if (!order.events || !order.fills) return <p role="status" className="paper-muted">正在读取成交与事件…</p>;
  return <section className="paper-order-history" aria-label="委托成交与事件">
    <h3>订单时间线</h3>
    <ol className="paper-timeline">{order.events.map(event => <li key={event.id}>
      <time>{paperTime(event.created_at)}</time><strong>{paperReason(event.event_type)}</strong>
      {typeof event.payload_json.status === "string" && <span>{paperStatus(event.payload_json.status)}</span>}
      {typeof event.payload_json.filled_contracts === "number" && <span>本次成交 {event.payload_json.filled_contracts} 张</span>}
      {typeof event.payload_json.remaining_contracts === "number" && <span>剩余 {event.payload_json.remaining_contracts} 张</span>}
      {typeof event.payload_json.reason === "string" && <small>{paperReason(event.payload_json.reason)}</small>}
    </li>)}</ol>
    {!order.events.length && <p className="paper-muted">暂无事件记录。</p>}
    {order.events_has_more && <p role="status" className="paper-muted">仅展示最早 {order.history_limit ?? 100} 条事件，后续事件未在此列表中完整展示。</p>}
    <h3>逐笔成交 · 当前展示 {order.fills.length} 笔</h3>
    <div className="paper-history-scroll"><table className="paper-fill-table"><thead><tr><th>成交时间</th><th>张数</th><th>价格</th><th>手续费</th><th>毛盈亏</th></tr></thead><tbody>{order.fills.map(fill => <tr key={fill.id}><td>{paperTime(fill.filled_at)}</td><td>{fill.contracts}</td><td>{paperNumber(fill.price)}</td><td>{paperNumber(fill.fee)}</td><td>{paperNumber(fill.realized_pnl)}</td></tr>)}</tbody></table></div>
    {!order.fills.length && <p className="paper-muted">尚无实际模拟成交；委托受理不等于开仓成功。</p>}
    {order.fills_has_more && <p role="status" className="paper-muted">仅展示最早 {order.history_limit ?? 100} 笔成交，不代表完整成交历史；完整账本可在成交明细分页查看。</p>}
  </section>;
}

type HistoryKind = "fills" | "events";
type HistorySlice = { page: number; items: (PaperFill | PaperOrderEvent)[]; has_more: boolean; next_cursor?: string | null };
export function PaperOrderHistoryExplorer({ order }: { order: PaperOrder }) {
  const [pages, setPages] = useState<Partial<Record<HistoryKind, HistorySlice>>>({}), [busy, setBusy] = useState(""), [error, setError] = useState("");
  const cursors = useRef<Record<HistoryKind, (string | undefined)[]>>({ fills: [], events: [] });
  const read = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => read.current?.abort(), []);
  const combined: PaperOrder = { ...order, fills: (pages.fills?.items as PaperFill[] | undefined) ?? order.fills,
    events: (pages.events?.items as PaperOrderEvent[] | undefined) ?? order.events,
    fills_has_more: !order.fills_next_cursor && !pages.fills ? order.fills_has_more : false,
    events_has_more: !order.events_next_cursor && !pages.events ? order.events_has_more : false };
  async function change(kind: HistoryKind, page: number) {
    if (read.current) return;
    if (page === 0) { setPages(values => ({ ...values, [kind]: undefined })); setError(""); return; }
    if (page === 1) cursors.current[kind][1] = order[`${kind}_next_cursor`] ?? undefined;
    const cursor = cursors.current[kind][page]; if (!cursor) return;
    const controller = new AbortController(); read.current = controller; setBusy(kind); setError("");
    try {
      const query = new URLSearchParams({ limit: "100", cursor });
      const result = await api<PaperPage<PaperFill | PaperOrderEvent>>(`/api/v2/paper/orders/${encodeURIComponent(order.id)}/${kind}?${query}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      cursors.current[kind].splice(page+1); if (result.next_cursor) cursors.current[kind][page+1] = result.next_cursor;
      setPages(values => ({ ...values, [kind]: { page, items: result.items, has_more: result.has_more, next_cursor: result.next_cursor } }));
    } catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "历史明细读取失败，请重试当前分页按钮"); }
    finally { if (read.current === controller) { read.current = undefined; setBusy(""); } }
  }
  return <><PageState loading={!!busy} error={error} /><PaperOrderTimeline order={combined} />
    {(["events", "fills"] as const).map(kind => {
      const page = pages[kind]?.page ?? 0, next = pages[kind]?.next_cursor ?? (page === 0 ? order[`${kind}_next_cursor`] : null);
      if (!page && !next) return null;
      return <nav className="pagination" aria-label={kind === "events" ? "订单事件分页" : "订单成交分页"} key={kind}><span>{kind === "events" ? "事件" : "成交"}第 {page+1} 页 · 每页最多 100 条</span><div><button type="button" disabled={!!busy || page === 0} onClick={() => void change(kind, page-1)}>上一页</button><button type="button" disabled={!!busy || !next} onClick={() => void change(kind, page+1)}>下一页</button></div></nav>;
    })}
  </>;
}

export function PaperDecisionPanel({ order }: { order: PaperOrder }) {
  const [showChart, setShowChart] = useState(false), [patternIndex, setPatternIndex] = useState(0), [imageFailed, setImageFailed] = useState(false);
  const snapshot = order.decision_snapshot;
  if (!snapshot) return <p className="paper-muted">{order.decision_snapshot_id ? "正在读取原始决策快照…" : "未关联分析依据：保留成交事实，不将订单归因于某个形态或策略。"}</p>;
  const evidence = snapshot.evidence_json, server = evidence.server_evidence, client = evidence.client_analysis;
  return <section className="paper-order-history" aria-label="原始开单依据"><h3>原始开单依据</h3>
    <dl className="paper-proof"><div><dt>分析时间 / 固化时间</dt><dd>{paperTime(evidence.observed_at)} / {paperTime(evidence.captured_at)}</dd></div>
      <div><dt>已冻结行情</dt><dd>{server.bar_count} 根 · {evidence.timeframe} · 最后收盘 {paperTime(server.last_closed_candle_at)}</dd></div>
      <div><dt>冻结指标末值（窗口内计算）</dt><dd>{Object.entries(server.indicators).map(([name, series]) => `${name} ${paperNumber(series.at(-1)?.value)}`).join(" · ")}</dd></div></dl>
    {server.data_status !== "AVAILABLE" && <p className="paper-reason">当时行情{server.data_status === "NO_CACHED_CANDLES" ? "没有可用缓存" : "存在缺口"}，未补造缺失数据。</p>}
    {server.window_limited && <p className="paper-muted">保存窗口有上限；原始形态超出窗口的部分不会移位补画。指标预热受该窗口限制。</p>}
    <details><summary>分析摘要{client.analysis_model ? ` · ${client.analysis_model}` : ""}</summary><p className="paper-decision-rationale">{client.rationale}</p>
      <p className="paper-muted">这是客户端提供的分析，不代表平台已验证其结论。</p>
      {!!client.coinglass_claims.length && <><h4>分析引用的 Coinglass 数据</h4><ul>{client.coinglass_claims.map((claim, index) => <li key={index}>{claim.metric}: {paperNumber(claim.value)} {claim.unit} · {claim.market_scope} · {paperTime(claim.source_at)}</li>)}</ul><p className="paper-muted">以上引用由客户端报告，未冒充平台已核验的上游响应。</p></>}
    </details>
    {!!server.pattern_events.length && <label className="paper-chart-selector">已保存形态<select value={patternIndex} onChange={event => { setPatternIndex(Number(event.target.value)); setImageFailed(false); }}>{server.pattern_events.map((event, index) => <option value={index} key={index}>{event.record.latest?.name ?? event.record.latest?.pattern ?? "归档形态"} · {event.phase} · {event.source_mode === "RECORDED" ? "实时归档" : "历史回放"}</option>)}</select></label>}
    {server.bar_count > 0 && <button type="button" onClick={() => { setShowChart(value => !value); setImageFailed(false); }}>{showChart ? "收起开单原图" : "查看开单分析原图"}</button>}
    {showChart && (imageFailed ? <p role="alert" className="paper-reason">原图暂时加载失败，请重试；不会使用最新行情代替。</p> : <img className="paper-decision-chart" src={`/api/v2/paper/orders/${encodeURIComponent(order.id)}/chart?pattern_index=${patternIndex}`} alt="不可变开单行情及当时归档形态" onError={() => setImageFailed(true)} />)}
    <details><summary>快照校验信息</summary><dl className="paper-proof"><div><dt>快照 ID</dt><dd>{snapshot.id}</dd></div><div><dt>证据 SHA-256</dt><dd>{snapshot.evidence_digest}</dd></div></dl></details>
  </section>;
}
