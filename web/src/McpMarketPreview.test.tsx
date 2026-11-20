import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { McpMarketPreview, McpSnapshotView, mcpSnapshotArguments, normalizeMcpClient, MCP_CALL_EXAMPLE, type McpMarketSnapshot } from "./McpWorkspace";

export const snapshot: McpMarketSnapshot = {
  instrument: { id: "btc", symbol: "BTC_USDT", market: "futures_usdt" }, timeframe: "15m", as_of: "2026-09-13T00:00:00Z",
  candles: [{ close: "100" }], latest_closed: { close: "100", close_time: "2026-09-13T00:00:00Z" },
  realtime: { status: "CURRENT", used_for_indicators: false },
  indicators: [{ indicator_id: "rsi", ready: false, parameters: { period: 14 }, values: { rsi: null }, bars_evaluated: 1 }],
  quality: { stale: false, missing_bars: 0, contiguous_indicator_bars: 1 }, quality_flags: [],
  coinglass: { status: "PARTIAL", instrument_id: "btc", timeframe: "15m", match: { status: "EXACT" },
    series: [{ id: "oi", label: "OI 持仓量", unit: "USD", unit_status: "VERIFIED", native_timeframe: "15m", population: "OPEN_INTEREST_MARK_VALUE", quality_flags: [], status: "READY", points: [{ time: 60, close_time: 960, available_at: 961, value: 1000 }] },
      { id: "funding", label: "资金费率", unit: "PERCENT", unit_status: "UNKNOWN", native_timeframe: "15m", population: "FUNDING_RATE", quality_flags: [], status: "MISMATCH", points: [] }] },
};

describe("MCP market preview rendering", () => {
  it("normalizes the real create response without a token list", () => {
    const created = { id: "new", name: "owner client", status: "ACTIVE" as const, etag: 1 };
    expect(normalizeMcpClient(created)).toEqual({ ...created, tokens: [] });
    expect(created).not.toHaveProperty("tokens");
  });
  it("documents per-call symbol and period, not client configuration", () => {
    expect(MCP_CALL_EXAMPLE.arguments).toEqual({ symbol: "BTC_USDT", market: "futures:usdt", timeframe: "15m", bar_limit: 500 });
    const html = renderToStaticMarkup(<McpMarketPreview notify={() => undefined} />);
    expect(html).toContain("不保存币种或周期");
  });
  it("copies bounded tool arguments without tokens or account data", () => {
    expect(mcpSnapshotArguments("btc", "1h", 500)).toEqual({ name: "market_analysis_snapshot", arguments: { instrument_id: "btc", timeframe: "1h", bar_limit: 500 } });
  });
  it("starts with explicit selection and a disabled read action", () => {
    const html = renderToStaticMarkup(<McpMarketPreview notify={() => undefined} />);
    expect(html).toContain("搜索并选择币种"); expect(html).toContain("MCP 输出周期"); expect(html).toContain("MCP 输出条数");
    expect(html).toContain("不会向 OpenAI 发送数据"); expect(html).toMatch(/disabled=""[^>]*>读取最新缓存/);
  });
  it("shows true indicator readiness, unit quarantine and display-only observation", () => {
    const html = renderToStaticMarkup(<McpSnapshotView snapshot={snapshot} onCopy={() => undefined} />);
    expect(html).toContain("样本不足"); expect(html).toContain("部分数据可用"); expect(html).toContain("单位待核实");
    expect(html).toContain("暂无已核实数据"); expect(html).toContain("实时，仅作观察"); expect(html).toContain("完整分析数据 JSON");
  });
  it("does not hide stale cache and missing bars", () => {
    const html = renderToStaticMarkup(<McpSnapshotView snapshot={{ ...snapshot, quality: { stale: true, missing_bars: 3, contiguous_indicator_bars: 1 }, realtime: { status: "STALE", used_for_indicators: false }, coinglass: { status: "NOT_CACHED", series: [] } }} onCopy={() => undefined} />);
    expect(html).toContain("行情缓存已过期"); expect(html).toContain("缺失 3 根"); expect(html).toContain("等待后台采集");
  });
  it("shows retained data as historical reference with source times and cooldown", () => {
    const html = renderToStaticMarkup(<McpSnapshotView snapshot={{ ...snapshot, coinglass: { ...snapshot.coinglass, status: "STALE", cache_retained: true,
      retry_at: Date.now() / 1000 + 120, refresh_status: "RATE_LIMITED", last_attempt_at: 1800000000,
      series: snapshot.coinglass.series!.map(series => ({ ...series, status: series.id === "oi" ? "STALE" : "DEFERRED" })) } }} onCopy={() => undefined} />);
    expect(html).toContain("缓存参考"); expect(html).toContain("数据截至"); expect(html).toContain("获取于");
    expect(html).toContain("数据源冷却中"); expect(html).toContain("等待下次采集");
    expect(html).toContain("历史缓存不能当作最新交易证据");
  });
  it("does not display values from another contract or an unverified unit", () => {
    const render = (context: McpMarketSnapshot["coinglass"]) => renderToStaticMarkup(<McpSnapshotView snapshot={{ ...snapshot, coinglass: context }} onCopy={() => undefined} />).split("完整分析数据 JSON")[0];
    const wrong = render({ ...snapshot.coinglass, instrument_id: "eth" });
    expect(wrong).toContain("维度或单位未核实"); expect(wrong).not.toContain("1,000 USD");
    const unknown = render({ ...snapshot.coinglass, series: snapshot.coinglass.series!.map(series => ({ ...series, unit_status: "UNKNOWN" })) });
    expect(unknown).not.toContain("1,000 USD"); expect(unknown).toContain("暂无已核实数据");
  });
  it("shows all five metric slots and independent refresh progress without hiding ready values", () => {
    const html = renderToStaticMarkup(<McpSnapshotView snapshot={{ ...snapshot, coinglass: { ...snapshot.coinglass,
      refresh_state: "COLLECTING", queue_age_seconds: 73, next_poll_after_seconds: 3 } }} onCopy={() => undefined} />);
    expect(html).toContain("已就绪 1/5"); expect(html).toContain("后台采集中"); expect(html).toContain("1,000 USD");
    expect(html).toContain("多单实际爆仓"); expect(html).toContain("空单实际爆仓"); expect(html).toContain("全账户多空比");
    expect(html).toContain("建议 3 秒后检查进度"); expect(html).toContain("已等待 1 分 13 秒");
    expect(html).toContain("不是完成时间");
  });
});
