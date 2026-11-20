import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, jsonBody } from "./api";
import { InstrumentPicker, type InstrumentChoice } from "./InstrumentPicker";
import { Dialog, PageState, Pagination } from "./ui";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";
import type { PaperAccount } from "./types";
import { PaperDecisionPanel, PaperOrderHistoryExplorer, PaperPreviewPanel } from "./PaperOrderInsights";
import { cancellablePaperOrder, paperCursorListPath, paperEmptyPage, paperIntentKey, paperListPath, paperMutationNotice, paperNumber, paperOrderBody, paperReason, paperStatus, paperTime, persistPaperAction, restorePaperAction,
  type PaperAction, type PaperFill, type PaperMutation, type PaperOrder, type PaperPage, type PaperPosition, type PaperPreview, type PaperSnapshot } from "./paperOrdersModel";
import "./paperOrders.css";

export function PaperBalance({ snapshot }: { snapshot: PaperSnapshot }) {
  const balance = snapshot.balance;
  return <div className="paper-account-summary"><dl className="paper-balance-grid">
    {[["现金余额", balance.cash_balance], ["可用余额", balance.available_balance], ["挂单冻结", balance.order_reserved_margin],
      ["冻结手续费", balance.order_reserved_fees], ["持仓保证金", balance.position_margin], ["已实现净变动", balance.realized_pnl],
      ["浮动盈亏", balance.unrealized_pnl], ["标记净值", balance.equity]].map(([label, value]) => <div key={String(label)}><dt>{label}</dt><dd>{paperNumber(value)}<small> {snapshot.account.base_currency}</small></dd></div>)}
  </dl><div className="paper-ledger-meta"><span>名义占用 / 上限 {paperNumber(balance.notional_used)} / {paperNumber(balance.notional_limit)}</span><span>风险占用 / 上限 {paperNumber(balance.risk_used)} / {paperNumber(balance.risk_limit)}</span><span title={balance.mark_status}>估值状态 {paperReason(balance.mark_status)}</span></div>
  {snapshot.execution_health && <p className={snapshot.execution_health.state === "HEALTHY" ? "paper-muted" : "paper-reason"} role="status">模拟撮合后台：{paperReason(snapshot.execution_health.state)} · 最近成功 {paperTime(snapshot.execution_health.last_success_at)}<small> · 轮询状态，不是止损实时成交保证</small></p>}
  {balance.unrealized_pnl === null && <p className="paper-muted">浮盈亏暂未获得有效标记价，净值不以现金余额替代。</p>}</div>;
}

export function PaperOrderCard({ order, busy, canTrade, onCancel, onDetail, showDetail = true }: { order: PaperOrder; busy: boolean; canTrade: boolean; onCancel: () => void; onDetail: () => void; showDetail?: boolean }) {
  return <article className="paper-record"><header><div><strong>{order.symbol}</strong><span>{order.effect === "OPEN" ? "开仓" : "平仓"} · {order.side === "LONG" ? "多" : "空"} · {order.order_type} / {order.time_in_force}</span></div><span className={`status ${order.status.toLowerCase()}`}>{paperStatus(order.status)}</span></header>
    <dl><div><dt>已成交 / 委托</dt><dd>{order.filled_contracts} / {order.contracts} 张</dd></div><div><dt>成交均价</dt><dd>{paperNumber(order.average_fill_price)}</dd></div><div><dt>委托价格</dt><dd>{order.order_type === "MARKET" ? "市价（深度撮合）" : paperNumber(order.limit_price)}</dd></div><div><dt>来源</dt><dd>{order.source}</dd></div></dl>
    <footer><time>{paperTime(order.created_at)}</time><div>{showDetail && <button type="button" onClick={onDetail}>详情</button>}{canTrade && cancellablePaperOrder(order) && <button type="button" disabled={busy} onClick={onCancel}>撤单</button>}</div></footer>{order.reason && <small className="paper-reason" title={order.reason}>{paperReason(order.reason)}</small>}
  </article>;
}

export function PaperPositionCard({ position, busy, canTrade, onClose }: { position: PaperPosition; busy: boolean; canTrade: boolean; onClose: () => void }) {
  const available = Math.max(0, position.remaining_contracts - position.reserved_close_contracts);
  return <article className="paper-record"><header><div><strong>{position.symbol} · {position.side === "LONG" ? "多" : "空"}</strong><span>{position.timeframe} · {position.status === "CLOSED" ? "已平仓" : "持仓中"}</span></div><span>{position.remaining_contracts} 张</span></header>
    <dl><div><dt>入场 / 出场均价</dt><dd>{paperNumber(position.average_entry_price)} / {paperNumber(position.average_exit_price)}</dd></div><div><dt>止损 / 止盈</dt><dd>{paperNumber(position.stop_price)} / {paperNumber(position.take_profit_price)}</dd></div><div><dt>已实现毛盈亏 / 累计手续费</dt><dd>{paperNumber(position.gross_realized_pnl ?? position.realized_pnl)} / {paperNumber(position.fees)}</dd></div><div><dt>已实现净盈亏</dt><dd>{paperNumber(position.net_realized_pnl)}</dd></div><div><dt>已平 / 待平占用</dt><dd>{position.closed_contracts} / {position.reserved_close_contracts} 张</dd></div></dl>
    <p className="paper-muted" title={position.protection_status}>保护状态 {paperReason(position.protection_status)}{position.trigger_reason ? ` · ${paperReason(position.trigger_reason)}` : ""} · 乘数 {paperNumber(position.multiplier)}</p>
    <footer><time>建仓 {paperTime(position.opened_at)}{position.closed_at ? ` · 平仓 ${paperTime(position.closed_at)}` : ""}</time>{canTrade && position.status === "OPEN" && <button type="button" disabled={busy || !available} onClick={onClose}>平仓</button>}</footer>
  </article>;
}

export function PaperFillCard({ fill, onOrder }: { fill: PaperFill; onOrder?: () => void }) {
  return <article className="paper-record"><header><strong>{fill.symbol ? `${fill.symbol} · ` : ""}{fill.effect === "OPEN" ? "开仓成交" : "平仓成交"} · {fill.execution_side}</strong><time>{paperTime(fill.filled_at)}</time></header><dl>
    <div><dt>成交数量 / 价格</dt><dd>{fill.contracts} 张 / {paperNumber(fill.price)}</dd></div><div><dt>基础币数量</dt><dd>{paperNumber(fill.base_quantity)}</dd></div><div><dt>成交额 / 手续费</dt><dd>{paperNumber(fill.notional)} / {paperNumber(fill.fee)}</dd></div><div><dt>本次已实现毛盈亏</dt><dd>{paperNumber(fill.realized_pnl)}</dd></div>
  </dl>{onOrder && <button type="button" onClick={onOrder}>查看关联订单</button>}<details><summary>成交凭据</summary><dl className="paper-proof"><div><dt>订单</dt><dd>{fill.order_id}</dd></div><div><dt>仓位</dt><dd>{fill.position_id}</dd></div><div><dt>行情快照</dt><dd>{fill.quote_id}</dd></div><div><dt>原因</dt><dd>{fill.reason}</dd></div></dl></details></article>;
}

type Tab = "positions" | "orders" | "fills";
type Modal = { kind: "open" } | { kind: "close"; position: PaperPosition } | { kind: "cancel"; order: PaperOrder };
const uncertainError = (cause: unknown) => !(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500 || ["REQUEST_TIMEOUT", "INVALID_RESPONSE"].includes(cause.code);

export function PaperOrdersWorkspace({ accounts, accountId, onAccountChange, canTrade, userId, notify }: {
  accounts: PaperAccount[]; accountId: string; onAccountChange: (value: string) => void;
  canTrade: boolean; userId: string; notify: (value: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("positions"), [page, setPage] = useState(0), [positionState, setPositionState] = useState("OPEN"), [orderState, setOrderState] = useState("ALL");
  const [snapshot, setSnapshot] = useState<PaperSnapshot>(), [rows, setRows] = useState<PaperPage<PaperOrder | PaperPosition | PaperFill>>(paperEmptyPage);
  const [rowsKey, setRowsKey] = useState("");
  const [loading, setLoading] = useState(false), [loadError, setLoadError] = useState(""), [updatedAt, setUpdatedAt] = useState<string>();
  const [modal, setModal] = useState<Modal>(), [detail, setDetail] = useState<PaperOrder>();
  const [instrument, setInstrument] = useState<InstrumentChoice>(), [orderType, setOrderType] = useState("MARKET");
  const [busy, setBusy] = useState(""), [actionError, setActionError] = useState(""), [pending, setPending] = useState<PaperAction | undefined>(() => restorePaperAction(userId));
  const [reauthenticate, setReauthenticate] = useState(false), [passwordError, setPasswordError] = useState("");
  const [preview, setPreview] = useState<{ action: PaperAction; result: PaperPreview; receivedAt: number }>();
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState("");
  const detailRequest = useRef(0);
  const pageCursors = useRef<Record<string, (string | undefined)[]>>({});
  const lock = useRef(false), read = useRef<AbortController | null>(null), mounted = useRef(true);
  const active = accounts.find(account => account.id === accountId)?.status === "ACTIVE";
  const selectionKey = `${accountId}|${tab}|${page}|${positionState}|${orderState}`;
  const cursorKey = `${accountId}|${tab}|${positionState}|${orderState}`;
  const visibleRows = rowsKey === selectionKey ? rows : paperEmptyPage<PaperOrder | PaperPosition | PaperFill>();
  const load = useCallback(async (quiet = false) => {
    if (!accountId) return;
    read.current?.abort(); const controller = new AbortController(); read.current = controller;
    if (!quiet) setLoading(true);
    try {
      const cursor = pageCursors.current[cursorKey]?.[page];
      const state = tab === "positions" ? positionState : tab === "orders" ? orderState : undefined;
      // Legacy OFFSET fallback is only for a rolling-upgrade response without
      // cursors. Normal pagination uses the frozen insertion-window keyset.
      const path = page === 0 || cursor ? paperCursorListPath(accountId, tab, state, cursor) : paperListPath(accountId, tab, page, state);
      const [balance, listing] = await Promise.all([
        api<PaperSnapshot>(`/api/v2/paper/accounts/${encodeURIComponent(accountId)}/snapshot`, { signal: controller.signal }),
        api<PaperPage<PaperOrder | PaperPosition | PaperFill>>(path, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      const stack = page === 0 ? [] : (pageCursors.current[cursorKey] ?? []);
      stack.splice(page+1); if (listing.next_cursor) stack[page+1] = listing.next_cursor;
      pageCursors.current[cursorKey] = stack;
      setSnapshot(balance); setRows(listing); setRowsKey(selectionKey); setLoadError(""); setUpdatedAt(new Date().toISOString());
    } catch (cause) { if (!controller.signal.aborted) setLoadError(cause instanceof Error ? cause.message : "加载模拟账户失败"); }
    finally { if (read.current === controller) { read.current = null; setLoading(false); } }
  }, [accountId, tab, page, positionState, orderState, selectionKey, cursorKey]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; read.current?.abort(); }; }, []);
  useEffect(() => { setLoadError(""); void load(); return () => read.current?.abort(); }, [load]);
  useEffect(() => {
    const refresh = () => { if (document.visibilityState === "visible" && !lock.current && !read.current) void load(true); };
    const interval = window.setInterval(refresh, 5000); document.addEventListener("visibilitychange", refresh);
    return () => { window.clearInterval(interval); document.removeEventListener("visibilitychange", refresh); };
  }, [load]);

  function show(next: Modal) { if (lock.current || pending) return; setModal(next); setOrderType("MARKET"); setInstrument(undefined); setPreview(undefined); setActionError(""); }
  function keepPending(action: PaperAction | undefined) { setPending(action); persistPaperAction(userId, action); }
  async function readOrder(id: string) {
    const sequence = ++detailRequest.current; setDetailLoading(true); setDetailError("");
    try { const result = await api<PaperOrder>(`/api/v2/paper/orders/${encodeURIComponent(id)}`); if (mounted.current && detailRequest.current === sequence) setDetail(result); }
    catch (cause) { if (mounted.current && detailRequest.current === sequence) setDetailError(cause instanceof Error ? cause.message : "订单详情加载失败"); }
    finally { if (mounted.current && detailRequest.current === sequence) setDetailLoading(false); }
  }
  function closeDetail() { ++detailRequest.current; setDetail(undefined); setDetailLoading(false); setDetailError(""); }
  async function execute(action: PaperAction) {
    if (lock.current || !canTrade) return;
    lock.current = true; setBusy("提交中…"); setActionError(""); keepPending(action);
    try {
      const result = await api<PaperMutation>(action.path, { method: "POST", body: jsonBody(action.body) });
      if (!mounted.current) return;
      keepPending(undefined); setModal(undefined); notify(paperMutationNotice(result)); await load(true);
    } catch (cause) {
      if (!mounted.current) return;
      const message = cause instanceof Error ? cause.message : "提交失败";
      if (cause instanceof ApiError && cause.code === "RECENT_AUTH_REQUIRED") {
        keepPending(action); setReauthenticate(true); setPasswordError(""); setActionError("请先确认登录身份；验证后可重试同一请求。");
      } else if (uncertainError(cause)) { keepPending(action); setActionError(`${message}。已保留原请求编号，请刷新记录核对，或重试同一请求；不要创建新委托。`); }
      else { keepPending(undefined); setActionError(message); }
    } finally { lock.current = false; if (mounted.current) setBusy(""); }
  }
  async function previewOpening(body: Record<string, unknown>) {
    lock.current = true; setBusy("估算中…"); setActionError(""); setPreview(undefined);
    try {
      const result = await api<PaperPreview>("/api/v2/paper/orders:preview", { method: "POST", body: jsonBody(body) });
      if (!mounted.current) return;
      setPreview({ action: { path: "/api/v2/paper/orders", body, label: "开仓" }, result, receivedAt: Date.now() });
      setSnapshot(result.account);
    } catch (cause) {
      if (mounted.current) setActionError(cause instanceof Error ? cause.message : "暂时无法预估，尚未提交委托");
    } finally { lock.current = false; if (mounted.current) setBusy(""); }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (lock.current || pending || !modal) return;
    const values = new FormData(event.currentTarget);
    try {
      if (modal.kind === "open") {
        const body = paperOrderBody(values, instrument?.id ?? "", accountId);
        if (!preview || Date.now() - preview.receivedAt > 30_000 || paperIntentKey(body) !== paperIntentKey(preview.action.body)) {
          void previewOpening(body);
        } else if (preview.result.checks.budget_available) {
          void execute(preview.action);
        }
        return;
      }
      if (modal.kind === "cancel") { void execute({ path: `/api/v2/paper/orders/${modal.order.id}:cancel`, body: { idempotency_key: crypto.randomUUID() }, label: "撤单" }); return; }
      const contracts = Number(values.get("contracts")), available = modal.position.remaining_contracts - modal.position.reserved_close_contracts;
      if (!Number.isSafeInteger(contracts) || contracts < 1 || contracts > available) throw new Error("平仓张数超出当前可平仓数量，请刷新记录核对");
      void execute({ path: `/api/v2/paper/positions/${modal.position.id}:close`, label: "平仓", body: { contracts,
        order_type: orderType, time_in_force: values.get("time_in_force"),
        ...(orderType === "LIMIT" ? { limit_price: values.get("limit_price") } : {}),
        max_slippage_bps: values.get("max_slippage_bps"), ttl_seconds: Number(values.get("ttl_seconds")), idempotency_key: crypto.randomUUID() } });
    } catch (cause) { setActionError(cause instanceof Error ? cause.message : "请检查委托参数"); }
  }
  async function confirmIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (lock.current) return;
    const form = event.currentTarget, password = String(new FormData(form).get("password") ?? ""); if (!password) return;
    form.reset(); lock.current = true; setBusy("验证中…"); setPasswordError("");
    try {
      const encrypted = await encryptSubmission(`user:${userId}:reauthenticate`, { password });
      await api("/auth/reauthenticate", { method: "POST", body: jsonBody(wrapEncryptedSubmission(encrypted)) });
      setReauthenticate(false); setActionError("身份已确认，请点击「重试同一请求」继续；尚未提交新委托。");
    } catch (cause) { setPasswordError(cause instanceof Error ? cause.message : "身份验证失败"); }
    finally { lock.current = false; setBusy(""); }
  }
  const locked = !!busy || !!pending;
  const closeDialog = () => { if (!lock.current && !pending) { setModal(undefined); setActionError(""); } };
  return <section className="panel paper-orders-workspace" aria-label="模拟委托与持仓">
    <header className="paper-ledger-header"><div><h2>模拟委托与持仓</h2><small>网页与 MCP 共用本人 PAPER 账户 · 非实盘</small></div><div className="button-row"><button type="button" disabled={loading || !!busy || !accountId} onClick={() => void load()}>刷新账本</button>{canTrade && <button type="button" className="primary" disabled={!active || locked} onClick={() => show({ kind: "open" })}>模拟开仓</button>}</div></header>
    <div className="paper-ledger-controls"><label>交易账户<select aria-label="模拟交易账户" value={accountId} disabled={locked || !!modal} onChange={e => { setPage(0); onAccountChange(e.target.value); }}><option value="">请选择账户</option>{accounts.map(account => <option key={account.id} value={account.id}>{account.name}{account.status !== "ACTIVE" ? "（停用）" : ""}</option>)}</select></label><small>1× 全额抵押 · 下单不等于成交</small></div>
    {pending && !modal && <div className="paper-pending-actions" role="status"><p>有一笔{pending.label}请求尚未核对。保留的是原始参数，重试不会创建第二笔相同委托。</p><code>请求编号 {String(pending.body.idempotency_key)}</code>{actionError && <p role="alert">{actionError}</p>}<button type="button" disabled={!canTrade || !!busy || reauthenticate} onClick={() => void execute(pending)}>{busy || "重试同一请求"}</button><button type="button" disabled={loading || !!busy} onClick={() => void load()}>刷新记录核对</button></div>}
    {!accountId ? <p className="empty">请在下方创建模拟账户，再选择账户查看余额与委托。</p> : <><PageState loading={loading} error={loadError} onRetry={() => void load()} />
      {snapshot?.account.id === accountId && <PaperBalance snapshot={snapshot} />}
      <div className="paper-ledger-tabs" role="tablist" aria-label="模拟交易记录">{(["positions", "orders", "fills"] as const).map(kind => <button type="button" role="tab" aria-selected={tab === kind} key={kind} onClick={() => { setTab(kind); setPage(0); }}>{({ positions: "持仓与平仓", orders: "委托记录", fills: "成交明细" })[kind]}</button>)}</div>
      <div className="paper-ledger-filter">{tab === "positions" && <label>持仓状态<select aria-label="持仓状态" value={positionState} onChange={e => { setPositionState(e.target.value); setPage(0); }}><option value="OPEN">当前持仓</option><option value="CLOSED">已平仓</option><option value="ALL">全部仓位</option></select></label>}{tab === "orders" && <label>委托状态<select aria-label="委托状态" value={orderState} onChange={e => { setOrderState(e.target.value); setPage(0); }}><option value="ALL">全部委托</option>{["OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED"].map(state => <option key={state} value={state}>{paperStatus(state)}</option>)}</select></label>}<small>{updatedAt ? `更新 ${paperTime(updatedAt)} · 前台每 5 秒刷新` : "等待账户数据"}</small></div>
      <div className="paper-record-grid">{visibleRows.items.map(row => tab === "positions" ? <PaperPositionCard key={row.id} position={row as PaperPosition} busy={locked} canTrade={canTrade} onClose={() => show({ kind: "close", position: row as PaperPosition })} /> : tab === "orders" ? <PaperOrderCard key={row.id} order={row as PaperOrder} busy={locked} canTrade={canTrade} onCancel={() => show({ kind: "cancel", order: row as PaperOrder })} onDetail={() => { setDetail(row as PaperOrder); void readOrder(row.id); }} /> : <PaperFillCard key={row.id} fill={row as PaperFill} onOrder={() => void readOrder((row as PaperFill).order_id)} />)}</div>
      {!visibleRows.items.length && !loading && !loadError && <p className="empty">当前筛选下暂无记录。</p>}
      <Pagination label="模拟交易记录" page={page} hasNext={visibleRows.has_more} busy={loading} onChange={setPage} />
      {visibleRows.snapshot_at && page > 0 && <small className="paper-muted">列表插入时间截止 {paperTime(visibleRows.snapshot_at)}；委托和仓位状态仍按最新结果更新。返回首页查看新记录。</small>}
    </>}
    <p className="paper-muted">撮合依据盘口深度和滑点上限，可能部分成交、挂单或拒单；无有效行情时不会假设成交。模拟不包含真实交易所排队、隐性流动性及强平机制。</p>
    {snapshot?.limitations?.length ? <details className="paper-model-limitations"><summary>撮合模型限制</summary><ul>{snapshot.limitations.map((item, index) => <li key={index} title={item}>{paperReason(item)}</li>)}</ul></details> : null}
    {modal && <Dialog title={modal.kind === "open" ? "模拟开仓" : modal.kind === "close" ? `模拟平仓 · ${modal.position.symbol}` : `撤销委托 · ${modal.order.symbol}`} busy={locked} onClose={closeDialog}>
      <form className="paper-order-form" onSubmit={submit} onChange={() => { if (!lock.current && !pending) { setPreview(undefined); setActionError(""); } }}><fieldset disabled={locked}>
        {modal.kind === "open" && <><InstrumentPicker value={instrument?.id ?? ""} selectedLabel={instrument?.symbol} onChange={choice => { setInstrument(choice); setPreview(undefined); }} /><div className="paper-form-grid"><label>方向<select name="side" defaultValue="LONG"><option value="LONG">做多</option><option value="SHORT">做空</option></select></label><label>分析周期<select name="timeframe" defaultValue="15m">{["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"].map(value => <option key={value}>{value}</option>)}</select></label></div></>}
        {modal.kind !== "cancel" ? <><div className="paper-form-grid"><label>合约张数<input name="contracts" type="number" min="1" max={modal.kind === "close" ? modal.position.remaining_contracts - modal.position.reserved_close_contracts : 1000000000} step="1" defaultValue={modal.kind === "close" ? modal.position.remaining_contracts - modal.position.reserved_close_contracts : 1} required /></label><label>委托类型<select name="order_type" value={orderType} onChange={e => setOrderType(e.target.value)}><option value="MARKET">市价</option><option value="LIMIT">限价</option></select></label>
          {orderType === "LIMIT" && <label>限价<input name="limit_price" type="number" min="0" step="any" required /></label>}
          <label>成交规则<select name="time_in_force" key={orderType} defaultValue={orderType === "MARKET" ? "IOC" : "GTC"}>{orderType === "LIMIT" && <option value="GTC">GTC · 未成交继续挂单</option>}<option value="IOC">IOC · 剩余立即取消</option>{orderType === "LIMIT" && <option value="FOK">FOK · 全部成交或取消</option>}</select></label>
          {modal.kind === "open" && <><label>止损价<input name="stop_price" type="number" min="0" step="any" required /></label><label>止盈价<input name="take_profit_price" type="number" min="0" step="any" required /></label></>}
          <label>最大滑点（bps）<input name="max_slippage_bps" type="number" min="0" max="100" step="any" defaultValue="50" required /></label><label>有效期（秒）<input name="ttl_seconds" type="number" min="1" max="86400" step="1" defaultValue="3600" required /></label>
        </div><p className="paper-muted">数量按交易所合约张数计算，不是币数；100 bps = 1%。实际成交以返回的成交记录为准。</p></> : <p>撤销剩余 {modal.order.remaining_contracts} 张。已成交部分及其持仓不会被撤回。</p>}
        {modal.kind === "open" && preview && <><PaperPreviewPanel preview={preview.result} /><button type="button" disabled={locked} onClick={() => setPreview(undefined)}>重新预估</button><p className="paper-muted">修改参数需重新预估；预览超过 30 秒将先更新，不会自动下单。</p></>}
        <button className="primary" disabled={locked || (modal.kind === "open" && (!instrument || (!!preview && !preview.result.checks.budget_available)))}>{busy || (modal.kind === "cancel" ? "确认撤单" : modal.kind === "open" && !preview ? "预览模拟委托" : "确认模拟委托")}</button>
      </fieldset></form>
      {actionError && <p role="alert" className="error-banner">{actionError}</p>}
      {pending && <div className="paper-pending-actions"><code>请求编号 {String(pending.body.idempotency_key)}</code><button type="button" disabled={!!busy || reauthenticate} onClick={() => void execute(pending)}>{busy || "重试同一请求"}</button><button type="button" disabled={!!busy || loading} onClick={() => void load()}>刷新记录核对</button></div>}
    </Dialog>}
    {detail && <Dialog title={`委托详情 · ${detail.symbol}`} onClose={closeDetail}>
      <PageState loading={detailLoading} error={detailError} onRetry={() => void readOrder(detail.id)} />
      <PaperOrderCard order={detail} busy={true} canTrade={false} showDetail={false} onCancel={() => undefined} onDetail={() => undefined} />
      <dl className="paper-proof"><div><dt>订单 ID</dt><dd>{detail.id}</dd></div><div><dt>仓位 ID</dt><dd>{detail.position_id ?? "尚未成交"}</dd></div><div><dt>止损 / 止盈</dt><dd>{paperNumber(detail.stop_price)} / {paperNumber(detail.take_profit_price)}</dd></div><div><dt>冻结名义 / 风险 / 手续费</dt><dd>{paperNumber(detail.reserved_notional)} / {paperNumber(detail.reserved_risk)} / {paperNumber(detail.reserved_fees)}</dd></div><div><dt>到期</dt><dd>{paperTime(detail.expires_at)}</dd></div></dl>
      {!detailError && <><PaperDecisionPanel key={detail.id} order={detail} /><PaperOrderHistoryExplorer key={detail.id} order={detail} /></>}
    </Dialog>}
    {!detail && (detailLoading || detailError) && <Dialog title="读取委托详情" onClose={closeDetail}><PageState loading={detailLoading} error={detailError} /></Dialog>}
    {reauthenticate && <Dialog title="确认登录身份" busy={!!busy} onClose={() => setReauthenticate(false)}><form className="stack-form" onSubmit={confirmIdentity}><label>当前密码<input name="password" type="password" autoComplete="current-password" required autoFocus /></label>{passwordError && <p role="alert">{passwordError}</p>}<button className="primary" disabled={!!busy}>{busy || "验证身份"}</button></form></Dialog>}
  </section>;
}
