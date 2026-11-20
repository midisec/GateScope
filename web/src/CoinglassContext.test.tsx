import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CoinglassCacheStatus, CoinglassControls, CoinglassReviewContent } from "./CoinglassContext";
import type { CoinglassBundle, CoinglassReview } from "./coinglassModel";

const bundle: CoinglassBundle = { status: "PARTIAL", instrument_id: "btc", timeframe: "1m",
  scope: { exchange: "Gate", symbol: "BTC_USDT", base_asset: "BTC", quote_asset: "USDT", settlement_currency: "USDT", market: "FUTURES_USDT" },
  match: { status: "EXACT" }, as_of: 1800000120, fetched_at: 1800000121, expires_at: 1800000241,
  series: [{ id: "funding", label: "资金费率", unit: "PERCENT", unit_status: "UNKNOWN", population: "FUNDING_RATE", native_timeframe: "1m", status: "MISMATCH", quality_flags: [], points: [] }] };
const review: CoinglassReview = { assessment_id: "hash", state: "READY", assessed_at: 1800000121, decision_bar_time: 1800000060, instrument_id: "btc", timeframe: "1m", candidate_id: "candidate", evidence_id: "evidence",
  basis: "CURRENT_REASSESSMENT_NOT_ORIGINAL_EVIDENCE", scope: bundle.scope, match: bundle.match, reason_codes: [],
  metric_evidence: [{ id: "oi", label: "OI 持仓量", unit: "USD", population: "OPEN_INTEREST_MARK_VALUE", latest: { time: 1800000060, close_time: 1800000120, available_at: 1800000121, value: 110 }, window_start: 1800000000, window_end: 1800000120, points_count: 2, window_change_percent: 10, quality_flags: [], note: "美元持仓估值变化含价格影响，不等于净多或净空资金流入" }],
  original: { phase: "FORMING", confidence: .85, signal_direction: "neutral", forecast: { id: "forecast", kind: "NEXT_PIVOT", target: { price: 115, lower: 110, upper: 120 }, issued_at: 1800000000 } },
  original_conditions: { confirmation: [{ side: 1, price: 103, projected_at: 1800000060, comparison: "CLOSE_ABOVE" }], projected_at: 1800000060, confirmation_closes: 2, invalidation: { price: 90, side: -1, projected_at: 1800000060, mode: "CLOSE_INVALIDATION" } },
  target_semantics: "NEXT_PIVOT_OBSERVATION_NOT_TAKE_PROFIT", adjusted_formation_probability: null, adjusted_win_rate: null, automatic_price_adjustment: false, auto_order: false, scenario: null };

describe("Coinglass chart context presentation", () => {
  it("labels scope, missing units and coverage instead of a fake zero funding rate", () => {
    const html = renderToStaticMarkup(<CoinglassControls bundle={bundle} metrics={[]} onMetricsChange={vi.fn()} onRefresh={vi.fn()} />);
    expect(html).toContain("Gate · BTC_USDT · USDT 结算 · 永续");
    expect(html).toContain("单位待核实"); expect(html).not.toContain("0%");
    expect(html).toContain("最多 256 期"); expect(html).toContain('disabled="" aria-label="显示 资金费率 副图"');
  });
  it("shows rate limiting and a bounded retry hint without repeated submit controls", () => {
    const html = renderToStaticMarkup(<CoinglassControls bundle={{ ...bundle, status: "RATE_LIMITED", retry_at: Date.now() / 1000 + 120, retry_after_seconds: 120 }} metrics={[]} onMetricsChange={vi.fn()} onRefresh={vi.fn()} loading />);
    expect(html).toContain("数据源限频中"); expect(html).toContain("秒后可重试"); expect(html).toContain("读取中…");
  });
  it("uses absolute cooldown time and does not call yesterday's limit current", () => {
    const current = { ...bundle, status: "RATE_LIMITED", refresh_status: "RATE_LIMITED", retry_at: 1120, retry_after_seconds: 120, last_attempt_at: 1000, refresh_requested: true };
    expect(renderToStaticMarkup(<CoinglassCacheStatus bundle={current} now={1090} />)).toContain("30 秒后可重试");
    const expired = renderToStaticMarkup(<CoinglassCacheStatus bundle={current} now={1120} />);
    expect(expired).not.toContain("限频中"); expect(expired).not.toContain("秒后可重试");
    expect(expired).toContain("已请求后台更新");
  });
  it("keeps retained same-scope series selectable but explicitly historical", () => {
    const retained: CoinglassBundle = { ...bundle, status: "STALE", cache_retained: true, series: [{ id: "oi", label: "OI", unit: "USD", unit_status: "VERIFIED", population: "OPEN_INTEREST_MARK_VALUE", native_timeframe: "1m", status: "STALE", quality_flags: [], points: [{ time: 60, close_time: 120, available_at: 121, value: 100 }] }] };
    const html = renderToStaticMarkup(<CoinglassControls bundle={retained} metrics={["oi"]} onMetricsChange={vi.fn()} onRefresh={vi.fn()} />);
    expect(html).toContain("包含历史缓存"); expect(html).toContain("缓存参考"); expect(html).toContain("获取于");
    expect(html).not.toContain('disabled="" aria-label="显示 OI 持仓量 副图"');
  });
  it("distinguishes actual collecting and queue age without promising a completion ETA", () => {
    const html = renderToStaticMarkup(<CoinglassCacheStatus bundle={{ ...bundle, refresh_state: "COLLECTING", queued_at: 1000, queue_age_seconds: 1, collection_started_at: 1060, next_poll_after_seconds: 3 }} now={1090} autoPolling />);
    expect(html).toContain("后台采集中"); expect(html).toContain("已等待 1 分 30 秒");
    expect(html).toContain("本次采集已进行 30 秒"); expect(html).toContain("每 3 秒检查进度"); expect(html).toContain("不是完成时间");
  });
  it("does not hide a ready metric while the next update is queued", () => {
    const pending: CoinglassBundle = { ...bundle, status: "PARTIAL", refresh_state: "QUEUED", queued_at: 1800000000, next_poll_after_seconds: 5,
      series: [{ id: "oi", label: "OI", unit: "USD", unit_status: "VERIFIED", population: "OPEN_INTEREST_MARK_VALUE", native_timeframe: "1m", status: "READY", quality_flags: [], points: [{ time: 1800000060, close_time: 1800000120, available_at: 1800000121, value: 100 }] }] };
    const status = renderToStaticMarkup(<CoinglassCacheStatus bundle={pending} now={1800000130} />);
    expect(status).toContain("已就绪 1/5"); expect(status).toContain("已排队，等待采集"); expect(status).toContain("部分数据可用");
    const controls = renderToStaticMarkup(<CoinglassControls bundle={pending} metrics={["oi"]} onMetricsChange={vi.fn()} onRefresh={vi.fn()} />);
    expect(controls).toContain("$100"); expect(controls).not.toContain('disabled="" aria-label="显示 OI 持仓量 副图"');
  });
  it("shows provider cooldown separately from scheduling and does not invent wait duration", () => {
    const cooling = renderToStaticMarkup(<CoinglassCacheStatus bundle={{ ...bundle, refresh_state: "COOLDOWN", retry_at: 2000, next_poll_after_seconds: 2 }} now={1000} autoPolling />);
    expect(cooling).toContain("等待额度"); expect(cooling).toContain("1000 秒后可重试"); expect(cooling).not.toContain("每 2 秒");
    const scheduled = renderToStaticMarkup(<CoinglassCacheStatus bundle={{ ...bundle, refresh_state: "SCHEDULED", next_eligible_at: 1100 }} now={1000} />);
    expect(scheduled).toContain("已计划，等待调度"); expect(scheduled).toContain("最早采集"); expect(scheduled).not.toContain("已等待");
  });
  it("keeps the forming pivot an observation and displays original prediction separately", () => {
    const original = structuredClone(review);
    const html = renderToStaticMarkup(<CoinglassReviewContent review={review} />);
    expect(review).toEqual(original); expect(html).toContain("下一拐点观察区"); expect(html).toContain("110–120");
    expect(html).toContain("原始结构 · 保持不变"); expect(html).toContain("不是已确认的交易方向或止盈区");
    expect(html).toContain("不等于净多或净空资金流入"); expect(html).toContain("未重新估计形成概率或胜率");
    expect(html).not.toContain("胜率 85%");
  });
  it("shows no usable metrics and stale reasons without manufacturing evidence", () => {
    const html = renderToStaticMarkup(<CoinglassReviewContent review={{ ...review, state: "UNAVAILABLE", reason_codes: ["SNAPSHOT_STALE"], metric_evidence: [] }} />);
    expect(html).toContain("数据快照已过期"); expect(html).toContain("暂无符合时间、单位与合约口径的数据");
    expect(html).not.toContain("$110");
  });
});
