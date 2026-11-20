import { describe, expect, it } from "vitest";
import { initialRun, localInput, tradeCsv, zeroTradeReason, selectedRules, selectionError, type Run, type Report, type Trade } from "./researchModel";

describe("research report navigation", () => {
  it("preserves old single rules and validates multi-selection conflicts", () => {
    expect(selectedRules({ strategy: "trend" })).toEqual(["trend"]);
    expect(selectionError({ strategy: "composite", rule_ids: [] }, [])).toContain("至少");
    expect(selectionError({ strategy: "composite", rule_ids: ["trend", "reversion"], match_mode: "ALL" }, [])).toContain("互斥");
    expect(selectionError({ strategy: "composite", rule_ids: ["trend", "reversion"], match_mode: "ANY" }, [])).toBe("");
  });
  it("distinguishes missing signals, expired signals and a losing sample", () => {
    expect(zeroTradeReason({ full: { diagnostics: { signal_bars: 483, expired_signals: 100 } } } as Report)).toContain("483");
    expect(zeroTradeReason({ full: { diagnostics: { signal_bars: 0 } } } as Report)).toContain("没有共同入场信号");
  });
  it("opens latest completed report, not a blank page", () => {
    expect(initialRun([{ id: "failed", state: "FAILED" }, { id: "recent", state: "SUCCEEDED" }, { id: "older", state: "SUCCEEDED" }] as Run[])).toBe("recent");
    expect(initialRun([])).toBe("");
  });
  it("keeps an active job visible across reloads", () => {
    expect(initialRun([{ id: "done", state: "SUCCEEDED" }, { id: "pending", state: "RUNNING" }] as Run[])).toBe("pending");
  });
  it("loads date boundaries without shifting local timezone", () => {
    const iso = "2026-09-09T05:30:00Z";
    expect(new Date(localInput(iso)).getTime()).toBe(new Date(iso).getTime());
    expect(localInput(null)).toBe("");
  });
  it("does not describe missing historical availability as no strategy matches", () => {
    expect(zeroTradeReason({ quality_flags: ["AVAILABLE_AT_UNKNOWN"] } as Report)).toContain("可用时间未知");
  });
  it("exports actual signed losses and trading costs", () => {
    const csv = tradeCsv([{ entry_time: "2026-01-01", exit_time: "2026-01-02", side: 1, entry: 100, exit_price: 95, quantity: 1, stop: 95, target: 110, net_pnl: -6, fees: 1, funding: 0, exit_reason: "STOP" }] as Trade[]);
    expect(csv).toContain('"-6"');
    expect(csv).toContain('"止损"');
    expect(csv.split("\r\n")).toHaveLength(2);
  });
});
