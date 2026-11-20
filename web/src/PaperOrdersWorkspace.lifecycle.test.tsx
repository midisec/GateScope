import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { ApiError } from "./api";
import { PaperOrdersWorkspace, PaperOrderCard, PaperPositionCard, PaperBalance } from "./PaperOrdersWorkspace";
import { PaperPreviewPanel } from "./PaperOrderInsights";
import { InstrumentPicker } from "./InstrumentPicker";
import { Dialog, PageState, Pagination } from "./ui";
import type { PaperAccount } from "./types";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => void | (() => void))[], api: vi.fn(), encrypt: vi.fn(), notify: vi.fn(), interval: vi.fn(), listeners: new Map<string, () => void>() }));
vi.mock("react", async original => ({ ...await original<typeof import("react")>(),
  useCallback(fn: unknown) { return fn; },
  useEffect(effect: () => void) { harness.effects.push(effect); },
  useRef(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: value }; return harness.slots[index]; },
  useState(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof value === "function" ? value() : value; return [harness.slots[index], (next: unknown) => { harness.slots[index] = typeof next === "function" ? next(harness.slots[index]) : next; }]; },
}));
vi.mock("./api", async original => ({ ...await original<typeof import("./api")>(), api: harness.api }));
vi.mock("./submissionCrypto", () => ({ encryptSubmission: harness.encrypt, wrapEncryptedSubmission: (envelope: unknown) => ({ encrypted_submission: envelope }) }));
type Element = ReactElement<Record<string, unknown>>;
const account: PaperAccount = { id: "account-a", name: "My account", base_currency: "USDT", equity: "10000", realized_pnl: "0", risk_fraction: ".01", max_notional_fraction: ".1", status: "ACTIVE", etag: 1 };
const order = { id: "order-a", account_id: account.id, symbol: "BTC_USDT", status: "OPEN", contracts: 5, filled_contracts: 0, remaining_contracts: 5, effect: "OPEN", side: "LONG", reason: null };
const position = { id: "position-a", account_id: account.id, symbol: "BTC_USDT", status: "OPEN", remaining_contracts: 5, reserved_close_contracts: 2 };
const snapshot = { environment: "PAPER", account, balance: { cash_balance: "10000", unrealized_pnl: null, equity: null } };
const preview = { environment: "PAPER", preview_only: true, order_created: false, account: snapshot, checks: { budget_available: true, reasons: [] } };
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function render(canTrade = true) { harness.cursor = 0; return PaperOrdersWorkspace({ accounts: [account], accountId: account.id, onAccountChange: vi.fn(), userId: "owner-a", canTrade, notify: harness.notify }); }
function find(predicate: (element: Element) => boolean, canTrade = true) { const result = elements(render(canTrade)).find(predicate); if (!result) throw new Error("Element not found"); return result; }
function click(text: string) { (find(node => node.type === "button" && node.props.children === text).props.onClick as () => void)(); }
function setRows(rows: unknown[]) { harness.api.mockResolvedValueOnce(snapshot).mockResolvedValueOnce({ items: rows, limit: 10, offset: 0, has_more: true, next_offset: 10 }); }
async function initial(rows: unknown[] = []) { setRows(rows); render(); harness.effects[0](); harness.effects[1](); harness.effects[2](); await vi.waitFor(() => expect(find(node => node.type === PageState).props.loading).toBe(false)); }
function submit(values: Record<string, string>) { (find(node => node.type === "form" && node.props.className === "paper-order-form").props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: { values } }); }
async function prepareOpening(values: Record<string, string>) {
  harness.api.mockResolvedValueOnce(preview); submit(values);
  await vi.waitFor(() => expect(elements(render()).some(node => node.type === PaperPreviewPanel)).toBe(true));
}
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); harness.encrypt.mockReset(); harness.notify.mockReset(); harness.interval.mockReset(); harness.listeners.clear();
  vi.stubGlobal("window", { setInterval: harness.interval, clearInterval: vi.fn() });
  vi.stubGlobal("document", { visibilityState: "visible", addEventListener: (name: string, callback: () => void) => harness.listeners.set(name, callback), removeEventListener: vi.fn() });
  vi.stubGlobal("crypto", { randomUUID: vi.fn(() => "stable-order-key") });
  vi.stubGlobal("FormData", class { constructor(private form: { values: Record<string, string> }) {} get(key: string) { return this.form.values[key] ?? null; } });
});
afterEach(() => vi.unstubAllGlobals());

describe("paper ledger request lifecycle", () => {
  it("reads selected account only and refreshes visible pages without overlap", async () => {
    await initial();
    expect(harness.api.mock.calls.map(call => call[0])).toEqual(["/api/v2/paper/accounts/account-a/snapshot", "/api/v2/paper/accounts/account-a/positions?limit=10&pagination=CURSOR&state=OPEN"]);
    expect(harness.interval.mock.calls[0][1]).toBe(5000);
    const tick = harness.interval.mock.calls[0][0];
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true }); tick(); expect(harness.api).toHaveBeenCalledTimes(2);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    let done!: (value: unknown) => void; harness.api.mockImplementationOnce(() => new Promise(resolve => { done = resolve; })).mockResolvedValueOnce({ items: [], has_more: false });
    tick(); tick(); expect(harness.api).toHaveBeenCalledTimes(4);
    done(snapshot); await vi.waitFor(() => expect(find(node => node.type === PageState).props.loading).toBe(false));
  });
  it("changes tabs and pages using bounded server queries", async () => {
    await initial(); click("委托记录"); setRows([order]);
    const effectIndex = harness.effects.length; render(); harness.effects[effectIndex + 1]();
    await vi.waitFor(() => expect(harness.api.mock.calls.at(-1)?.[0]).toContain("/orders?limit=10&pagination=CURSOR"));
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === PaperOrderCard)).toBe(true));
    (find(node => node.type === Pagination).props.onChange as (page: number) => void)(2);
    setRows([]); const index = harness.effects.length; render(); harness.effects[index + 1]();
    await vi.waitFor(() => expect(harness.api.mock.calls.at(-1)?.[0]).toContain("offset=20"));
  });
  it("never renders old-row types under a newly selected tab before its request completes", async () => {
    await initial([position]); expect(elements(render()).some(node => node.type === PaperPositionCard)).toBe(true);
    click("委托记录");
    expect(elements(render()).some(node => node.type === PaperPositionCard || node.type === PaperOrderCard)).toBe(false);
    harness.api.mockImplementation(() => new Promise(() => undefined));
    const index = harness.effects.length; render(); harness.effects[index + 1]();
    expect(find(node => node.type === PaperBalance).props.snapshot).toBe(snapshot);
  });
  it("uses the next opaque cursor rather than shifting offsets when newer records arrive", async () => {
    await initial();
    harness.api.mockResolvedValueOnce(snapshot).mockResolvedValueOnce({ items: [position], has_more: true, next_cursor: "owner-bound-cursor", snapshot_at: "2026-09-14T00:00:00Z" });
    click("刷新账本"); await vi.waitFor(() => expect(find(node => node.type === PageState).props.loading).toBe(false));
    (find(node => node.type === Pagination).props.onChange as (page: number) => void)(1);
    setRows([]); const index = harness.effects.length; render(); harness.effects[index+1]();
    await vi.waitFor(() => expect(harness.api.mock.calls.at(-1)?.[0]).toContain("pagination=CURSOR&cursor=owner-bound-cursor"));
    expect(harness.api.mock.calls.at(-1)?.[0]).not.toContain("offset=");
  });
  it("read-only users see records but receive no opening or position action authority", async () => {
    await initial([position]);
    expect(elements(render(false)).some(node => node.props.children === "模拟开仓")).toBe(false);
    expect(find(node => node.type === PaperPositionCard, false).props.canTrade).toBe(false);
    expect(harness.api.mock.calls.every(call => !call[1]?.method)).toBe(true);
  });
  it("retains one exact request after uncertain submission and locks repeat opening", async () => {
    await initial(); click("模拟开仓");
    (find(node => node.type === InstrumentPicker).props.onChange as (choice: unknown) => void)({ id: "btc", symbol: "BTC_USDT" });
    const values = { contracts: "2", order_type: "MARKET", side: "LONG", timeframe: "5m", stop_price: "90", take_profit_price: "120", time_in_force: "IOC", max_slippage_bps: "50", ttl_seconds: "3600" };
    await prepareOpening(values);
    harness.api.mockRejectedValueOnce(new ApiError(408, "REQUEST_TIMEOUT", "请求超时"));
    submit(values); submit(values);
    await vi.waitFor(() => expect(elements(render()).some(node => node.props.children === "重试同一请求")).toBe(true));
    const first = harness.api.mock.calls.find(call => call[0] === "/api/v2/paper/orders")!;
    expect(JSON.parse(first[1].body).idempotency_key).toBe("stable-order-key");
    expect(find(node => node.props.children === "模拟开仓").props.disabled).toBe(true);
    harness.api.mockResolvedValueOnce({ environment: "PAPER", replayed: true, accepted: true, order: { ...order, status: "PARTIALLY_FILLED", filled_contracts: 1, contracts: 2 }, fills: [] }); setRows([]);
    click("重试同一请求"); await vi.waitFor(() => expect(harness.notify).toHaveBeenCalledWith(expect.stringContaining("已核对原请求：部分成交")));
    const writes = harness.api.mock.calls.filter(call => call[0] === "/api/v2/paper/orders"); expect(writes).toHaveLength(2); expect(writes[0][1].body).toBe(writes[1][1].body);
  });
  it("sends a close request only after explicit quantity confirmation", async () => {
    await initial([position]);
    (find(node => node.type === PaperPositionCard).props.onClose as () => void)();
    expect(find(node => node.props.name === "contracts").props.max).toBe(3);
    expect(harness.api.mock.calls.filter(call => call[1]?.method === "POST")).toHaveLength(0);
    submit({ contracts: "4" }); expect(harness.api).toHaveBeenCalledTimes(2);
    harness.api.mockResolvedValueOnce({ environment: "PAPER", replayed: false, accepted: true, order: { ...order, effect: "CLOSE", status: "FILLED", filled_contracts: 2, contracts: 2 }, fills: [] }); setRows([]);
    submit({ contracts: "2", time_in_force: "IOC", max_slippage_bps: "50", ttl_seconds: "3600" });
    await vi.waitFor(() => expect(harness.notify).toHaveBeenCalledOnce());
    const write = harness.api.mock.calls.find(call => call[1]?.method === "POST")!;
    expect(write[0]).toBe("/api/v2/paper/positions/position-a:close"); expect(JSON.parse(write[1].body)).toMatchObject({ contracts: 2, order_type: "MARKET", idempotency_key: "stable-order-key" });
  });
  it("reauthenticates with encrypted password and never automatically places an order", async () => {
    await initial(); click("模拟开仓");
    (find(node => node.type === InstrumentPicker).props.onChange as (choice: unknown) => void)({ id: "btc", symbol: "BTC_USDT" });
    const values = { contracts: "1", stop_price: "90", take_profit_price: "120" };
    await prepareOpening(values);
    harness.api.mockRejectedValueOnce(new ApiError(403, "RECENT_AUTH_REQUIRED", "需验证身份"));
    submit(values);
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === Dialog && node.props.title === "确认登录身份")).toBe(true));
    harness.encrypt.mockResolvedValueOnce({ ciphertext: "encrypted-fixture" }); harness.api.mockResolvedValueOnce({ ok: true });
    const reset = vi.fn(); (find(node => node.type === "form" && node.props.className === "stack-form").props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: { values: { password: "test-only-password" }, reset } });
    await vi.waitFor(() => expect(harness.api.mock.calls.some(call => call[0] === "/auth/reauthenticate")).toBe(true));
    expect(reset).toHaveBeenCalledOnce(); expect(harness.api.mock.calls.filter(call => call[0] === "/api/v2/paper/orders")).toHaveLength(1);
    const request = harness.api.mock.calls.find(call => call[0] === "/auth/reauthenticate")!;
    expect(request[1].body).toContain("encrypted-fixture"); expect(request[1].body).not.toContain("test-only-password");
  });
  it("previews without creating an order and requires a separate confirmation", async () => {
    await initial(); click("模拟开仓");
    (find(node => node.type === InstrumentPicker).props.onChange as (choice: unknown) => void)({ id: "btc", symbol: "BTC_USDT" });
    await prepareOpening({ contracts: "1", stop_price: "90", take_profit_price: "120" });
    expect(harness.api.mock.calls.filter(call => call[0] === "/api/v2/paper/orders")).toHaveLength(0);
    expect(find(node => node.type === "button" && node.props.children === "确认模拟委托").props.disabled).toBe(false);
  });
  it("cannot submit a changed intent using an earlier preview", async () => {
    await initial(); click("模拟开仓");
    (find(node => node.type === InstrumentPicker).props.onChange as (choice: unknown) => void)({ id: "btc", symbol: "BTC_USDT" });
    await prepareOpening({ contracts: "1", stop_price: "90", take_profit_price: "120" });
    harness.api.mockResolvedValueOnce(preview);
    submit({ contracts: "2", stop_price: "90", take_profit_price: "120" });
    await vi.waitFor(() => expect(harness.api.mock.calls.filter(call => call[0] === "/api/v2/paper/orders:preview")).toHaveLength(2));
    expect(harness.api.mock.calls.some(call => call[0] === "/api/v2/paper/orders")).toBe(false);
  });
  it("blocks confirmation when the read-only preview reports insufficient budget", async () => {
    await initial(); click("模拟开仓");
    (find(node => node.type === InstrumentPicker).props.onChange as (choice: unknown) => void)({ id: "btc", symbol: "BTC_USDT" });
    harness.api.mockResolvedValueOnce({ ...preview, checks: { budget_available: false, reasons: ["ACCOUNT_RISK_LIMIT"] } });
    const values = { contracts: "1", stop_price: "90", take_profit_price: "120" };
    submit(values); await vi.waitFor(() => expect(elements(render()).some(node => node.type === PaperPreviewPanel)).toBe(true));
    expect(find(node => node.type === "button" && node.props.children === "确认模拟委托").props.disabled).toBe(true);
    submit(values); expect(harness.api.mock.calls.some(call => call[0] === "/api/v2/paper/orders")).toBe(false);
  });
});
