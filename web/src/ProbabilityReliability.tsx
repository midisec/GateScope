import { useState } from "react";
import { patternPercent as pct, patternTime } from "./patternModel";

type Interval = { lower: number; upper: number } | null;
export type ReliabilityWindow = {
  status: string; sample_count: number; calibration_count: number; unpredicted_count: number;
  sampled_candidate_count?: number; unresolved_count?: number; censored_count?: number;
  confirmation_rate: number | null; confirmation_interval: Interval;
  formation_rate: number | null; formation_interval: Interval;
  mean_prediction: number | null; predicted_cohort_confirmation_rate: number | null;
  overconfidence_gap: number | null; brier: number | null;
  bins: { lower: number; upper: number; sample_count: number; mean_prediction: number | null; observed_confirmation_rate: number | null; interval: Interval }[];
};
export type ProbabilityReliabilityData = {
  version: string; as_of: number; annual_from: number; recent_from: number;
  annual: ReliabilityWindow; recent: ReliabilityWindow; brier_delta: number | null;
  minimum_predictions: number; interval_method: string; interval_comparisons: number;
  scope: string; comparison: string; current_regime_status: string; formation_calibration_status: string;
  environment_reference?: { key: string | null; as_of: number; matched: ReliabilityWindow; known_sample_count: number;
    legacy_unlabelled_count: number; fallback: string; confidence_applied: boolean };
  profit_probability: null;
};

const interval = (value: Interval) => value ? `${pct(value.lower)}–${pct(value.upper)}` : "样本不足 · 暂无区间";
const number = (value: number | null) => value == null ? "—" : value.toFixed(3);

export function ProbabilityReliability({ value, phase }: { value: ProbabilityReliabilityData; phase: string }) {
  const [window, setWindow] = useState<"recent" | "annual">("recent");
  const selected = value[window];
  const environment = value.environment_reference;
  return <section aria-label="历史概率可靠性对比">
    <p>{phase === "FORMING" ? "形成中候选" : "已形成结构"} · 同模型、同币种周期、全部初始评分</p>
    <div className="pattern-stats" role="region" aria-label="近期与年度可靠性" tabIndex={0}><table>
      <thead><tr><th>窗口</th><th>已结束 / 有预测</th><th>平均预测</th><th>预测样本实际确认率</th><th>Brier</th><th>历史形成率</th></tr></thead>
      <tbody>{(["recent", "annual"] as const).map(key => {
        const row = value[key];
        return <tr key={key}><th>{key === "recent" ? "近 30 天" : "近 365 天"}</th><td>{row.sample_count} / {row.calibration_count}
          {row.sampled_candidate_count != null && <small className="pattern-caption"> · 待定 {row.unresolved_count} · 中断排除 {row.censored_count}</small>}</td>
          <td>{pct(row.mean_prediction)}</td><td>{pct(row.predicted_cohort_confirmation_rate)}</td><td>{number(row.brier)}</td>
          <td>{phase === "FORMING" ? pct(row.formation_rate) : "已形成"}</td></tr>;
      })}</tbody>
    </table></div>
    <div className="pattern-filters"><label>概率分桶<select aria-label="可靠性窗口" value={window} onChange={event => setWindow(event.target.value as "recent" | "annual")}>
      <option value="recent">近 30 天</option><option value="annual">近 365 天</option></select></label></div>
    {selected.status !== "READY" && <p role="status">有效预测不足 {value.minimum_predictions} 个，当前结果仅供描述，不作为高胜率结论。</p>}
    <div className="pattern-stats" role="region" aria-label="预测概率与实际确认率分桶" tabIndex={0}><table>
      <thead><tr><th>原始预测区间</th><th>样本</th><th>平均预测</th><th>实际确认率</th><th>95% 校正区间</th></tr></thead>
      <tbody>{selected.bins.map((bin, index) => <tr key={bin.lower}><th>{pct(bin.lower)}–{pct(bin.upper)}{index < 4 ? "（不含上界）" : ""}</th>
        <td>{bin.sample_count}</td><td>{pct(bin.mean_prediction)}</td><td>{pct(bin.observed_confirmation_rate)}</td><td>{interval(bin.interval)}</td></tr>)}</tbody>
    </table></div>
    <p className="pattern-caption">该窗口没有原始概率的 {selected.unpredicted_count} 个样本保留在完成统计中，不补填预测或加入 Brier。Brier 越低表示历史预测误差越小，不等于交易收益更高。</p>
    {environment && <section aria-label="同环境历史样本"><h3>报告末端环境对照</h3>
      <p>{environment.key ?? "连续数据不足，暂不分类"} · 截至 {patternTime(environment.as_of)}</p>
      <p>同环境有效样本 {environment.matched.sample_count} · 历史确认率 {pct(environment.matched.confirmation_rate)} · {interval(environment.matched.confirmation_interval)}</p>
      <p>{environment.fallback === "OVERALL" ? "匹配样本不足，继续参考整体历史，不替换原有概率。" : "匹配组仅供对照，不自动替换原有概率。"}旧样本中 {environment.legacy_unlabelled_count} 个缺少当时环境标签，保留整体统计但不编造归类。</p>
    </section>}
    <details><summary>数据范围与限制</summary><p>近 30 天范围：{patternTime(value.recent_from)} 至 {patternTime(value.as_of)}。年度范围从 {patternTime(value.annual_from)} 开始，缺失行情不补造。</p>
      <p>只评估当时已发布的确认概率；待定、中断、重叠和统计时刻尚未结束的样本排除。待定与中断数量仅统计已去重采样的候选；确认率是已结束样本的比例，不是所有新候选的最终成功率。窗口相互重叠，对比不是独立样本显著性检验。形成率与确认率分开，历史形成概率尚未完整投影，不伪造形成概率校准。</p>
      <p>环境标签仅在新阶段首次观测时冻结，使用已收盘前缀的 EMA20/50 趋势、ATR14 相对波动和相对成交量。旧样本不倒填标签，报告末端环境不代表此刻行情；确认率不是盈利概率。</p>
    </details>
  </section>;
}
