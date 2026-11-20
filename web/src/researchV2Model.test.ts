import { describe, expect, it } from "vitest";
import { directionLabel, researchDate, researchNumber, shadowQuery } from "./researchV2Model";
import { progressPercent } from "./structureResearchModel";

describe("research presentation boundaries", () => {
  it("never turns missing or nonfinite returns into a fabricated zero win rate", () => {
    for (const value of [null, undefined, NaN, Infinity, "0"]) expect(researchNumber(value, true)).toBe("—");
    expect(researchNumber(0, true)).toBe("0.00%");
    expect(researchNumber(-.0123, true)).toBe("-1.23%");
  });
  it("uses epoch seconds and accepts the API ISO timestamp", () => {
    expect(researchDate(1704067200)).toBe(researchDate("2024-01-01T00:00:00Z"));
    expect(researchDate("invalid")).toBe("—");
    expect(researchDate(null)).toBe("—");
  });
  it("keeps observation cohorts independent and omits state for summary", () => {
    expect(new URLSearchParams(shadowQuery({ cohort: "SIGNAL", state: "CLOSED", instrument: "btc" }, 2)).get("offset")).toBe("40");
    const summary = new URLSearchParams(shadowQuery({ cohort: "DELIVERED", instrument: "btc" }));
    expect(summary.get("cohort")).toBe("DELIVERED");
    expect(summary.has("state")).toBe(false); expect(summary.has("offset")).toBe(false);
  });
  it("does not send unrecognized cohort or state values", () => {
    expect(shadowQuery({ cohort: "ALL", state: "BAD", instrument: "" })).toBe("");
  });
  it("maps frozen direction without inventing a trade side", () => {
    expect(directionLabel(1)).toBe("多"); expect(directionLabel("SHORT")).toBe("空"); expect(directionLabel(null)).toBe("待定");
  });
  it("clamps processing progress but never claims completion for unknown totals", () => {
    expect(progressPercent(undefined)).toBeNull();
    expect(progressPercent({ processed_bars: 20, total_bars: 0, phase: "SNAPSHOT" })).toBeNull();
    expect(progressPercent({ processed_bars: 50, total_bars: 200, phase: "REPLAY" })).toBe(25);
    expect(progressPercent({ processed_bars: 250, total_bars: 200, phase: "REPLAY" })).toBe(100);
  });
});
