import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { AdminAuditPage, AdminUsersPage } from "./AdminConsole";
import { adminMember, adminMembersPage, adminSession } from "./adminConsole.testFixtures";
import { ApiError } from "./api";
import { Dialog, Pagination } from "./ui";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, api: vi.fn(), encrypt: vi.fn(), ask: vi.fn(), resource: vi.fn(), page: undefined as unknown, loading: false, error: "" }));
vi.mock("react", async importOriginal => ({ ...await importOriginal<typeof import("react")>(),
  useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
  useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
}));
vi.mock("./api", async importOriginal => ({ ...await importOriginal<typeof import("./api")>(), api: harness.api }));
vi.mock("./submissionCrypto", () => ({ encryptSubmission: harness.encrypt, wrapEncryptedSubmission: (value: unknown) => ({ encrypted_submission: value }) }));
vi.mock("./useResearchResource", () => ({ useResearchResource: (path: string, refresh?: number) => { harness.resource(path, refresh); return { data: harness.page, loading: harness.loading, error: harness.error, retry: vi.fn() }; } }));
vi.mock("./ui", async importOriginal => ({ ...await importOriginal<typeof import("./ui")>(), useConfirm: () => ({ ask: harness.ask, dialog: null }) }));
type Element = ReactElement<Record<string, unknown>>;
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean) { const found = elements(node).find(predicate); if (!found) throw new Error("Element not found"); return found; }
function render() { harness.cursor = 0; return AdminUsersPage({ session: adminSession }); }
function click(label: string) { return (find(render(), value => value.type === "button" && value.props.children === label).props.onClick as () => unknown)(); }
function form(field: string, values: Record<string, string>) {
  const input = { value: values.initial_password ?? "" }, element = { values, elements: { namedItem: () => input }, reset: vi.fn() };
  const tree = render(), scope = field === "q" ? tree : find(tree, value => value.type === Dialog && elements(value.props.children as ReactNode).some(child => child.props.name === field));
  const node = find(scope, value => value.type === "form" && elements(value.props.children as ReactNode).some(child => child.props.name === field));
  const submit = () => (node.props.onSubmit as (event: unknown) => Promise<unknown>)({ preventDefault() {}, currentTarget: element });
  return { element, input, submit };
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.api.mockReset(); harness.encrypt.mockReset(); harness.ask.mockReset(); harness.resource.mockReset(); harness.page = adminMembersPage; harness.loading = false; harness.error = "";
  harness.ask.mockResolvedValue(true); harness.encrypt.mockResolvedValue({ ciphertext: "encrypted-fixture" });
  vi.stubGlobal("FormData", class { constructor(private form: { values: Record<string, string> }) {} get(name: string) { return this.form.values[name] ?? null; } });
});
afterEach(() => vi.unstubAllGlobals());

describe("administration user workflows", () => {
  it("requests server-side filters and resets to page one on search", () => {
    let node = render(); (find(node, value => value.type === Pagination).props.onChange as (page: number) => void)(1); render();
    expect(harness.resource).toHaveBeenLastCalledWith("/api/v2/admin/users?page=2&page_size=20", 0);
    form("q", { q: "member", role: "OPERATOR", status: "LOCKED" }).submit(); render();
    expect(harness.resource).toHaveBeenLastCalledWith("/api/v2/admin/users?page=1&page_size=20&q=member&role=OPERATOR&status=LOCKED", 0);
  });
  it("submits an owner-bound encrypted user creation, clears the password field and never invents MFA state", async () => {
    harness.api.mockResolvedValue({ id: "created" }); click("创建用户");
    const created = form("initial_password", { username: "member-new", initial_password: "password-test-fixture", role: "RESEARCHER" });
    const first = created.submit(), repeated = created.submit(); await first; await repeated;
    expect(created.input.value).toBe(""); expect(harness.encrypt).toHaveBeenCalledTimes(1);
    expect(harness.encrypt).toHaveBeenCalledWith("tenant:tenant-a:user_create", { username: "member-new", initial_password: "password-test-fixture", role: "RESEARCHER" });
    expect(harness.api).toHaveBeenCalledExactlyOnceWith("/api/v2/users", { method: "POST", body: JSON.stringify({ encrypted_submission: { ciphertext: "encrypted-fixture" } }) });
    expect(harness.api.mock.calls[0][1].body).not.toContain("password-test-fixture");
    expect(elements(render()).some(value => value.type === Dialog)).toBe(false);
  });
  it("never falls back to cleartext if browser encryption fails", async () => {
    harness.encrypt.mockRejectedValue(new Error("加密不可用")); click("创建用户");
    await form("initial_password", { username: "member-new", initial_password: "password-test-fixture", role: "RESEARCHER" }).submit();
    expect(harness.api).not.toHaveBeenCalled(); expect(elements(render()).some(value => value.props.children === "加密不可用")).toBe(true);
  });
  it("requires explicit extra confirmation before granting owner authority", async () => {
    harness.ask.mockResolvedValue(false); click("创建用户");
    await form("initial_password", { username: "another-owner", initial_password: "password-test-fixture", role: "OWNER" }).submit();
    expect(harness.ask).toHaveBeenCalledWith("授予所有者权限", expect.stringContaining("敏感接入")); expect(harness.encrypt).not.toHaveBeenCalled(); expect(harness.api).not.toHaveBeenCalled();
  });
  it("edits only membership role/status with its current ETag after confirmation", async () => {
    harness.api.mockResolvedValue({}); click("编辑"); await form("role", { role: "OPERATOR", status: "REVOKED" }).submit();
    expect(harness.ask).toHaveBeenCalledWith("确认成员权限变更", expect.stringContaining("停用"));
    expect(harness.api).toHaveBeenCalledExactlyOnceWith("/api/v2/memberships/membership-a", { method: "PATCH", headers: { "If-Match": '"4"' }, body: JSON.stringify({ role: "OPERATOR", status: "REVOKED" }) });
  });
  it("cancels a revoke without writing and prevents self-session revocation", async () => {
    harness.ask.mockResolvedValue(false); click("注销会话"); await settle(); expect(harness.api).not.toHaveBeenCalled();
    harness.page = { ...adminMembersPage, items: [{ ...adminMember, is_self: true }] };
    expect(find(render(), value => value.type === "button" && value.props.children === "注销会话").props.disabled).toBe(true);
  });
  it("revokes only the selected member's sessions after a separate confirmation", async () => {
    harness.api.mockResolvedValue({ revoked_sessions: 2 }); click("注销会话"); await settle();
    expect(harness.ask).toHaveBeenCalledWith("注销该用户的会话", expect.stringContaining("test_member"));
    expect(harness.api).toHaveBeenCalledExactlyOnceWith("/api/v2/admin/users/member/sessions:revoke", { method: "POST" });
  });
  it("offers password reauthentication on expiry without automatically replaying a write", async () => {
    harness.api.mockRejectedValueOnce(new ApiError(403, "RECENT_AUTH_REQUIRED", "需要近期认证"));
    click("创建用户"); await form("initial_password", { username: "member-new", initial_password: "password-test-fixture", role: "RESEARCHER" }).submit();
    expect(elements(render()).some(value => value.type === Dialog && value.props.title === "确认管理身份")).toBe(true);
    harness.api.mockResolvedValueOnce({ reauthenticated: true });
    const reauth = form("password", { password: "owner-password-fixture" }); await reauth.submit();
    expect(reauth.element.reset).toHaveBeenCalledOnce(); expect(harness.encrypt).toHaveBeenLastCalledWith("user:owner:reauthenticate", { password: "owner-password-fixture" });
    expect(harness.api).toHaveBeenCalledTimes(2); expect(harness.api.mock.calls[1][0]).toBe("/auth/reauthenticate");
    expect(elements(render()).some(value => value.type === Dialog && value.props.title === "确认管理身份")).toBe(false);
  });
  it("does not label role rejection or last-owner protection as an expired password", async () => {
    harness.api.mockRejectedValue(new ApiError(409, "LAST_OWNER_REQUIRED", "不能移除最后一个所有者"));
    click("编辑"); await form("role", { role: "VIEWER", status: "ACTIVE" }).submit();
    expect(elements(render()).some(value => value.type === Dialog && value.props.title === "确认管理身份")).toBe(false);
    expect(elements(render()).some(value => value.props.children === "不能移除最后一个所有者")).toBe(true);
  });
  it("keeps an uncertain network write visible for checking instead of automatically retrying", async () => {
    harness.api.mockRejectedValue(new ApiError(0, "NETWORK_ERROR", "网络中断，请先刷新记录")); click("创建用户");
    await form("initial_password", { username: "member-new", initial_password: "password-test-fixture", role: "RESEARCHER" }).submit();
    expect(harness.api).toHaveBeenCalledOnce(); expect(elements(render()).some(value => value.props.children === "网络中断，请先刷新记录")).toBe(true);
  });
  it("explains recovery from an ETag conflict without retrying stale role changes", async () => {
    harness.api.mockRejectedValue(new ApiError(412, "PRECONDITION_FAILED", "成员版本已变化")); click("编辑");
    await form("role", { role: "OPERATOR", status: "ACTIVE" }).submit();
    expect(harness.api).toHaveBeenCalledOnce();
    expect(elements(render()).some(value => typeof value.props.children === "string" && value.props.children.includes("刷新用户列表后重新编辑"))).toBe(true);
  });
});
describe("administration audit workflow", () => {
  it("uses independent server filtering and bounded pagination", () => {
    harness.page = { ...adminMembersPage, items: [] }; harness.cursor = 0; const tree = AdminAuditPage();
    const search = find(tree, value => value.type === "form");
    (search.props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: { values: { action: "mfa.verify", outcome: "DENIED" } } });
    harness.cursor = 0; AdminAuditPage(); expect(harness.resource).toHaveBeenLastCalledWith("/api/v2/admin/audit?page=1&page_size=20&action=mfa.verify&outcome=DENIED", undefined);
  });
});
