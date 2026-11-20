import { expect, it } from "vitest";
import { drillNeedsRefresh, drillStateLabel, executionObservation, reconciliationNotice } from "./executionStatus";

it("keeps reconciling drills visible as uncertain and refreshable", () => {
  expect(drillStateLabel("RECONCILIATION_PENDING")).toBe("对账中，暂禁开仓");
  expect(drillNeedsRefresh("RECONCILIATION_PENDING")).toBe(true);
  expect(drillNeedsRefresh("PROTECTED")).toBe(true);
  expect(drillNeedsRefresh("CLOSED_PROTECTED")).toBe(false);
  expect(reconciliationNotice).toContain("不表示已确认止损有效或已经平仓");
  expect(executionObservation("ENTRY_FINALITY_PENDING")).toContain("尚未确认");
});
