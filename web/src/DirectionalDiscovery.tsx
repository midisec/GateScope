import { useEffect, useMemo, useRef, useState } from "react";
import { AreaSeries, BaselineSeries, ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { api } from "./api";
import { priceText } from "./chartModel";
import { fmt, type Metrics, type Screening } from "./researchModel";
import { Dialog, PageState, Pagination } from "./ui";
import { observationNotes, observationOnly } from "./PerformanceScreen";

type Row = { key: string; name: string; category: string; dataset: string; timeframe: string; metrics: Metrics; stress_metrics: Metrics; screening: Screening; stress_screening: Screening; rule: { family: string; direction: string; regime: string; exit_style: string } };
export type DirectionalStudy = { kind: "DIRECTIONAL"; title: string; from_time: string; to_time: string; development_trials: number; frozen_count: number; qualified_count: number; stress_qualified_count: number; rows: Row[]; notes: string[]; artifact_digest: string; source_artifact_digest?: string };
type Trade = { symbol?: string; side: number; entry_time: string; exit_time: string; signal_time: string; entry: number; exit_price: number; stop: number; target: number; net_pnl: number; fees: number; funding: number; exit_reason: string; indicators?: Record<string, number | null>; signal?: { projected_target: number | null; anchors: { index: number; price: number; known_index: number; time: string; known_time: string }[] } };
type Detail = { row: Row; trades: Trade[]; equity_curve: { time: string; equity: number }[]; full_metrics: Metrics; artifact_digest: string };
const regime: Record<string,string> = { ANY: "不限环境", TREND: "EMA20/200同向", RANGE: "ADX<20", STRONG_TREND: "ADX≥25且趋势同向", SLOW_TREND: "EMA80/800同向", NORMAL_VOL: "正常波动率" };
const exit: Record<string,string> = { QUICK: "0.8R / 最多24根", SWING: "1.4R / 最多72根", MEASURED: "结构投影 / 最多72根" };

function DailyCurve({ points, mode }: { points: Detail["equity_curve"]; mode: "equity" | "drawdown" }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, { autoSize: true, layout: { background: { type: ColorType.Solid, color: "#101722" }, textColor: "#9bacbd" }, grid: { vertLines: { color: "#1b2532" }, horzLines: { color: "#1b2532" } }, handleScroll: { vertTouchDrag: false } });
    let peak = points[0]?.equity ?? 10000;
    const series = mode === "equity" ? chart.addSeries(AreaSeries, { lineColor: "#6c9dff", topColor: "#6c9dff33", bottomColor: "#6c9dff05" }) : chart.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, bottomLineColor: "#ef5c75", bottomFillColor1: "#ef5c7544", bottomFillColor2: "#ef5c7505", priceFormat: { type: "percent" } });
    series.setData(points.map(p => { peak = Math.max(peak, p.equity); return { time: Math.floor(Date.parse(p.time)/1000) as UTCTimestamp, value: mode === "equity" ? p.equity : (p.equity / peak - 1)*100 }; }));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, mode]);
  return <div ref={ref} className="research-chart" aria-label={mode === "equity" ? "方向策略日净值" : "方向策略日回撤"} />;
}

export function DirectionalDiscovery({ report, identifier }: { report: DirectionalStudy; identifier: string }) {
  const [category, setCategory] = useState(""), [status, setStatus] = useState(""), [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Row>(), [detail, setDetail] = useState<Detail>(), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [retry, setRetry] = useState(0), [tradePage, setTradePage] = useState(0), [mode, setMode] = useState<"equity" | "drawdown">("equity"), [tradeIndex, setTradeIndex] = useState<number>();
  const evidenceRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (tradeIndex === undefined) return;
    const frame = requestAnimationFrame(() => evidenceRef.current?.scrollIntoView({ block: "start", behavior: "smooth" }));
    return () => cancelAnimationFrame(frame);
  }, [tradeIndex]);
  const rows = useMemo(() => report.rows.filter(r => (!category || r.category === category) && (!status || (status === "OBSERVE_ONLY" ? observationOnly(r.screening) : !observationOnly(r.screening) && r.screening.status === status))), [report, category, status]);
  useEffect(() => {
    setSelected(undefined); setDetail(undefined); setPage(0); setCategory(""); setStatus("");
  }, [identifier]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController(); setLoading(true); setDetail(undefined); setError(""); setTradePage(0); setTradeIndex(undefined); setMode("equity");
    void api<Detail>(`/api/v2/research/discoveries/${encodeURIComponent(identifier)}/directional/${encodeURIComponent(selected.key)}`, { signal: controller.signal }).then(r => { if (!controller.signal.aborted) setDetail(r); }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [identifier, selected, retry]);
  const trade = tradeIndex === undefined ? undefined : detail?.trades[tradeIndex];
  return <div className="directional-discovery">
    <section className="panel research-report"><h2>{report.title}</h2><p>BINANCE 永续历史数据 · 开发组合 {report.development_trials.toLocaleString()} · 冻结验证 {report.frozen_count} · 非对冲</p><strong>{report.rows.length} 套绩效记录 · 未开启实盘</strong><p>基础成本与双倍成本指标均为观察参考，不再按四项数值限制部署。真实执行仍需账户权限、执行认证和安全检查；历史评估保留。</p><p className="muted-copy">后段区间 {report.from_time} — {report.to_time}，按周期额外隔离72根 K 线。BTC/ETH 的已看过数据不作为新的验证证据。</p></section>
    <section className="panel research-report"><h2>指标 / 形态 / 波浪验证</h2><div className="button-row"><label>类别<select aria-label="方向研究类别" value={category} onChange={e => { setCategory(e.target.value); setPage(0); }}><option value="">全部</option>{["指标","形态","波浪"].map(c => <option key={c}>{c}</option>)}</select></label><label>记录<select aria-label="方向研究验证结果" value={status} onChange={e => { setStatus(e.target.value); setPage(0); }}><option value="">全部</option><option value="OBSERVE_ONLY">绩效观察</option><option value="PASS">历史参考达标</option><option value="REJECTED">历史参考未达标</option></select></label></div>
      <div className="research-table-scroll"><table><thead><tr><th>策略 / 数据集</th><th>夏普</th><th>回撤</th><th>命中率</th><th>t 值</th><th>交易</th><th>净收益</th><th>观察备注</th><th>详情</th></tr></thead><tbody>{rows.slice(page*10,page*10+10).map(r => <tr key={r.key}><td>{r.name}<br />{r.dataset}</td><td>{fmt(r.metrics.sharpe_ratio)}</td><td>{fmt(r.screening.checks.find(c => c.metric === "max_drawdown")?.value, true)}</td><td>{fmt(r.metrics.win_rate, true)}</td><td>{fmt(r.metrics.t_statistic)}</td><td>{r.metrics.trade_count}</td><td>{fmt(r.metrics.total_return, true)}</td><td>{observationNotes(r.screening)}</td><td><button onClick={() => setSelected(r)}>查看验证交易</button></td></tr>)}</tbody></table></div>
      {!rows.length && <p>没有符合当前筛选的方案。</p>}<Pagination label="方向研究结果" page={page} pages={Math.max(1,Math.ceil(rows.length/10))} total={rows.length} onChange={setPage} />
    </section>
    <details className="panel research-report"><summary>规则与验证边界</summary>{report.notes.map(n => <p key={n}>{n}</p>)}<code className="research-hash">{report.source_artifact_digest ?? report.artifact_digest}</code></details>
    {selected && <Dialog title={`${selected.name} · ${selected.dataset}`} onClose={() => setSelected(undefined)}><PageState loading={loading} error={error} onRetry={() => setRetry(n => n+1)} />{detail && <>
      <p>{selected.rule.direction === "LONG" ? "只做多" : "单仓位多 / 空"} · {regime[selected.rule.regime]} · {exit[selected.rule.exit_style]}</p><p>基础观察：{observationNotes(selected.screening)}；双倍成本夏普 {fmt(selected.stress_metrics.sharpe_ratio)}，命中率 {fmt(selected.stress_metrics.win_rate,true)}；压力观察：{observationNotes(selected.stress_screening)}。以上不作为部署限制。</p>
      <div className="button-row"><button aria-pressed={mode === "equity"} onClick={() => setMode("equity")}>日净值</button><button aria-pressed={mode === "drawdown"} onClick={() => setMode("drawdown")}>日回撤</button></div><DailyCurve points={detail.equity_curve} mode={mode} /><small>曲线按日显示；筛选回撤取完整区间与验证区间的周期收盘回撤较大值，不是逐 tick 回撤。</small>
      <div className="research-table-scroll"><table className="directional-trades"><thead><tr><th>币种 / 方向</th><th>入场 → 退出</th><th>成交价 → 退出价</th><th>止损 / 止盈</th><th>手续费 / 资金费</th><th>净损益</th><th>信号证据</th></tr></thead><tbody>{detail.trades.slice(tradePage*10,tradePage*10+10).map((t,i) => <tr key={`${t.symbol ?? ""}-${t.entry_time}`}><td>{t.symbol ?? selected.dataset}<br />{t.side > 0 ? "多" : "空"}</td><td>{new Date(t.entry_time).toLocaleString()} → {new Date(t.exit_time).toLocaleString()}</td><td>{priceText(t.entry)} → {priceText(t.exit_price)}</td><td>{priceText(t.stop)} / {priceText(t.target)}</td><td>{fmt(t.fees)} / {fmt(t.funding)}</td><td>{fmt(t.net_pnl)}</td><td><button onClick={() => setTradeIndex(tradePage*10+i)}>入场依据</button></td></tr>)}</tbody></table></div><Pagination label="方向验证交易" page={tradePage} pages={Math.max(1,Math.ceil(detail.trades.length/10))} total={detail.trades.length} onChange={setTradePage} />
      {trade && <section ref={evidenceRef} className="trade-rule-facts"><strong>该笔冻结信号 · {new Date(trade.signal_time).toLocaleString()}</strong><span>原始结构投影：{trade.signal?.projected_target == null ? "无结构投影，按固定盈亏比退出" : priceText(trade.signal.projected_target)}；实际止盈：{priceText(trade.target)}</span><span>退出原因：{trade.exit_reason}（盘中止盈止损按该根 K 线结束记账，不冒充逐笔成交时间）</span><div className="research-table-scroll"><table><thead><tr><th>枢轴时间</th><th>当时价格</th><th>确认可用时间</th></tr></thead><tbody>{trade.signal?.anchors.map(p => <tr key={p.index}><td>{new Date(p.time).toLocaleString()}</td><td>{priceText(p.price)}</td><td>{new Date(p.known_time).toLocaleString()}</td></tr>)}</tbody></table></div><span>RSI {fmt(trade.indicators?.["rsi.rsi"])} · ADX {fmt(trade.indicators?.["adx.adx"])} · ATR {priceText(trade.indicators?.["atr.atr"] ?? undefined)}</span><button onClick={() => setTradeIndex(undefined)}>收起依据</button></section>}
    </>}</Dialog>}
  </div>;
}
