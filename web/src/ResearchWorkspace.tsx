import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, jsonBody } from "./api";
import type { Session } from "./types";
import { ReportDimensions, DrawdownDimensions, ProbabilitySummary, TradeTimeFacts } from "./ReportDimensions";
import { holdingLabel, priceText } from "./chartModel";
import { ResearchChart } from "./ResearchChart";
import { activeRun, annualIndicatorBars, exitLabel, fmt, initialRun, localInput, stateLabel, strategyName, tradeCsv, zeroTradeReason, selectionError, type Rule, type Run, type Spec, type Strategy, type Trade } from "./researchModel";
import { ResearchRulePicker } from "./ResearchRulePicker";
import { ReplayMode, ResearchCoverage } from "./ResearchAvailability";
import { ResearchDiagnostics, RuleResults, TradeRuleFacts } from "./ResearchRuleResults";
import { InstrumentPicker } from "./InstrumentPicker";
import { ResearchHistory } from "./ResearchHistory";
import { PerformanceScreen } from "./PerformanceScreen";
import { DiscoveryReports } from "./DiscoveryReports";
import { StructureResearchWorkspace } from "./StructureResearchWorkspace";
import { AnnualIndicatorReport } from "./AnnualIndicatorReport";
import { PortfolioResearchWorkspace } from "./PortfolioResearchWorkspace";
import "./research.css";

type Tab = "overview" | "trades" | "history" | "discovery" | "structure" | "portfolio";
const message = (e: unknown) => e instanceof Error ? e.message : "请求失败，请重试";
const runFromUrl = () => new URLSearchParams(location.search).get("run") ?? "";
const tabFromUrl = (): Tab => { const value = new URLSearchParams(location.search).get("view"); return value === "structure" || value === "discovery" || value === "portfolio" ? value : "overview"; };

export function ResearchWorkspace({ session }: { session: Session }) {
  const [runs, setRuns] = useState<Run[]>([]), [selectedId, setSelectedId] = useState(runFromUrl);
  const [selected, setSelected] = useState<Run>(), [loading, setLoading] = useState(true);
  const [listLoading, setListLoading] = useState(true), [listError, setListError] = useState("");
  const [runTotal, setRunTotal] = useState(0);
  const [reportError, setReportError] = useState(""), [retry, setRetry] = useState(0);
  const [tab, setTab] = useState<Tab>(tabFromUrl), [chartMode, setChartMode] = useState<"equity" | "drawdown">("equity");
  const [catalog, setCatalog] = useState<Strategy[]>([]), [defaults, setDefaults] = useState<Spec>();
  const [rules, setRules] = useState<Rule[]>([]);
  const [configError, setConfigError] = useState(""), [builder, setBuilder] = useState(false), [copy, setCopy] = useState<Run>();
  const [tradeIndex, setTradeIndex] = useState<number>(), [page, setPage] = useState(0), [filter, setFilter] = useState("all");
  const [builderBusy, setBuilderBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => {
    try {
      const r = await api<{ items: Run[]; total: number }>("/api/v2/research/backtests?engine_mode=INDICATOR");
      setRuns(r.items); setRunTotal(r.total); setSelectedId(current => current || initialRun(r.items)); setListError("");
    } catch (e) { setListError(message(e)); } finally { setListLoading(false); }
  }, []);
  const loadCatalog = useCallback(() => {
    setConfigError("");
    void api<{ items: Strategy[]; rules?: Rule[]; defaults: Spec }>("/api/v2/research/strategies").then(c => { setCatalog(c.items); setRules(c.rules ?? c.items.map(s => ({ ...s, group: "指标策略", direction: "neutral" }))); setDefaults(c.defaults); }).catch(e => setConfigError(message(e)));
  }, []);
  useEffect(() => { void refresh(); loadCatalog(); }, [refresh, loadCatalog]);
  useEffect(() => {
    const back = () => { setSelectedId(runFromUrl()); setTab(tabFromUrl()); };
    window.addEventListener("popstate", back); return () => window.removeEventListener("popstate", back);
  }, []);
  useEffect(() => {
    const url = new URL(location.href);
    if (tab === "discovery" || tab === "structure" || tab === "portfolio") url.searchParams.set("view", tab); else url.searchParams.delete("view");
    history.replaceState({}, "", url);
  }, [tab]);
  useEffect(() => {
    if (selectedId) { const url = new URL(location.href); url.searchParams.set("run", selectedId); history.replaceState({}, "", url); }
  }, [selectedId]);
  const active = runs.some(activeRun), selectedState = runs.find(r => r.id === selectedId)?.state;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [active, refresh]);
  useEffect(() => {
    if (listLoading) return;
    if (!selectedId) { setSelected(undefined); setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setReportError(""); setSelected(undefined); setTradeIndex(undefined); setPage(0);
    void api<Run>(`/api/v2/research/backtests/${encodeURIComponent(selectedId)}`, { signal: controller.signal }).then(r => {
      if (!controller.signal.aborted) { setSelected(r); if (r.state === "SUCCEEDED" && !r.report) setReportError("报告暂时不可读取，请重试"); }
    }).catch(e => { if (!controller.signal.aborted) setReportError(message(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selectedId, selectedState, retry, listLoading]);
  function selectRun(run: Run) {
    const url = new URL(location.href); url.searchParams.set("run", run.id); history.pushState({}, "", url);
    setSelectedId(run.id); setTab("overview"); setTradeIndex(undefined); setPage(0); setFilter("all");
  }
  function openBuilder(run?: Run) { setCopy(run); setBuilder(true); }
  function download(csv = false) {
    if (!selected?.report) return;
    const content = csv ? tradeCsv(selected.report.full.trades) : JSON.stringify(selected, null, 2);
    const url = URL.createObjectURL(new Blob([content], { type: csv ? "text/csv;charset=utf-8" : "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `backtest-${selected.id}.${csv ? "csv" : "json"}`; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const report = selected?.spec.engine_mode === "STRUCTURE" ? undefined : selected?.report, metrics = report?.full?.metrics;
  const currentProgress = runs.find(run => run.id === selectedId)?.progress ?? selected?.progress;
  const filtered = report?.full.trades.map((t, index) => ({ t, index })).filter(({ t }) => filter === "all" || (filter === "win" ? t.net_pnl > 0 : t.net_pnl <= 0)) ?? [];
  const trade = tradeIndex === undefined ? undefined : report?.full.trades[tradeIndex];
  return <main className="settings-page research-page">
    <section className="page-title"><div><h1>策略回测</h1><small className="user-meta">历史研究 · 不是真实交易</small></div>{tab !== "structure" && tab !== "portfolio" && <div className="button-row"><button onClick={() => { void refresh(); setRetry(n => n + 1); }}>刷新</button><button className="primary" onClick={() => openBuilder()} disabled={session.user.role === "VIEWER"}>＋ 新建指标回测</button></div>}</section>
    {notice && <div className="research-feedback" role="status">{notice}<button onClick={() => setNotice("")}>关闭</button></div>}
    {listError && <div className="error-banner" role="alert">记录刷新失败：{listError}<button onClick={() => void refresh()}>重新加载记录</button></div>}
    <nav className="research-tabs" aria-label="回测视图">{([["structure", "同源形态回测"], ["portfolio", "共享资金组合"], ["overview", "指标结果概览"], ["trades", `逐笔交易${metrics ? ` · ${metrics.trade_count}` : ""}`], ["history", `指标历史 · ${listLoading ? "…" : runTotal}`], ["discovery", "策略挖掘"]] as [Tab, string][]).map(([id, label]) => <button key={id} aria-pressed={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>)}</nav>
    {tab === "structure" ? <StructureResearchWorkspace session={session} /> : tab === "portfolio" ? <PortfolioResearchWorkspace session={session} /> : tab === "discovery" ? <DiscoveryReports refreshToken={retry} /> : tab === "history" ? <ResearchHistory selectedId={selectedId} onSelect={selectRun} /> : <>
      {selected?.spec.engine_mode === "STRUCTURE" && <section className="panel research-empty"><h2>这是同源形态回测报告</h2><a href={`?view=structure&structure_run=${encodeURIComponent(selected.id)}`}>打开同源形态报告</a></section>}
      {(loading || listLoading) && <Loading label="正在加载回测报告…" />}
      {reportError && <section className="panel research-empty" role="alert"><h2>报告加载失败</h2><p>{reportError}</p><button onClick={() => setRetry(n => n + 1)}>重新加载报告</button><button onClick={() => setTab("history")}>选择其他记录</button></section>}
      {!loading && !listLoading && !selectedId && !listError && <section className="panel research-empty"><h2>还没有回测报告</h2><p>选择币种和策略，运行后收益曲线与逐笔交易会自动显示在这里。</p><button className="primary" onClick={() => openBuilder()} disabled={session.user.role === "VIEWER"}>创建第一次回测</button></section>}
      {!loading && selected && activeRun(selected) && <section className="panel research-empty" role="status"><Loading label={`${stateLabel[selected.state]} · ${selected.symbol} / ${selected.timeframe}`} />{currentProgress?.phase && <p>{({ FREEZING_INPUTS: "冻结历史输入", CALCULATING: "计算指标与交易", SAVING_EVIDENCE: "保存对比证据", COMPLETE: "生成报告" } as Record<string, string>)[currentProgress.phase] ?? "后台处理中"} · {(currentProgress.processed_bars ?? 0).toLocaleString()} / {(currentProgress.total_bars ?? 0).toLocaleString()} 根</p>}<p>已保存任务，完成后自动展示报告。可以离开页面，稍后从历史记录查看。</p><small>提交于 {new Date(selected.created_at).toLocaleString()}</small></section>}
      {!loading && selected?.state === "FAILED" && <section className="panel research-empty" role="alert"><h2>这次回测未完成</h2><p>{selected.error ?? "计算失败"}</p><button onClick={() => openBuilder(selected)}>调整参数后重试</button></section>}
      {!loading && report && selected && metrics && <>
        <section className="research-result-heading"><div><small>{strategyName[selected.spec.strategy]} · {selected.timeframe} · {selected.spec.direction === "LONG" ? "只做多" : selected.spec.direction === "SHORT" ? "只做空" : "双向"}</small><h2>{selected.symbol} 回测报告</h2><p>{new Date(report.range.from).toLocaleString()} — {new Date(report.range.to).toLocaleString()} · 实际 {report.bar_count} 根 K 线</p></div><div className="button-row"><button onClick={() => openBuilder(selected)}>载入参数</button><button onClick={() => download()}>下载报告</button></div></section>
        {report.detail_storage ? <AnnualIndicatorReport key={selected.id} report={report} view={tab === "trades" ? "trades" : "overview"} /> : tab === "overview" ? <>
          <p className="research-context-note">指标研究引擎：{report.engine_version}。旧报告保持原计算规则，不等同于新版同源形态回测。</p><PerformanceScreen report={report} />
          <div className="research-metrics">{[["净损益", fmt(metrics.net_pnl), metrics.net_pnl < 0 ? "research-negative" : "research-positive"], ["净收益率", fmt(metrics.total_return, true)], ["最大回撤", fmt(metrics.max_drawdown, true)], ["胜率", fmt(metrics.win_rate, true)], ["成交笔数", metrics.trade_count], ["期末净值", fmt(report.spec.initial_equity + metrics.net_pnl)]].map(([label, value, color]) => <div className="panel" key={String(label)}><span>{label}</span><b className={String(color ?? "")}>{value}</b></div>)}</div>
          {!metrics.trade_count && <div className="research-warning" role="status"><strong>本次回测没有成交</strong><p>{zeroTradeReason(report)}</p></div>}
          <section className="panel research-report"><header><h2>{chartMode === "equity" ? "账户净值" : "回撤走势"}</h2><div className="button-row"><button aria-pressed={chartMode === "equity"} onClick={() => setChartMode("equity")}>净值曲线</button><button aria-pressed={chartMode === "drawdown"} onClick={() => setChartMode("drawdown")}>回撤曲线</button></div></header><ResearchChart report={report} mode={chartMode} onTradeSelect={setTradeIndex} />{chartMode === "drawdown" && <DrawdownDimensions report={report} />}<footer>初始资金 {fmt(report.spec.initial_equity)} · 手续费 {fmt(metrics.fees)} · 资金成本 {fmt(metrics.funding)} · 盈利因子 {fmt(metrics.profit_factor)}</footer></section>
          <ResearchDiagnostics report={report} requested={selected.bar_limit} /><RuleResults report={report} /><ReportDimensions report={report} /><ProbabilitySummary report={report} />
          <div className="research-analysis-grid"><section className="panel research-report"><h2>区间验证</h2><div className="research-table-scroll"><table><thead><tr><th>区间</th><th>净收益率</th><th>回撤</th><th>交易</th></tr></thead><tbody>{[{ label: "前段样本", metric: report.training }, { label: "后段验证", metric: report.validation }].map(({ label, metric }) => <tr key={label}><td>{label}</td><td>{fmt(metric.total_return, true)}</td><td>{fmt(metric.max_drawdown, true)}</td><td>{metric.trade_count}</td></tr>)}</tbody></table></div></section><section className="panel research-report"><h2>证据状态</h2><strong>{report.evidence.positive_validation_evidence ? "验证期出现正向收益证据" : "尚无充分正收益证据"}</strong><p className="muted-copy">{report.validation.trade_count < 20 ? `后段仅 ${report.validation.trade_count} 笔交易，未达到统计下界的最低样本要求。` : `交易收益下界 ${fmt(report.evidence.bootstrap_mean_lcb, true)}。历史验证不代表未来收益。`}</p><button onClick={() => setTab("trades")}>检查逐笔交易 →</button></section></div>
          <details className="panel research-report"><summary>参数、数据质量与计算依据</summary><p>{report.evidence.reasons.join("；")}</p><p>{report.quality_flags.join(" · ") || "无数据质量标记"}</p><p>EMA {report.spec.fast_period}/{report.spec.slow_period} · ADX {report.spec.adx_threshold} · 止损 {report.spec.stop_atr} ATR · 盈亏比 {report.spec.reward_risk} · 风险 {fmt(report.spec.risk_fraction, true)}</p><p>历史盈亏平衡胜率 {fmt(report.evidence.payoff?.empirical_break_even_win_rate, true)} · 置信水平 {fmt(report.evidence.confidence_level, true)} · 累计尝试 {report.evidence.tested_hypotheses}</p><code className="research-hash">{report.engine_version} · {report.artifact_digest}</code></details>
        </> : <section className="panel research-trades"><header><h2>逐笔交易</h2><div className="button-row"><select aria-label="交易盈亏筛选" value={filter} onChange={e => { setFilter(e.target.value); setPage(0); }}><option value="all">全部交易</option><option value="win">盈利交易</option><option value="loss">亏损 / 持平</option></select><button onClick={() => download(true)}>导出 CSV</button></div></header><div className="research-table-scroll"><table><thead><tr><th>入场时间</th><th>退出时间（记账）</th><th>持仓时长</th><th>方向</th><th>成交价 → 退出价</th><th>数量</th><th>手续费 / 资金费</th><th>退出原因</th><th>净损益</th><th>详情</th></tr></thead><tbody>{filtered.slice(page * 20, (page + 1) * 20).map(({ t, index }) => <tr key={index}><td>{new Date(t.entry_time).toLocaleString()}</td><td>{new Date(t.exit_time).toLocaleString()}</td><td>{holdingLabel(t)}</td><td>{t.side > 0 ? "多" : "空"}</td><td>{priceText(t.entry)} → {priceText(t.exit_price)}</td><td>{priceText(t.quantity)}</td><td>{fmt(t.fees)} / {fmt(t.funding)}</td><td>{exitLabel[t.exit_reason]}</td><td className={t.net_pnl >= 0 ? "research-positive" : "research-negative"}>{fmt(t.net_pnl)}</td><td><button onClick={() => setTradeIndex(index)}>查看图表</button></td></tr>)}</tbody></table></div>{!filtered.length && <p className="muted-copy">{metrics.trade_count ? "没有符合筛选的交易。" : zeroTradeReason(report)}</p>}<div className="button-row"><button disabled={!page} onClick={() => setPage(page - 1)}>上一页</button><span>{page + 1} / {Math.max(1, Math.ceil(filtered.length / 20))}</span><button disabled={(page + 1) * 20 >= filtered.length} onClick={() => setPage(page + 1)}>下一页</button></div></section>}
      </>}
    </>}
    {builder && <ResearchDialog busy={builderBusy} title={copy ? "载入参数 · 新建回测" : "新建回测"} onClose={() => setBuilder(false)}>{configError ? <div role="alert">{configError}<button onClick={loadCatalog}>重试加载策略</button></div> : defaults ? <BacktestBuilder rules={rules} onBusy={setBuilderBusy} defaults={defaults} catalog={catalog} copy={copy} active={active} onCreated={run => { setRuns(items => [run, ...items]); selectRun(run); setBuilder(false); setNotice("回测已提交，完成后自动展示结果。"); void refresh(); }} /> : <Loading label="正在加载策略配置…" />}</ResearchDialog>}
    {trade && report && <ResearchDialog title="历史入场记录" onClose={() => setTradeIndex(undefined)}><div className="research-trade-summary"><strong className={trade.net_pnl < 0 ? "research-negative" : "research-positive"}>净损益 {fmt(trade.net_pnl)}</strong><span>{trade.side > 0 ? "做多" : "做空"} · {exitLabel[trade.exit_reason]}</span><span>{new Date(trade.entry_time).toLocaleString()} → {new Date(trade.exit_time).toLocaleString()}</span></div><TradeTimeFacts trade={trade} /><TradeRuleFacts report={report} trade={trade} /><ResearchChart report={report} trade={trade} /><div className="research-trend-values">{[["入场", trade.entry], ["退出", trade.exit_price], ["止损", trade.stop], ["止盈", trade.target], ["手续费", trade.fees], ["资金成本", trade.funding]].map(([label, v]) => <div key={label}><span>{label}</span><b>{priceText(Number(v))}</b></div>)}</div><details><summary>开仓时指标快照</summary><p>信号可用时间 {new Date(trade.signal_available_at).toLocaleString()}</p><div className="research-trend-values">{Object.entries(trade.indicators).map(([k, v]) => <div key={k}><span>{k}</span><b>{priceText(v)}</b></div>)}</div></details><div className="button-row"><button disabled={tradeIndex === 0} onClick={() => setTradeIndex(i => i! - 1)}>上一笔</button><span>{tradeIndex! + 1} / {report.full.trades.length}</span><button disabled={tradeIndex === report.full.trades.length - 1} onClick={() => setTradeIndex(i => i! + 1)}>下一笔</button></div></ResearchDialog>}
  </main>;
}

function Loading({ label }: { label: string }) { return <div className="research-loading" role="status"><span className="research-spinner" />{label}</div>; }

export function ResearchDialog({ title, onClose, children, busy = false }: { title: string; onClose: () => void; children: ReactNode; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; const previous = document.body.style.overflow; dialog.showModal(); document.body.style.overflow = "hidden"; return () => { dialog.close(); document.body.style.overflow = previous; }; }, []);
  return <dialog ref={ref} className="research-dialog" aria-label={title} onCancel={e => { e.preventDefault(); if (!busy) onClose(); }} onClick={e => { if (!busy && e.target === ref.current) onClose(); }}><div className="research-dialog-body"><header><h2>{title}</h2><button disabled={busy} aria-label="关闭弹窗" onClick={onClose}>✕</button></header>{children}</div></dialog>;
}

function BacktestBuilder({ defaults, catalog, rules, copy, active, onCreated, onBusy }: { onBusy: (busy: boolean) => void; defaults: Spec; catalog: Strategy[]; rules: Rule[]; copy?: Run; active: boolean; onCreated: (run: Run) => void }) {
  const [spec, setSpec] = useState(copy?.spec ?? defaults), [instrument, setInstrument] = useState(copy?.instrument_id ?? "");
  const [timeframe, setTimeframe] = useState(copy?.timeframe ?? "4h"), [limit, setLimit] = useState(copy?.bar_limit ?? 2190);
  const [annual, setAnnual] = useState(!copy);
  const [start, setStart] = useState(localInput(copy?.from_time)), [end, setEnd] = useState(localInput(copy?.to_time));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  function numberField(key: keyof Spec, label: string, min: number, max: number, step = 1) {
    return <label>{label}<input type="number" required min={min} max={max} step={step} value={Number(spec[key])} onChange={e => setSpec(s => ({ ...s, [key]: Number(e.target.value) }))} /></label>;
  }
  async function submit(e: FormEvent) {
    e.preventDefault(); if (busy) return;
    if (!instrument) { setError("请搜索并选择交易标的"); return; }
    const invalid = selectionError(spec, rules); if (invalid) { setError(invalid); return; }
    if (spec.fast_period >= spec.slow_period) { setError("EMA 快线周期必须小于慢线周期"); return; }
    if (start && end && new Date(start) >= new Date(end)) { setError("开始时间必须早于结束时间"); return; }
    if (limit > 5000 && spec.rule_ids?.some(rule => rule.startsWith("pattern:"))) { setError("包含形态扫描的组合最多 5000 根；年度形态请使用「同源形态回测」。"); return; }
    setBusy(true); onBusy(true); setError("");
    try { onCreated(await api<Run>("/api/v2/research/backtests", { method: "POST", body: jsonBody({ instrument_id: instrument, timeframe, spec, bar_limit: limit, from_time: start ? new Date(start).toISOString() : null, to_time: end ? new Date(end).toISOString() : null }) })); }
    catch (e) { setError(message(e)); } finally { setBusy(false); onBusy(false); }
  }
  return <form className="research-form" onSubmit={submit} aria-busy={busy}>
    <div className="research-wide"><InstrumentPicker value={instrument} selectedLabel={copy?.symbol} onChange={item => setInstrument(item.id)} /></div>
    <label>周期<select aria-label="回测周期" value={timeframe} onChange={e => { setTimeframe(e.target.value); if (annual) setLimit(annualIndicatorBars(e.target.value)); }}>{["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"].map(t => <option key={t}>{t}</option>)}</select></label>
    <label>方向<select value={spec.direction} onChange={e => setSpec({ ...spec, direction: e.target.value })}><option value="LONG">只做多</option><option value="SHORT">只做空（合约）</option><option value="BOTH">双向（合约）</option></select></label><label>最多 K 线<input type="number" min={300} max={550000} value={limit} onChange={e => { setLimit(Number(e.target.value)); setAnnual(false); }} required /></label>
    <div className="button-row research-wide"><button type="button" aria-pressed={annual} onClick={() => { setLimit(annualIndicatorBars(timeframe)); setAnnual(true); setStart(""); setEnd(""); }}>最近一年</button><span>按所选周期读取本地缓存 · 长历史分片计算</span></div>
    <ReplayMode value={spec.availability_mode} onChange={availability_mode => setSpec({ ...spec, availability_mode })} /><ResearchCoverage instrument={instrument} timeframe={timeframe} limit={limit} start={start} end={end} />
    <ResearchRulePicker spec={spec} catalog={rules} onChange={setSpec} />
    {numberField("initial_equity", "初始资金", 100, 1e8)}{numberField("risk_fraction", "风险比例（0.01 = 1%）", .001, .03, .001)}
    {numberField("stop_atr", "止损 ATR 倍数", .5, 6, .1)}{numberField("reward_risk", "止盈 / 止损比", .5, 5, .1)}
    <details className="research-wide"><summary>指标参数与持仓期限</summary><div className="research-form">{numberField("fast_period", "EMA 快线", 3, 100)}{numberField("slow_period", "EMA 慢线", 10, 200)}{numberField("lookback", "通道 / 布林周期", 5, 100)}{numberField("adx_threshold", "ADX 阈值", 5, 50, .5)}{numberField("max_holding_bars", "最长持仓 K 线", 1, 200)}<p className="research-wide muted-copy">{catalog.find(s => s.id === spec.strategy)?.rule}</p></div></details>
    <details className="research-wide"><summary>手续费、滑点与日期范围</summary><div className="research-form">{numberField("fee_bps", "单边手续费（bp）", 0, 100, .1)}{numberField("slippage_bps", "单边滑点（bp）", 0, 100, .1)}{numberField("funding_bps_per_8h", "每 8h 资金成本（bp）", 0, 100, .1)}<label>开始时间<input type="datetime-local" value={start} onChange={e => setStart(e.target.value)} /></label><label>结束时间<input type="datetime-local" value={end} onChange={e => setEnd(e.target.value)} /></label></div></details>
    {error && <div className="error-banner research-wide" role="alert">{error}</div>}
    <button className="primary research-wide" disabled={busy || active}>{busy ? "提交中…" : active ? "已有回测运行中，请稍后" : "开始回测"}</button>
  </form>;
}
