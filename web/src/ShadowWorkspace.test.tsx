import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ShadowCohortCard, ShadowDetailView, ShadowEnvironment, ShadowWorkerStatus } from "./ShadowWorkspace";
import type { ShadowDetail, ShadowSummary } from "./researchV2Model";

const trade: ShadowDetail = {
  id: "shadow-id", alert_event_id: "original-alert-id", instrument_id: "btc-id", symbol: "BTC_USDT", timeframe: "4h", execution_timeframe: "1m", pattern_id: "double_bottom", pattern_name: "双底", cohort: "DELIVERED", state: "CLOSED", reason_code: "SAME_BAR_STOP_FIRST", signal_at: "2026-01-01T00:00:00Z", delivered_at: "2026-01-01T00:00:10Z", eligible_at: "2026-01-01T00:00:10Z", entry_at: "2026-01-01T00:01:00Z", exit_at: "2026-01-01T02:00:00Z", side: "LONG", entry_price: 100, exit_price: 95, stop_price: 95, target_price: 110,
  raw_gross_return: -.05, gross_return: -.052, net_return: -.054, slippage_return: .002, costs_return: .004, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T02:00:00Z", engine_version: "structure-trade-plan-1", environment: "SHADOW", forward_only: true,
  plan: { reference_price: 100, signal_time: 1767225600, deadline_time: 1767312000, stop_price: 95, target_price: 110 }, source_event: { evidence_id: "frozen-proof" }, result: { fees: .001, funding: .001, ambiguous: true }, policy: { fee_bps: 5, slippage_bps: 5, funding_bps_per_8h: 1, max_holding_bars: 48, stop_mode: "PRICE_TOUCH" }, events_total: 27, events_has_more: true, events_next_offset: 20,
  events: [{ sequence: 1, event_type: "CAPTURED", observed_at: "2026-01-01T00:00:00Z", payload: {} }, { sequence: 2, event_type: "STATE_CHANGED", observed_at: "2026-01-01T02:00:00Z", payload: { state: "STOP" } }],
};
const summary: ShadowSummary = { enabled_at: null, environment: "SHADOW", forward_only: true, counts: { total: 2, waiting_delivery: 0, pending: 0, open: 0, closed: 2, skipped: 0, expired: 0 }, cohorts: [], reason_counts: [], by_context: [{ dimension: "trend", bucket: "UNKNOWN", cohort: "SIGNAL", total: 1, closed: 1, wins: 0, win_rate: null, mean_net_return: -.01, descriptive_only: true }, { dimension: "trend", bucket: "UNKNOWN", cohort: "DELIVERED", total: 1, closed: 1, wins: 1, win_rate: null, mean_net_return: .01, descriptive_only: true }] };

describe("notification shadow ledger presentation", () => {
  it("shows frozen levels, normalized raw gross and fully inclusive costs separately", () => {
    const html = renderToStaticMarkup(<ShadowDetailView trade={trade} />);
    expect(html).toContain("<dt>价格毛收益</dt><dd>-5.00%</dd>");
    expect(html).toContain("<dt>扣滑点收益</dt><dd>-5.20%</dd>");
    expect(html).toContain("<dt>总成本（含滑点）</dt><dd>0.40%</dd>");
    expect(html).toContain("<dt>模拟净收益</dt><dd>-5.40%</dd>");
    expect(html).toContain("95 / 110"); expect(html).toContain("frozen-proof");
  });
  it("makes simulation and lifecycle outcome uncertainty explicit", () => {
    const html = renderToStaticMarkup(<ShadowDetailView trade={trade} />);
    expect(html).toContain("模拟记录，不是真实订单"); expect(html).toContain("同根触及止盈止损，按止损优先");
    expect(html).toContain("记录信号"); expect(html).toContain("模拟状态变化 · 止损先触及");
    expect(html).toContain("共 27 条"); expect(html).toContain("历史计划不会随走势改写");
    expect(html).not.toContain("确认开单");
  });
  it("does not fabricate unfilled prices, arrival timestamps or net returns", () => {
    const html = renderToStaticMarkup(<ShadowDetailView trade={{ ...trade, state: "WAITING_DELIVERY", entry_price: null, exit_price: null, entry_at: null, exit_at: null, delivered_at: null, net_return: null, raw_gross_return: null, gross_return: null, costs_return: null, slippage_return: null, result: null }} />);
    expect(html).toContain("<dt>通知送达</dt><dd>—</dd>"); expect(html).toContain("<dt>模拟净收益</dt><dd>—</dd>"); expect(html).toContain("等待通知送达");
  });
  it("retains unknown environments and separates same-signal observation cohorts", () => {
    const html = renderToStaticMarkup(<ShadowEnvironment summary={summary} />);
    expect(html).toContain("全部信号观察组"); expect(html).toContain("通知送达观察组"); expect(html).toContain("样本未就绪");
    expect(html).toContain("未就绪"); expect(html).toContain("不可加总"); expect(html).not.toContain("50.00%");
  });
  it("warns of a stopped or stale background worker independently of record count", () => {
    const html = renderToStaticMarkup(<ShadowWorkerStatus worker={{ state: "STALE", last_cycle_at: null, safe_error_code: null }} />);
    expect(html).toContain("后台观察：更新延迟"); expect(html).toContain("当前记录可能尚未更新"); expect(html).not.toContain("没有信号");
  });
  it("keeps a one-observation win descriptive instead of implying stable certainty", () => {
    const html = renderToStaticMarkup(<ShadowCohortCard group={{ cohort: "SIGNAL", total: 1, closed: 1, wins: 1, win_rate: 1, mean_net_return: .01, mean_gross_return: .012, mean_costs_return: .002 }} />);
    expect(html).toContain("100.00%"); expect(html).toContain("结算样本不足 20"); expect(html).toContain("尚不足以判断稳定性");
  });
});
