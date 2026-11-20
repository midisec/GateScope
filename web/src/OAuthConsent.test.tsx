import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "./api";
import { OAuthConsent, chatGptOAuthRedirect, oauthConsentRequest, type OAuthConsentDetails } from "./OAuthConsent";
import { McpOAuthGuide } from "./McpWorkspace";
import { Dialog, PageState } from "./ui";
import type { Session } from "./types";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => void)[], api: vi.fn(), encrypt: vi.fn(), assign: vi.fn(), expired: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useCallback(fn: unknown) { return fn; },
    useEffect(effect: () => void) { harness.effects.push(effect); },
    useRef(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: initial }; return harness.slots[index]; },
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
  };
});
vi.mock("./api", async importOriginal => ({ ...await importOriginal<typeof import("./api")>(), api: harness.api }));
vi.mock("./submissionCrypto", () => ({ encryptSubmission: harness.encrypt, wrapEncryptedSubmission: (envelope: unknown) => ({ encrypted_submission: envelope }) }));

const requestId = "oauth-request-fixture-12345678901234567890123";
const session: Session = { user: { id: "owner-user", tenant_id: "tenant", username: "owner", role: "OWNER" }, expires_at: "2099-01-01T00:00:00Z", mfa_required: false, auth_strength: "MFA" };
const details: OAuthConsentDetails = { client_name: "ChatGPT", scopes: ["mcp:connect", "market:read", "analysis:read"], resource: "https://example.test/mcp", expires_at: "2099-01-01T00:00:00Z" };
type Element = ReactElement<Record<string, unknown>>;
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean): Element { const found = elements(node).find(predicate); if (!found) throw new Error("Element not found"); return found; }
function render(id: string | null = requestId) { harness.cursor = 0; return OAuthConsent({ session, requestId: id, onSessionExpired: harness.expired }); }
function button(label: string) { return find(render(), node => node.type === "button" && node.props.children === label); }
function click(label: string) { (button(label).props.onClick as () => void)(); }
async function load(value: OAuthConsentDetails = details) {
  harness.api.mockResolvedValueOnce(value); render(); harness.effects[0]();
  await vi.waitFor(() => expect(elements(render()).some(node => node.props.className === "oauth-permissions")).toBe(true));
}
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); harness.encrypt.mockReset(); harness.assign.mockReset(); harness.expired.mockReset();
  vi.stubGlobal("window", { location: { origin: "https://example.test", assign: harness.assign }, setTimeout: vi.fn(), clearTimeout: vi.fn() });
  vi.stubGlobal("FormData", class { constructor(private form: { values: Record<string, string> }) {} get(key: string) { return this.form.values[key] ?? null; } });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("OAuth consent boundaries", () => {
  it("accepts exactly one bounded opaque request without accepting an arbitrary return URL", () => {
    expect(oauthConsentRequest(`?request=${requestId}`)).toBe(requestId);
    for (const search of ["", "?request=short", `?request=${requestId}&request=${requestId}`, "?request=https://evil.test/", `?request=${"x".repeat(257)}`]) expect(oauthConsentRequest(search)).toBeNull();
  });
  it("allows only supported ChatGPT HTTPS callback destinations", () => {
    for (const path of ["/connector_platform_oauth_redirect", "/connector/oauth/callback-123"])
      expect(chatGptOAuthRedirect(`https://chatgpt.com${path}?code=fixture&state=fixture`)).toBe(`https://chatgpt.com${path}?code=fixture&state=fixture`);
    for (const value of ["javascript:alert(1)", "/markets/chart", "https://chatgpt.com.evil.test/connector_platform_oauth_redirect", "https://user@chatgpt.com/connector_platform_oauth_redirect", "http://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com/connector_platform_oauth_redirect#token", "https://chatgpt.com:444/connector_platform_oauth_redirect", "https://chatgpt.com/", "https://chatgpt.com/connector/oauth/id/another"])
      expect(() => chatGptOAuthRedirect(value)).toThrow("授权返回地址无效");
  });
  it("does not request or expose data for a missing authorization handle", () => {
    const tree = render(null); harness.effects[0]();
    expect(harness.api).not.toHaveBeenCalled();
    expect(renderToStaticMarkup(tree)).toContain("授权链接无效");
    expect(elements(tree).some(node => node.props.children === "允许只读访问")).toBe(false);
  });
  it("describes OAuth setup separately from PAT use", () => {
    const html = renderToStaticMarkup(<McpOAuthGuide notify={() => undefined} />);
    expect(html).toContain("https://example.test/mcp"); expect(html).toContain("客户端 ID 和密钥留空");
    expect(html).toContain("动态注册"); expect(html).toContain("手动令牌");
  });
});

describe("OAuth consent lifecycle", () => {
  it("loads only consent metadata and waits for explicit approval", async () => {
    await load();
    expect(harness.api).toHaveBeenCalledTimes(1);
    expect(harness.api.mock.calls[0][0]).toBe(`/api/v2/mcp/oauth/consent?request=${requestId}`);
    const html = renderToStaticMarkup(render());
    expect(html).toContain("行情与 K 线"); expect(html).toContain("指标与 Coinglass 数据");
    expect(html).toContain("不能开单或修改设置"); expect(html).toContain("owner");
    expect(html).not.toContain(requestId); expect(html).not.toContain(details.resource);
    expect(button("允许只读访问").props.disabled).toBe(false);
  });
  it.each(["trade:write", "constructor", "alerts:read"])("rejects unexpected scope %s instead of showing read-only consent", async scope => {
    harness.api.mockResolvedValueOnce({ ...details, scopes: [...details.scopes, scope] }); render(); harness.effects[0]();
    await vi.waitFor(() => expect(find(render(), node => node.type === PageState).props.error).toContain("不支持的权限"));
    expect(elements(render()).some(node => node.props.children === "允许只读访问")).toBe(false);
  });
  it("guards rapid double clicks and redirects only after successful approval", async () => {
    await load(); let resolve!: (value: { redirect_url: string }) => void;
    harness.api.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const submit = button("允许只读访问").props.onClick as () => void;
    submit(); submit();
    expect(harness.api).toHaveBeenCalledTimes(2); expect(button("授权中…").props.disabled).toBe(true);
    expect(JSON.parse(harness.api.mock.calls[1][1].body)).toEqual({ request_id: requestId, decision: "approve", approved_scopes: details.scopes });
    const callback = "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture&state=fixture";
    resolve({ redirect_url: callback }); await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledWith(callback));
    expect(button("允许只读访问").props.disabled).toBe(true);
  });
  it("can explicitly deny and returns the server-generated error callback", async () => {
    await load(); const callback = "https://chatgpt.com/connector_platform_oauth_redirect?error=access_denied&state=fixture";
    harness.api.mockResolvedValueOnce({ redirect_url: callback }); click("拒绝并返回");
    await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledWith(callback));
    expect(JSON.parse(harness.api.mock.calls[1][1].body)).toEqual({ request_id: requestId, decision: "deny" });
  });
  it("keeps requested private research off unless explicitly checked", async () => {
    await load({ ...details, scopes: [...details.scopes, "research:read"] });
    expect(find(render(), node => node.props.name === "research:read").props.checked).toBe(false);
    expect(find(render(), node => node.props.name === "mcp:connect").props.disabled).toBe(true);
    harness.api.mockResolvedValueOnce({ redirect_url: "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture" });
    click("允许只读访问");
    await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[1][1].body).approved_scopes).toEqual(details.scopes);
  });
  it("submits only explicitly selected requested scopes and permits narrowing public analysis", async () => {
    await load({ ...details, scopes: [...details.scopes, "research:read"] });
    for (const [scope, checked] of [["research:read", true], ["analysis:read", false]] as const)
      (find(render(), node => node.props.name === scope).props.onChange as (event: unknown) => void)({ target: { checked } });
    harness.api.mockResolvedValueOnce({ redirect_url: "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture" });
    click("允许只读访问");
    await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[1][1].body).approved_scopes).toEqual(["mcp:connect", "market:read", "research:read"]);
  });
  it("does not offer an unrequested research scope and explains reconnecting", async () => {
    await load();
    expect(elements(render()).some(node => node.props.name === "research:read")).toBe(false);
    expect(renderToStaticMarkup(render())).toContain("本次连接未请求个人研究数据");
    expect(renderToStaticMarkup(render())).toContain("重新添加连接");
  });
  it("keeps both paper permissions unchecked and grants neither from the original consent", async () => {
    await load({ ...details, scopes: [...details.scopes, "research:read", "paper:read", "paper:trade"] });
    for (const scope of ["research:read", "paper:read", "paper:trade"])
      expect(find(render(), node => node.props.name === scope).props.checked).toBe(false);
    harness.api.mockResolvedValueOnce({ redirect_url: "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture" });
    click("允许只读访问"); await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[1][1].body).approved_scopes).toEqual(details.scopes);
  });
  it("explicit simulated trading updates warnings and includes reading, not research", async () => {
    await load({ ...details, scopes: [...details.scopes, "research:read", "paper:read", "paper:trade"] });
    (find(render(), node => node.props.name === "paper:trade").props.onChange as (value: unknown) => void)({ target: { checked: true } });
    expect(find(render(), node => node.props.name === "paper:read").props.checked).toBe(true);
    expect(find(render(), node => node.props.name === "research:read").props.checked).toBe(false);
    expect(renderToStaticMarkup(render())).toContain("不能进行实盘或测试网交易");
    harness.api.mockResolvedValueOnce({ redirect_url: "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture" });
    click("允许模拟交易"); await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[1][1].body).approved_scopes).toEqual([...details.scopes, "paper:read", "paper:trade"]);
  });
  it("removing paper reading also removes trading and restores read-only authorization", async () => {
    await load({ ...details, scopes: [...details.scopes, "paper:read", "paper:trade"] });
    for (const [scope, checked] of [["paper:trade", true], ["paper:read", false]] as const)
      (find(render(), node => node.props.name === scope).props.onChange as (value: unknown) => void)({ target: { checked } });
    expect(find(render(), node => node.props.name === "paper:trade").props.checked).toBe(false);
    expect(button("允许只读访问").props.disabled).toBe(false);
  });
  it("rejects a malformed trade-only requested scope list", async () => {
    harness.api.mockResolvedValueOnce({ ...details, scopes: [...details.scopes, "paper:trade"] }); render(); harness.effects[0]();
    await vi.waitFor(() => expect(find(render(), node => node.type === PageState).props.error).toContain("不支持的权限"));
    expect(elements(render()).some(node => node.props.children === "允许模拟交易")).toBe(false);
  });
  it("blocks an unexpected redirect without exposing its contents", async () => {
    await load(); harness.api.mockResolvedValueOnce({ redirect_url: "https://evil.test/?code=secret-fixture" }); click("允许只读访问");
    await vi.waitFor(() => expect(find(render(), node => node.type === PageState).props.error).toContain("返回地址无效"));
    expect(harness.assign).not.toHaveBeenCalled(); expect(button("允许只读访问").props.disabled).toBe(true);
    expect(renderToStaticMarkup(render())).not.toContain("secret-fixture");
  });
  it("disables an expired request and does not submit it", async () => {
    await load({ ...details, expires_at: "2000-01-01T00:00:00Z" });
    expect(button("允许只读访问").props.disabled).toBe(true); click("允许只读访问");
    expect(harness.api).toHaveBeenCalledTimes(1); expect(renderToStaticMarkup(render())).toContain("请求已过期");
  });
  it("expires a loaded request while the consent page remains open", async () => {
    await load(); render(); harness.effects[harness.effects.length - 1]();
    const timer = vi.mocked(window.setTimeout).mock.calls[0][0] as () => void; timer();
    expect(button("允许只读访问").props.disabled).toBe(true);
  });
  it("preserves the decision when a password check is required, without automatically approving", async () => {
    await load({ ...details, scopes: [...details.scopes, "research:read"] });
    (find(render(), node => node.props.name === "research:read").props.onChange as (event: unknown) => void)({ target: { checked: true } });
    harness.api.mockRejectedValueOnce(new ApiError(403, "RECENT_AUTH_REQUIRED", "password")); click("允许只读访问");
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === Dialog)).toBe(true));
    harness.encrypt.mockResolvedValueOnce({ ciphertext: "encrypted-fixture" }); harness.api.mockResolvedValueOnce({ reauthenticated: true });
    const form = { values: { password: "password-test-fixture" }, reset: vi.fn() };
    (find(render(), node => node.type === "form").props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: form });
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === Dialog)).toBe(false));
    expect(form.reset).toHaveBeenCalledOnce(); expect(harness.encrypt).toHaveBeenCalledWith("user:owner-user:reauthenticate", { password: "password-test-fixture" });
    expect(harness.api.mock.calls[2]).toEqual(["/auth/reauthenticate", { method: "POST", body: JSON.stringify({ encrypted_submission: { ciphertext: "encrypted-fixture" } }) }]);
    expect(harness.api).toHaveBeenCalledTimes(3); expect(harness.assign).not.toHaveBeenCalled();
    expect(renderToStaticMarkup(render())).toContain("请再次点击"); expect(button("允许只读访问").props.disabled).toBe(false);
    expect(find(render(), node => node.props.name === "research:read").props.checked).toBe(true);
    harness.api.mockResolvedValueOnce({ redirect_url: "https://chatgpt.com/connector_platform_oauth_redirect?code=fixture" });
    click("允许只读访问"); await vi.waitFor(() => expect(harness.assign).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[3][1].body).approved_scopes).toEqual([...details.scopes, "research:read"]);
  });
  it("does not reinterpret a denied user role as a password reauthentication request", async () => {
    await load(); harness.api.mockRejectedValueOnce(new ApiError(403, "ROLE_FORBIDDEN", "role")); click("允许只读访问");
    await vi.waitFor(() => expect(find(render(), node => node.type === PageState).props.error).toContain("没有 MCP 授权权限"));
    expect(elements(render()).some(node => node.type === Dialog)).toBe(false);
    expect(button("允许只读访问").props.disabled).toBe(true);
  });
  it("does not retry or enable repeat submission after an uncertain network result", async () => {
    await load(); harness.api.mockRejectedValueOnce(new ApiError(0, "NETWORK_ERROR", "网络中断")); click("允许只读访问");
    await vi.waitFor(() => expect(button("允许只读访问").props.disabled).toBe(true));
    expect(renderToStaticMarkup(render())).toContain("提交状态尚未确认");
    expect(find(render(), node => node.type === PageState).props.onRetry).toBeUndefined();
    click("允许只读访问"); expect(harness.api).toHaveBeenCalledTimes(2);
  });
  it("returns an expired login to the existing authentication gate", async () => {
    harness.api.mockRejectedValueOnce(new ApiError(401, "AUTH_REQUIRED", "auth")); render(); harness.effects[0]();
    await vi.waitFor(() => expect(harness.expired).toHaveBeenCalledOnce());
    expect(harness.assign).not.toHaveBeenCalled();
  });
});
