import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PaperBalance, PaperFillCard, PaperOrderCard, PaperPositionCard } from "./PaperOrdersWorkspace";
import { PaperDecisionPanel, PaperOrderTimeline, PaperPreviewPanel } from "./PaperOrderInsights";
import type { PaperPreview } from "./paperOrdersModel";
import { cancellablePaperOrder, paperListPath, paperMutationNotice, paperNumber, paperOrderBody, persistPaperAction, restorePaperAction, type PaperFill, type PaperOrder, type PaperPosition, type PaperSnapshot } from "./paperOrdersModel";

const order: PaperOrder = { id: "order-a", account_id: "account-a", instrument_id: "btc", symbol: "BTC_USDT", timeframe: "15m", position_id: "position-a", effect: "OPEN", side: "LONG", execution_side: "BUY", order_type: "LIMIT", time_in_force: "GTC", status: "PARTIALLY_FILLED", contracts: 10, filled_contracts: 4, remaining_contracts: 6, average_fill_price: "100", limit_price: "101", stop_price: "90", take_profit_price: "120", reserved_notional: "606", reserved_risk: "66", reserved_fees: ".3", reason: "INSUFFICIENT_DEPTH", source: "MCP", created_at: "2026-09-13T12:00:00Z", updated_at: "2026-09-13T12:00:01Z", expires_at: "2026-09-13T13:00:00Z" };
const position: PaperPosition = { id: "position-a", account_id: "account-a", instrument_id: "btc", symbol: "BTC_USDT", timeframe: "15m", side: "LONG", opened_contracts: 4, closed_contracts: 1, remaining_contracts: 3, reserved_close_contracts: 1, multiplier: ".001", average_entry_price: "100", average_exit_price: "110", stop_price: "90", take_profit_price: "120", realized_pnl: "9.9", fees: ".1", status: "OPEN", protection_status: "ACTIVE", trigger_reason: null, triggered_at: null, opened_at: "2026-09-13T12:00:01Z", closed_at: null, updated_at: "2026-09-13T12:00:02Z" };
const fill: PaperFill = { id: "fill-a", order_id: order.id, position_id: position.id, effect: "OPEN", side: "LONG", execution_side: "BUY", contracts: 4, price: "100", base_quantity: ".004", notional: ".4", fee: ".0002", realized_pnl: null, reason: "BOOK_MATCH", filled_at: "2026-09-13T12:00:01Z", quote_id: "immutable-quote" };
const snapshot: PaperSnapshot = { environment: "PAPER", account: { id: "account-a", name: "Account", base_currency: "USDT", status: "ACTIVE" }, balance: { initial_equity: "10000", realized_pnl: "9.9", cash_balance: "10009.9", available_balance: "9000", legacy_open_fees: "0", position_margin: "400", order_reserved_margin: "606", order_reserved_fees: ".3", risk_used: "106", risk_limit: "1000", notional_used: "1006", notional_limit: "10000", unrealized_pnl: null, equity: null, mark_status: "NOT_REQUESTED" } };

describe("paper ledger presentation and contracts", () => {
  it("shows fill and event evidence with explicit truncation rather than claiming a complete timeline", () => {
    const html = renderToStaticMarkup(<PaperOrderTimeline order={{ ...order, fills: [fill], events: [{ id: "event", event_type: "MATCHED", created_at: order.created_at,
      payload_json: { status: "PARTIALLY_FILLED", filled_contracts: 4, remaining_contracts: 6, reason: "INSUFFICIENT_DEPTH", request: "do-not-render-raw-request" } }], events_has_more: true, fills_has_more: true, history_limit: 100 }} />);
    expect(html).toContain("本次成交 4 张"); expect(html).toContain("0.0002"); expect(html).toContain("不代表完整成交历史");
    expect(html).not.toContain("do-not-render-raw-request");
  });
  it("does not invent decision attribution for manual or old orders", () => {
    const html = renderToStaticMarkup(<PaperDecisionPanel order={order} />);
    expect(html).toContain("未关联分析依据"); expect(html).not.toContain("<img");
  });
  it("pretrade display separates all-order collateral from partial-fill risk without float calculations", () => {
    const preview: PaperPreview = { environment: "PAPER", preview_only: true, order_created: false, account: snapshot,
      market: { spec: { symbol: "SOL_USDT", multiplier: "0.1", price_tick: "0.01", min_contracts: "0.1", max_contracts: "10000", market_max_contracts: "1000" },
        book: { source_at: order.created_at, captured_at: order.created_at, quote_id: "quote", best_bid: "99", best_ask: "100" } },
      estimate: { requested_contracts: 10, filled_contracts: 2, remaining_contracts: 8, vwap: "100", fee: "0.1", notional: "20", reason: "INSUFFICIENT_DEPTH", resting: true, remainder_cancelled: false },
      sizing: { requested_base_quantity: "1", estimated_base_quantity: "0.2", reference_notional: "100", required_margin: "100.5", required_fees: "0.05", reserved_risk: "10", estimated_fill_fees: "0.1", estimated_stop_loss: "2.20000000000000000002", max_contracts_by_budget: 20, stop_loss_is_guaranteed: false },
      checks: { budget_available: false, reasons: ["ACCOUNT_RISK_LIMIT"], balance_reserved: false, depth_reserved: false } };
    const html = renderToStaticMarkup(<PaperPreviewPanel preview={preview} />);
    expect(html).toContain("10 张 → 1"); expect(html).toContain("2 / 10 张"); expect(html).toContain("2.20000000000000000002");
    expect(html).toContain("不是最大亏损承诺"); expect(html).toContain("超过账户风险额度"); expect(html).toContain("将继续挂单");
  });
  it("keeps unmarked equity and unrealized pnl unknown rather than copying cash", () => {
    const html = renderToStaticMarkup(<PaperBalance snapshot={snapshot} />);
    expect(html).toContain("10,009.9"); expect(html).toContain("浮动盈亏</dt><dd>—"); expect(html).toContain("标记净值</dt><dd>—");
    expect(html).toContain("挂单冻结"); expect(html).toContain("冻结手续费"); expect(html).toContain("净值不以现金余额替代");
  });
  it("distinguishes accepted quantities, actual fills, source and depth failures", () => {
    const html = renderToStaticMarkup(<PaperOrderCard order={order} canTrade={true} busy={false} onCancel={() => undefined} onDetail={() => undefined} />);
    expect(html).toContain("部分成交"); expect(html).toContain("4 / 10 张"); expect(html).toContain("MCP"); expect(html).toContain("INSUFFICIENT_DEPTH"); expect(html).toContain("撤单");
    expect(cancellablePaperOrder(order)).toBe(true);
    for (const status of ["FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"]) expect(cancellablePaperOrder({ ...order, status })).toBe(false);
  });
  it("does not offer cancellation for read-only viewers or completed orders", () => {
    for (const props of [{ canTrade: false, order }, { canTrade: true, order: { ...order, status: "FILLED" } }]) {
      const html = renderToStaticMarkup(<PaperOrderCard {...props} busy={false} onCancel={() => undefined} onDetail={() => undefined} />);
      expect(html).not.toContain(">撤单<");
    }
  });
  it("shows original stop targets, close reservation and fees for positions", () => {
    const html = renderToStaticMarkup(<PaperPositionCard position={position} canTrade={true} busy={false} onClose={() => undefined} />);
    expect(html).toContain("90 / 120"); expect(html).toContain("1 / 1 张"); expect(html).toContain("9.9 / 0.1"); expect(html).toContain(">平仓<");
    expect(html).toContain("已实现毛盈亏 / 累计手续费"); expect(html).toContain("已实现净盈亏</dt><dd>—");
    const reserved = renderToStaticMarkup(<PaperPositionCard position={{ ...position, reserved_close_contracts: 3 }} canTrade={true} busy={false} onClose={() => undefined} />);
    expect(reserved).toContain('disabled="">平仓');
  });
  it("preserves immutable quote provenance and actual fee in fill records", () => {
    const html = renderToStaticMarkup(<PaperFillCard fill={fill} />);
    expect(html).toContain("immutable-quote"); expect(html).toContain("0.4 / 0.0002"); expect(html).toContain("本次已实现毛盈亏</dt><dd>—");
  });
  it("uses server exact net pnl without subtracting fees using browser floats", () => {
    const html = renderToStaticMarkup(<PaperPositionCard position={{ ...position, gross_realized_pnl: "0.30000000000000000003", fees: "0.10000000000000000001", net_realized_pnl: "0.20000000000000000002" }} canTrade={false} busy={false} onClose={() => undefined} />);
    expect(html).toContain("已实现净盈亏</dt><dd>0.20000000000000000002");
  });
  it("uses server pagination and explicit all-position state", () => {
    expect(paperListPath("account-a", "orders", 2, "PARTIALLY_FILLED")).toBe("/api/v2/paper/accounts/account-a/orders?limit=10&offset=20&state=PARTIALLY_FILLED");
    expect(paperListPath("account-a", "positions", 0, "ALL")).toContain("state=ALL");
    expect(paperListPath("account-a", "orders", 0, "ALL")).not.toContain("state=");
  });
  it("does not call an accepted partial order a fully opened trade", () => {
    const notice = paperMutationNotice({ environment: "PAPER", accepted: true, replayed: true, order, fills: [] });
    expect(notice).toContain("已核对原请求：部分成交"); expect(notice).toContain("4/10 张");
    expect(paperNumber(null)).toBe("—"); expect(paperNumber("NaN")).toBe("—"); expect(paperNumber("0")).toBe("0");
    expect(paperNumber("0.00000000123456789")).toBe("0.00000000123456789");
    expect(paperNumber("1234567890123456789.123456789")).toBe("1,234,567,890,123,456,789.123456789");
  });
  it("builds an explicit integer-contract order with prices and one idempotency key", () => {
    const values = new Map(Object.entries({ contracts: "3", side: "SHORT", order_type: "LIMIT", time_in_force: "GTC", timeframe: "5m", limit_price: "100.12345678", stop_price: "110", take_profit_price: "90", max_slippage_bps: "50", ttl_seconds: "3600" }));
    const form = { get: (key: string) => values.get(key) ?? null } as FormData;
    vi.stubGlobal("crypto", { randomUUID: () => "single-request" });
    try {
      const body = paperOrderBody(form, "btc", "account-a");
      expect(body).toMatchObject({ instrument_id: "btc", account_id: "account-a", contracts: 3, limit_price: "100.12345678", order_type: "LIMIT", side: "SHORT", idempotency_key: "single-request" });
      for (const contracts of ["0", "1.5", "-1", "Infinity"]) { values.set("contracts", contracts); expect(() => paperOrderBody(form, "btc", "account-a")).toThrow("正整数"); }
      values.set("contracts", "1"); values.set("ttl_seconds", "86401"); expect(() => paperOrderBody(form, "btc", "account-a")).toThrow("有效期");
    } finally { vi.unstubAllGlobals(); }
  });
  it("persists uncertain commands only within this user's browser session and never retries automatically", () => {
    const stored = new Map<string, string>();
    vi.stubGlobal("sessionStorage", { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value), removeItem: (key: string) => stored.delete(key) });
    try {
      const action = { path: "/api/v2/paper/orders", body: { idempotency_key: "stable-request", account_id: "account-a" }, label: "开仓" };
      persistPaperAction("owner-a", action); expect(restorePaperAction("owner-a")).toEqual(action); expect(restorePaperAction("owner-b")).toBeUndefined();
      persistPaperAction("owner-a", undefined); expect(restorePaperAction("owner-a")).toBeUndefined();
      stored.set("gatescope:paper-pending:owner-a", JSON.stringify({ ...action, path: "https://evil.test/orders" })); expect(restorePaperAction("owner-a")).toBeUndefined();
      stored.set("gatescope:paper-pending:owner-a", "malformed-json"); expect(restorePaperAction("owner-a")).toBeUndefined();
    } finally { vi.unstubAllGlobals(); }
  });
});
