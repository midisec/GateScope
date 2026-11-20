import type { TestnetDrill } from "./types";

export function drillStateLabel(state: TestnetDrill["state"]) {
  return state === "RECONCILIATION_PENDING" ? "对账中，暂禁开仓" : state;
}
export function drillNeedsRefresh(state: TestnetDrill["state"]) {
  return ["QUEUED", "PROCESSING", "RECONCILIATION_PENDING", "PROTECTED", "ENTRY_UNKNOWN"].includes(state);
}
export const reconciliationNotice = "交易所状态尚未确认，账户暂停新开仓。后台继续对账和管理已有风险；此状态不表示已确认止损有效或已经平仓。";
export function executionObservation(code: string) {
  return ({
    PROTECTION_QUERY_RETRY_PENDING: "保护单查询暂不可用，等待后台再次核对",
    ENTRY_FACT_NOT_YET_PROVABLE: "尚无法核实入场事实，等待对账",
    ENTRY_FINALITY_PENDING: "入场订单终态尚未确认",
    DEGRADED_PROTECTION_EXIT_RECONCILIATION_PENDING: "保护异常后的退出仍待对账",
    PROTECTION_DEADLINE_EXCEEDED: "保护核对已超过时限，请核查交易所状态",
  } as Record<string,string>)[code] ?? code;
}
