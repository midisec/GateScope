import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ChartStudiesPicker, type ChartStudiesPickerProps } from "./ChartStudiesPicker";
import { defaultChartStudiesSettings, normalizeChartStudiesSettings } from "./chartStudies";
import type { CoinglassBundle, CoinglassSeries } from "./coinglassModel";

vi.mock("./useCoinglassClock", () => ({ useCoinglassClock: () => 180182 }));
const NOW = 180182;
function source(extra: Partial<CoinglassSeries> = {}): CoinglassSeries {
  return { id: "oi", label: "OI", unit: "USD", unit_status: "VERIFIED", population: "TEST", native_timeframe: "1m",
    status: "READY", quality_flags: [], points: [
      { time: 180060, close_time: 180120, available_at: 180120, value: 100 },
      { time: 180120, close_time: 180180, available_at: 180180, value: 110 },
    ], ...extra };
}
function bundle(extra: Partial<CoinglassBundle> = {}): CoinglassBundle {
  return { status: "READY", instrument_id: "sol", timeframe: "1m", scope: null, match: { status: "EXACT" },
    as_of: 180180, fetched_at: NOW, expires_at: NOW + 60, series: [source()], ...extra };
}
function render(extra: Partial<ChartStudiesPickerProps> = {}) {
  return renderToStaticMarkup(<ChartStudiesPicker settings={defaultChartStudiesSettings()} onChange={vi.fn()} onClose={vi.fn()} onRefresh={vi.fn()} {...extra} />);
}
function option(html: string, title: string) {
  const row = html.split('<label class="chart-study-option').find(part => part.includes(`aria-label="显示 ${title}"`));
  if (!row) throw new Error(`Missing option ${title}`);
  return row.split("</label>")[0];
}

describe("curated chart study picker rendering", () => {
  it("offers seven unchecked multi-select studies without fetching on open", () => {
    const refresh = vi.fn(), changed = vi.fn(), html = render({ onRefresh: refresh, onChange: changed });
    expect(html.match(/type="checkbox"/g)).toHaveLength(7);
    expect(html).not.toContain('checked=""');
    expect(html).toContain("按需读取"); expect(html).toContain("支持多选");
    expect(html).toContain('aria-label="搜索精选指标"');
    expect(html).toContain('aria-label="刷新 Coinglass 数据" disabled=""');
    expect(refresh).not.toHaveBeenCalled(); expect(changed).not.toHaveBeenCalled();
  });
  it.each(["NO_CONFIG", "NOT_CONFIGURED", "DISABLED"])("allows choosing studies before source configuration: %s", status => {
    const html = render({ bundle: bundle({ status, series: [] }) });
    expect(html).toContain('href="/integrations/data-sources"');
    expect(html).not.toContain('type="checkbox" disabled');
    expect(option(html, "OI 美元估值")).toContain(status === "DISABLED" ? "已停用" : "未配置");
    expect(option(html, "OI 美元估值")).not.toContain('availability ready');
  });
  it("shows queued data honestly while retaining a checked selection", () => {
    const settings = normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["oi"] });
    const html = render({ settings, bundle: bundle({ status: "QUEUED", series: [], refresh_state: "QUEUED" }) });
    expect(option(html, "OI 美元估值")).toContain('checked=""');
    expect(option(html, "OI 美元估值")).toContain("已排队");
    expect(option(html, "OI 美元估值")).not.toContain('availability ready');
  });
  it("uses the curated validator for native and derived availability", () => {
    const html = render({ bundle: bundle() });
    expect(option(html, "OI 美元估值")).toContain('availability ready');
    expect(option(html, "OI美元估值变化率")).toContain('availability ready');
    expect(option(html, "资金费率")).toContain("暂无数据");
    const single = render({ bundle: bundle({ series: [source({ points: source().points.slice(1) })] }) });
    expect(option(single, "OI 美元估值")).toContain('availability ready');
    expect(option(single, "OI美元估值变化率")).toContain("暂无数据");
  });
  it("distinguishes historical caches from current or invalid data", () => {
    const stale = render({ bundle: bundle({ expires_at: NOW - 1 }) });
    expect(option(stale, "OI 美元估值")).toContain("历史缓存");
    for (const extra of [{ match: { status: "AMBIGUOUS" } }, { series: [source({ unit_status: "UNKNOWN" })] }, { series: [source({ native_timeframe: "5m" })] }]) {
      expect(option(render({ bundle: bundle(extra) }), "OI 美元估值")).toContain("暂无数据");
    }
  });
  it("disables refresh for the actual Retry-After window without blocking selection", () => {
    const html = render({ bundle: bundle({ status: "RATE_LIMITED", series: [], retry_at: NOW + 7 }) });
    expect(html).toContain("7 秒后可刷新"); expect(html).toContain("等待额度");
    expect(html).toContain('aria-label="刷新 Coinglass 数据" disabled=""');
    expect(option(html, "OI 美元估值")).not.toContain("disabled");
  });
  it("keeps VPVR parameters compact and method explanations collapsed", () => {
    const settings = normalizeChartStudiesSettings({ version: 1, vpvr: { enabled: true, rows: 96, valueAreaPercent: 85, widthPercent: 30 } });
    const html = render({ settings });
    expect(html).toContain("VPVR 显示参数"); expect(html).toContain('value="96" selected=""');
    expect(html).toContain('aria-label="VPVR 价值区间百分比" min="50" max="95"');
    expect(html).toContain('aria-label="VPVR 侧边宽度百分比" min="10" max="40"');
    expect(html).toContain("细周期估算"); expect(html).toContain("不是逐笔成交");
    expect(html).toContain('<details class="chart-studies-method">');
    expect(html).not.toContain("<details open");
    expect(html).toContain("不代表净资金流入");
  });
  it("exposes errors without erasing selections or fabricating readiness", () => {
    const html = render({ settings: normalizeChartStudiesSettings({ version: 1, coinglassMetrics: ["funding"] }), error: "连接暂时不可用" });
    expect(html).toContain('role="alert">连接暂时不可用');
    expect(option(html, "资金费率")).toContain('checked=""');
    expect(option(html, "资金费率")).not.toContain('availability ready');
  });
});
