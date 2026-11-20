import { useEffect, useState } from "react";
import { api } from "./api";
import { ResearchChart } from "./ResearchChart";
import { ResearchDialog } from "./ResearchWorkspace";
import { ProbabilitySummary, ReportDimensions, TradeTimeFacts } from "./ReportDimensions";
import { PerformanceScreen } from "./PerformanceScreen";
import { RuleResults } from "./ResearchRuleResults";
import { exitLabel, fmt, type Report, type Trade } from "./researchModel";
import { holdingLabel, priceText } from "./chartModel";

type ChartWindow = { offset: number; total: number; has_more: boolean; next_offset: number | null; selected_trade: Trade | null; candles: Report["candles"]; input_bars: Report["input_bars"]; indicator_series: Report["indicator_series"]; equity_curve: Report["full"]["equity_curve"] };
type TradePage = { items: Trade[]; offset: number; total: number; has_more: boolean; next_offset: number | null };
const errorText = (error: unknown) => error instanceof Error ? error.message : "加载失败，请重试";

function FrozenWindow({ report, tradeIndex }: { report: Report; tradeIndex?: number }) {
  const [offset, setOffset] = useState<number>(), [data, setData] = useState<ChartWindow>();
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [retry, setRetry] = useState(0);
  const [mode, setMode] = useState<"equity" | "drawdown">("equity");
  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ limit: "512" });
    if (tradeIndex !== undefined) query.set("trade_index", String(tradeIndex));
    else if (offset !== undefined) query.set("offset", String(offset));
    setBusy(true); setError("");
    void api<ChartWindow>(`${report.detail_storage!.base_url}/chart?${query}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(value => { if (!controller.signal.aborted) setError(errorText(value)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [report, offset, tradeIndex, retry]);
  const windowReport: Report | undefined = data ? { ...report, candles: data.candles, input_bars: data.input_bars,
    indicator_series: data.indicator_series, full: { ...report.full, trades: data.selected_trade ? [data.selected_trade] : [], equity_curve: data.equity_curve } } : undefined;
  return <section className="panel research-report" aria-busy={busy}>
    <header><h2>{tradeIndex === undefined ? "冻结行情与账户走势" : `第 ${tradeIndex + 1} 笔 · 原始入场图`}</h2>{tradeIndex === undefined && <div className="button-row"><button aria-pressed={mode === "equity"} onClick={() => setMode("equity")}>净值</button><button aria-pressed={mode === "drawdown"} onClick={() => setMode("drawdown")}>回撤</button></div>}</header>
    {error && <div role="alert" className="error-banner">{error}<button onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {busy && <p role="status">正在加载冻结图表…</p>}
    {windowReport && <ResearchChart report={windowReport} trade={data?.selected_trade ?? undefined} mode={mode} />}
    {data && <div className="button-row">{tradeIndex === undefined && <button disabled={busy || data.offset === 0} onClick={() => setOffset(Math.max(0, data.offset - 512))}>更早 512 根</button>}<span>第 {data.offset + 1}—{data.offset + data.candles.length} / {data.total.toLocaleString()} 根</span>{tradeIndex === undefined && <button disabled={busy || !data.has_more} onClick={() => setOffset(data.next_offset!)}>更晚 512 根</button>}</div>}
    {data?.selected_trade && <><TradeTimeFacts trade={data.selected_trade} /><p>入场 {priceText(data.selected_trade.entry)} · 止损 {priceText(data.selected_trade.stop)} · 目标 {priceText(data.selected_trade.target)} · 退出 {priceText(data.selected_trade.exit_price)}</p><p>手续费 {fmt(data.selected_trade.fees)} · 资金费 {fmt(data.selected_trade.funding)} · 净损益 {fmt(data.selected_trade.net_pnl)}</p></>}
    <small>图表按原周期分段加载；全区间指标使用全部 K 线计算，回撤保留此前真实资金峰值。</small>
  </section>;
}

export function AnnualIndicatorReport({ report, view }: { report: Report; view: "overview" | "trades" }) {
  const [offset, setOffset] = useState(0), [page, setPage] = useState<TradePage>();
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<number>();
  useEffect(() => {
    if (view !== "trades") return;
    const controller = new AbortController();
    setBusy(true); setError("");
    void api<TradePage>(`${report.detail_storage!.base_url}/trades?limit=25&offset=${offset}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setPage(value); })
      .catch(value => { if (!controller.signal.aborted) setError(errorText(value)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [report, view, offset, retry]);
  const metrics = report.full.metrics;
  return <>
    {!!report.cache_coverage?.missing_bars && <div className="research-warning" role="status">请求 {report.cache_coverage.requested_bars.toLocaleString()} 根，实际覆盖 {report.cache_coverage.available_bars.toLocaleString()} 根；缺失部分未参与回测。</div>}
    {view === "overview" ? <>
      <div className="research-metrics">{[["净损益", fmt(metrics.net_pnl)], ["净收益率", fmt(metrics.total_return, true)], ["最大回撤", fmt(metrics.max_drawdown, true)], ["胜率", fmt(metrics.win_rate, true)], ["成交笔数", metrics.trade_count.toLocaleString()], ["完整日样本", metrics.daily_observations ?? "—"]].map(([label, value]) => <div className="panel" key={label}><span>{label}</span><b>{value}</b></div>)}</div>
      <FrozenWindow key={report.artifact_digest} report={report} />
      <PerformanceScreen report={report} /><RuleResults report={report} /><ReportDimensions report={report} /><ProbabilitySummary report={report} />
      <section className="panel research-report"><h2>前段与后段验证</h2><div className="research-table-scroll"><table><thead><tr><th>区间</th><th>净收益</th><th>最大回撤</th><th>成交</th><th>夏普</th></tr></thead><tbody>{[["前段样本", report.training], ["后段验证", report.validation]].map(([label, metric]) => { const value = metric as Report["training"]; return <tr key={String(label)}><td>{String(label)}</td><td>{fmt(value.total_return, true)}</td><td>{fmt(value.max_drawdown, true)}</td><td>{value.trade_count}</td><td>{fmt(value.sharpe_ratio)}</td></tr>; })}</tbody></table></div></section>
      <details className="panel research-report"><summary>数据与计算依据</summary><p>{report.evidence.reasons.join("；")}</p><p>{report.quality_flags.join(" · ") || "无数据质量标记"}</p><p>原始行情、指标、逐笔交易和资金点位均以校验分片保存，可分页查询；下载报告为汇总与完整分片校验清单。</p><code className="research-hash">{report.engine_version} · {report.artifact_digest}</code></details>
    </> : <section className="panel research-trades" aria-busy={busy}>
      <header><h2>逐笔交易 · {metrics.trade_count.toLocaleString()} 笔</h2></header>
      {error && <div role="alert" className="error-banner">{error}<button onClick={() => setRetry(value => value + 1)}>重试</button></div>}
      {busy && <p role="status">正在加载交易记录…</p>}
      <div className="research-table-scroll"><table><thead><tr><th>入场时间</th><th>退出时间</th><th>方向</th><th>持仓</th><th>入场 → 退出</th><th>费用 / 资金费</th><th>退出原因</th><th>净损益</th><th>详情</th></tr></thead><tbody>{page?.items.map((trade, index) => <tr key={page.offset + index}><td>{new Date(trade.entry_time).toLocaleString()}</td><td>{new Date(trade.exit_time).toLocaleString()}</td><td>{trade.side > 0 ? "多" : "空"}</td><td>{holdingLabel(trade)}</td><td>{priceText(trade.entry)} → {priceText(trade.exit_price)}</td><td>{fmt(trade.fees)} / {fmt(trade.funding)}</td><td>{exitLabel[trade.exit_reason] ?? trade.exit_reason}</td><td>{fmt(trade.net_pnl)}</td><td><button disabled={busy} onClick={() => setSelected(page.offset + index)}>原始图表</button></td></tr>)}</tbody></table></div>
      {!busy && page?.total === 0 && <p>本区间没有完成成交。全部信号和行情证据已保留，零成交不代表盈利。</p>}
      <div className="button-row"><button disabled={busy || !offset} onClick={() => setOffset(Math.max(0, offset - 25))}>上一页</button><span>第 {Math.floor(offset / 25) + 1} / {Math.max(1, Math.ceil((page?.total ?? metrics.trade_count) / 25))} 页</span><button disabled={busy || !page?.has_more} onClick={() => setOffset(page!.next_offset!)}>下一页</button></div>
    </section>}
    {selected !== undefined && <ResearchDialog title="年度回测 · 历史入场记录" onClose={() => setSelected(undefined)}><FrozenWindow key={`${report.artifact_digest}:${selected}`} report={report} tradeIndex={selected} /></ResearchDialog>}
  </>;
}
