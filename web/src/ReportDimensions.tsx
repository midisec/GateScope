import { priceText, drawdownSummary, durationLabel, holdingLabel, timeNumber } from "./chartModel";
import { fmt, type Report } from "./researchModel";

export function ReportDimensions({ report }: { report: Report }) {
  const trades = report.full.trades, d = report.dimensions;
  const meanHold = d?.average_holding_seconds ?? (trades.length ? trades.reduce((s, t) => s + timeNumber(t.exit_time) - timeNumber(t.entry_time), 0) / trades.length : null);
  const firstEntry = d?.first_entry_time ?? trades[0]?.entry_time, lastExit = d?.last_exit_time ?? trades.at(-1)?.exit_time;
  return <section className="panel report-dimensions"><h2>交易时间与执行维度</h2><dl>{[
    ["首次入场", firstEntry ? new Date(firstEntry).toLocaleString() : "无成交"],
    ["最后退出（记账）", lastExit ? new Date(lastExit).toLocaleString() : "无成交"],
    ["平均持仓", meanHold === null ? "—" : durationLabel(meanHold)],
    ["多 / 空交易", `${d?.long_count ?? trades.filter(t => t.side > 0).length} / ${d?.short_count ?? trades.filter(t => t.side < 0).length}`],
    ["平均每笔净损益", fmt(d?.average_net_pnl ?? (trades.length ? report.full.metrics.net_pnl / trades.length : null))],
    ["验证区间开始", new Date(report.range.validation_from).toLocaleString()],
  ].map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl><small>{report.time_convention ?? "入场按开盘撮合；K 线内止盈止损的精确时刻未知，退出时间按该 K 线终结记账。"}</small></section>;
}

export function DrawdownDimensions({ report }: { report: Report }) {
  const d = drawdownSummary(report);
  if (!d || d.trough.drawdown >= 0) return <div className="drawdown-facts">本区间未发生净值回撤。</div>;
  return <div className="drawdown-facts"><div><small>最大峰谷回撤</small><strong>{d.trough.drawdown.toFixed(2)}% · {fmt(d.peak.equity - d.trough.equity)} USDT</strong></div><div><small>峰值 → 谷底</small><span>{new Date(d.peak.time).toLocaleString()} → {new Date(d.trough.time).toLocaleString()}</span></div><div><small>恢复历史峰值</small><span>{d.recovery ? new Date(d.recovery.time).toLocaleString() : "截至区间末未恢复"} · {durationLabel(d.seconds)}</span></div><small>DD = 净值 / 此前最高净值 − 1；0% 为历史峰值，不是累计收益率。按 K 线收盘净值计算，非盘中最大回撤。</small></div>;
}

export function ProbabilitySummary({ report }: { report: Report }) {
  return <section className="panel probability-summary"><h2>历史胜率与概率区间</h2><div className="probability-grid">{(["full", "validation"] as const).map(key => {
    const p = report.probability?.[key], m = key === "full" ? report.full.metrics : report.validation;
    return <article key={key}><h3>{key === "full" ? "全区间" : "后段验证"}</h3><strong>{fmt(m.win_rate, true)}</strong><span>{m.trade_count} 笔 · 扣成本后盈利为胜</span><small>{p?.interval ? `二项近似区间 ${fmt(p.interval.lower, true)}–${fmt(p.interval.upper, true)}（${fmt(p.confidence_level, true)}）` : m.trade_count < 20 ? "少于 20 笔，不估计概率区间" : "旧版报告未计算区间；可载入参数新建回测"}</small></article>;
  })}</div><small>区间基于独立同分布假设，并按累计尝试数修正；交易相关性与调参会降低可信度。历史胜率不等于下一笔盈利概率。</small></section>;
}

export function TradeTimeFacts({ trade }: { trade: Report["full"]["trades"][number] }) {
  return <dl className="trade-time-facts">{[["信号可用", trade.signal_available_at ? new Date(trade.signal_available_at).toLocaleString() : "旧报告未记录"], ["入场时间", new Date(trade.entry_time).toLocaleString()], ["退出记账时间", new Date(trade.exit_time).toLocaleString()], ["持仓时长", holdingLabel(trade)], ["数量", priceText(trade.quantity)], ["时间精度", ["STOP", "STOP_GAP", "TARGET"].includes(trade.exit_reason) ? "OHLC 区间内，非秒级成交时刻" : "K 线收盘"]].map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}
