import { fmt, type Report, type Screening } from "./researchModel";

export function screeningLabel(value?: Screening | null) {
  return !value ? "未计算绩效指标" : observationOnly(value) ? "绩效观察 · 不限制部署" : "历史参考评估 · 不再限制部署";
}

export const observationOnly = (value: Screening) => value.status === "OBSERVE_ONLY" || value.thresholds_enforced === false;
export const observationNotes = (value: Screening) => [
  ...value.checks.filter(c => !c.passed).map(c => `${c.label}${c.value == null ? "不可估计" : "在参考范围外"}`),
  ...(!value.sample_passed ? ["样本偏少"] : []),
  ...([...(value.observation_codes ?? []), ...value.reason_codes].includes("COLLATERAL_EXHAUSTION") ? ["历史模拟保证金不足"] : []),
].join("、") || "指标在参考范围内";

export function PerformanceScreen({ report }: { report: Report }) {
  const screen = report.screening;
  if (!screen) return <section className="panel research-report"><h2>绩效观察</h2><p>旧报告未计算完整绩效指标，可载入参数重新回测。绩效指标不限制部署；真实执行仍需账户权限与执行认证。</p></section>;
  return <section className="panel research-report performance-screen" aria-label="绩效观察">
    <header><h2>绩效观察 · 后段验证</h2><strong>{screeningLabel(screen)}</strong></header>
    <p>夏普、回撤、命中率、t 值和样本量仅作观察参考，不作为强制部署条件。</p>
    {!observationOnly(screen) && <p className="muted-copy">此处为历史报告的原始数值和参考评估，历史记录不变，当前已取消绩效门槛。</p>}
    <p className="muted-copy">成本模型：单边手续费 {fmt(report.spec?.fee_bps)} bp · 滑点 {fmt(report.spec?.slippage_bps)} bp · 每 8h 资金 {fmt(report.spec?.funding_bps_per_8h)} bp</p>
    <div className="research-table-scroll"><table><thead><tr><th>指标</th><th>验证值</th><th>参考线（非门槛）</th><th>观察</th></tr></thead><tbody>{screen.checks.map(c => {
      const percent = c.metric === "max_drawdown" || c.metric === "win_rate";
      return <tr key={c.gate_code}><td>{c.label}</td><td title={String(c.value ?? "不可估计")}>{fmt(c.value, percent)}</td><td>{c.operator} {fmt(c.threshold, percent)}</td><td>{c.passed ? "参考范围内" : c.value == null ? "无法估计" : "参考范围外"}</td></tr>;
    })}</tbody></table></div>
    <p className="muted-copy">验证样本 {screen.daily_observations} 日 · {screen.trade_count} 笔交易；参考样本量 {screen.minimum_daily_observations} 日 / {screen.minimum_trades} 笔{screen.sample_passed ? "" : " · 样本偏少，估计可能不稳定"}</p>
    <details><summary>统计口径与执行权限</summary>{observationOnly(screen) && <p>{screen.basis}</p>}<p>完整 UTC 日净值收益，包含空仓日，年化 365 日、现金基准为 0；夏普与 t 使用保守 HAC 方差。含手续费、滑点及资金成本。真实执行仍需满足账户权限、策略执行认证和安全检查。</p><p>{screen.policy_version} · 验证开始 {new Date(report.range.validation_from).toLocaleString()}</p></details>
  </section>;
}
