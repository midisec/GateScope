import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PerformanceScreen, screeningLabel, observationNotes } from "./PerformanceScreen";
import type { Report, Screening } from "./researchModel";

it("never labels a missing report as eligible", () => {
  expect(screeningLabel()).toContain("未计算绩效");
  expect(renderToStaticMarkup(<PerformanceScreen report={{} as Report} />)).toContain("真实执行仍需账户权限与执行认证");
});
it("preserves old metrics as historical reference without retaining deployment gates", () => {
  const screening = { status: "REJECTED", shortlisted: false, sample_passed: false, deployable: false, policy_version: "performance-screen-1", basis: "历史筛选",
    daily_observations: 90, minimum_daily_observations: 90, trade_count: 12, minimum_trades: 30, reason_codes: [],
    checks: [{ gate_code: "OOS_SHARPE", metric: "sharpe_ratio", label: "夏普比率", value: 1.5, threshold: 1.5, operator: ">", passed: false }] } as Screening;
  const html = renderToStaticMarkup(<PerformanceScreen report={{ screening, range: { validation_from: "2026-01-01" } } as Report} />);
  expect(html).toContain("参考范围外"); expect(html).toContain("样本偏少"); expect(html).toContain("&gt; 1.5");
  expect(html).toContain("当前已取消绩效门槛"); expect(html).not.toContain("淘汰"); expect(html).not.toContain("严格门槛");
  expect(screeningLabel({ ...screening, shortlisted: true })).toContain("历史参考");
});
it("does not call observational shortlisting a deployment or live authorization", () => {
  const screening = { status: "OBSERVE_ONLY", thresholds_enforced: false, shortlisted: true, deployable: false,
    policy_version: "performance-observation-2", basis: "绩效参考", sample_passed: false, reason_codes: [], observation_codes: ["OOS_SHARPE"],
    daily_observations: 20, minimum_daily_observations: 90, trade_count: 4, minimum_trades: 30,
    checks: [{ gate_code: "OOS_SHARPE", metric: "sharpe_ratio", label: "夏普比率", value: -.5, threshold: 1.5, operator: ">", passed: false, enforced: false }] } as Screening;
  const html = renderToStaticMarkup(<PerformanceScreen report={{ screening, range: { validation_from: "2026-01-01" } } as Report} />);
  expect(screeningLabel(screening)).toBe("绩效观察 · 不限制部署"); expect(observationNotes(screening)).toContain("夏普比率在参考范围外");
  expect(html).toContain("账户权限、策略执行认证和安全检查"); expect(html).toContain("-0.5"); expect(html).not.toContain("四项达标");
  expect(html).not.toContain("可实盘"); expect(html).not.toContain("LIVE 可用");
});
