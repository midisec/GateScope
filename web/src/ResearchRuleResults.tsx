import { fmt, regimeName, zeroTradeReason, type Report, type Trade } from "./researchModel";

export function ResearchDiagnostics({ report, requested }: { report: Report; requested: number }) {
  const d = report.full.diagnostics;
  return <section className="panel research-diagnostics"><h2>样本与入场诊断</h2><div className="diagnostic-grid"><span>请求上限<strong>{requested} 根</strong></span><span>实际行情<strong>{report.bar_count} 根</strong></span><span>产生信号<strong>{d?.signal_bars ?? "旧报告未记录"}{d ? " 根" : ""}</strong></span><span>信号过期<strong>{d?.expired_signals ?? "未记录"}</strong></span><span>风险约束拒绝<strong>{d?.risk_rejections ?? "未记录"}</strong></span><span>实际成交<strong>{report.full.metrics.trade_count} 笔</strong></span></div><div className="replay-report-mode"><strong>{report.availability?.assumed ? "历史收盘重建 · 研究假设" : "严格接收时间回放"}</strong><small>{report.availability?.assumed ? "假设收盘数据当时可用，历史修订与接收延迟未被复现；不是实盘可成交证明。" : "事后补录或迟到数据会阻止历史入场。"}</small></div>{report.bar_count < requested && <p>所选区间实际只有 {report.bar_count} 根可用 K 线；{requested} 是读取上限，不是已获取数量。</p>}{!report.full.metrics.trade_count && <p>{zeroTradeReason(report)}</p>}</section>;
}

export function RuleResults({ report }: { report: Report }) {
  const selection = report.selection;
  if (!selection) return null;
  return <section className="panel research-rule-results"><header><h2>本次策略条件</h2><span>{selection.match_mode === "ALL" ? "全部同向满足" : "任一满足"} · {selection.regimes.length ? selection.regimes.map(r => regimeName[r]).join(" / ") : "不限趋势"}</span></header><div className="rule-report-chips">{selection.rules.map(rule => <span key={rule.id}>{rule.name}</span>)}</div>{selection.rules.some(r => r.id.startsWith("pattern:")) && <small>已确认形态 · 评分 ≥ {selection.pattern_confidence} · 共现窗口 {selection.coincidence_bars} 根</small>}<details><summary>各规则参与成交情况</summary><div className="research-table-scroll"><table><thead><tr><th>规则</th><th>参与成交</th><th>历史胜率</th><th>净损益</th></tr></thead><tbody>{report.rule_attribution?.map(r => <tr key={r.id}><td>{r.name}</td><td>{r.trade_count}</td><td>{fmt(r.win_rate, true)}</td><td>{fmt(r.net_pnl)}</td></tr>)}</tbody></table></div><small>一笔交易可能由多项规则共同触发；此表是参与归因，不能相加或当作各规则独立回测。</small></details></section>;
}

export function TradeRuleFacts({ report, trade }: { report: Report; trade: Trade }) {
  if (!trade.matched_rules?.length) return null;
  const events = (report.pattern_events ?? []).filter(e => trade.pattern_refs?.includes(e.evidence_id));
  return <section className="trade-rule-facts"><strong>实际触发：{trade.matched_rules.map(id => report.selection?.rules.find(r => r.id === id)?.name ?? id).join(" + ")}</strong><small>入场信号趋势：{regimeName[trade.regime ?? "UNKNOWN"]}</small>{events.map(e => <span key={e.evidence_id}>{e.name} · 评分 {e.confidence} · 首次确认可用 {new Date(e.available_at).toLocaleString()}</span>)}</section>;
}
