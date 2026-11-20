import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AdminConsole, AdminCountGrid, AdminLoginTrend, AdminMemberDetail, AdminPhysicalResources } from "./AdminConsole";
import { adminMember, adminResources, adminSession } from "./adminConsole.testFixtures";
import { adminBytes, adminCreatePayload, adminListPath, adminNumber, adminTime, adminView } from "./adminConsoleModel";
afterEach(() => vi.unstubAllGlobals());
const values = (overrides: Record<string, string | undefined> = {}) => ({ get: (name: string) => ({ username: "  member-one  ", initial_password: "long-password-fixture", role: "RESEARCHER", ...overrides } as Record<string, string | undefined>)[name] ?? null });
describe("independent administration presentation", () => {
  it.each(["overview", "users", "resources", "audit"])("renders the independent %s route without trading workbench controls", view => {
    vi.stubGlobal("location", { pathname: `/admin/${view}` });
    const html = renderToStaticMarkup(<AdminConsole session={adminSession} />);
    expect(html).toContain("后台导航"); expect(html).toContain(`href="/admin/${view}" aria-current="page"`);
    expect(html).toContain("返回交易台"); expect(html).not.toContain("模拟开仓"); expect(html).not.toContain("行情图表菜单");
  });
  it("blocks non-owners and incomplete authentication without fetching or exposing management forms", () => {
    for (const session of [{ ...adminSession, user: { ...adminSession.user, role: "OPERATOR" } }, { ...adminSession, mfa_required: true }]) {
      const html = renderToStaticMarkup(<AdminConsole session={session} />); expect(html).toContain("无法进入管理后台"); expect(html).not.toContain("后台导航"); expect(html).not.toContain("创建用户");
    }
  });
  it("does not claim an optional MFA policy is a verified MFA session", () => {
    const html = renderToStaticMarkup(<AdminMemberDetail member={{ ...adminMember, mfa_required: false }} />);
    expect(html).toContain("非强制（不代表已完成 MFA）"); expect(html).toContain("有效会话"); expect(html).not.toContain("password");
  });
  it("separates shared database estimates and host disk from user accounting and preserves unavailable values", () => {
    const html = renderToStaticMarkup(<AdminPhysicalResources physical={adminResources.physical} />);
    expect(html).toContain("全库共享 · 非用户占用"); expect(html).toContain("主机共享 · 非用户占用"); expect(html).toContain("不是精确计数"); expect(html).toContain("暂不可用"); expect(html).toContain("1.0 GiB"); expect(html).toContain("外置压缩数据段未完整归因");
    const unavailable = renderToStaticMarkup(<AdminPhysicalResources physical={{ ...adminResources.physical, database_bytes: null, disk: { ...adminResources.physical.disk, available: false } }} />);
    expect(unavailable).toContain("文件系统统计暂不可用"); expect(unavailable).not.toContain("50.0%");
  });
  it("keeps exact daily login counts accessible with UTC labels, including zero-event days", () => {
    const html = renderToStaticMarkup(<AdminLoginTrend trend={{ timezone: "UTC", from_date: "2026-09-10", to_date: "2026-09-16", items: [{ date: "2026-09-10", success: 2, denied: 1, failed: 0 }, { date: "2026-09-11", success: 0, denied: 0, failed: 0 }] }} />);
    expect(html).toContain("近 7 天密码登录"); expect(html).toContain("不含 MFA 结果"); expect(html).toContain("按 UTC 日期"); expect(html).toContain("成功 2，拒绝 1，失败 0"); expect(html).toContain("2026-09-11"); expect(html).not.toMatch(/NaN|Infinity/);
    expect(renderToStaticMarkup(<AdminCountGrid counts={{ watchlists: null, paper_accounts: 0 }} />)).toContain("<strong>—</strong>");
  });
});
describe("administration request models", () => {
  it("uses bounded server pagination with encoded literal search and independent audit filters", () => {
    expect(adminListPath("users", { page: 2, q: " x&role=OWNER ", role: "VIEWER", status: "ALL" })).toBe("/api/v2/admin/users?page=2&page_size=20&q=x%26role%3DOWNER&role=VIEWER");
    expect(adminListPath("audit", { page: 0, action: "user.create", outcome: "DENIED", q: "unused" })).toBe("/api/v2/admin/audit?page=1&page_size=20&action=user.create&outcome=DENIED");
    expect(new URLSearchParams(adminListPath("users", { page: 1, q: "a".repeat(200) }).split("?")[1]).get("q")).toHaveLength(80);
  });
  it("recognizes only exact administration subroutes", () => {
    expect(adminView("/admin/")).toBe("overview"); expect(adminView("/admin/users/")).toBe("users"); expect(adminView("/admin/users/other")).toBeUndefined(); expect(adminView("/markets/chart")).toBeUndefined();
  });
  it("validates passwords by both the current API character floor and UTF-8 byte ceiling, with no MFA policy mutation", () => {
    expect(adminCreatePayload(values())).toEqual({ username: "member-one", initial_password: "long-password-fixture", role: "RESEARCHER" });
    for (const invalid of [{ username: "ab" }, { username: "name with spaces" }, { username: "-user" }, { username: "_user" }, { username: ".user" }, { role: "ADMIN" }, { initial_password: "密".repeat(86) }, { initial_password: "密".repeat(15) }, { initial_password: "x".repeat(257) }]) expect(() => adminCreatePayload(values(invalid))).toThrow();
    expect(adminCreatePayload(values({ username: "ｕｓｅｒ" })).username).toBe("ｕｓｅｒ");
    expect(adminCreatePayload(values({ initial_password: "密".repeat(16) })).initial_password).toHaveLength(16);
  });
  it("does not format unknown disk or timestamps as zero", () => {
    expect(adminBytes(null)).toBe("—"); expect(adminBytes(-1)).toBe("—"); expect(adminBytes("2048")).toBe("2.0 KiB"); expect(adminNumber(0)).toBe("0"); expect(adminNumber(undefined)).toBe("—"); expect(adminTime("bad")).toBe("—");
  });
});
