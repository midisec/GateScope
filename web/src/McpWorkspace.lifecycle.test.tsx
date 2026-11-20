import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { ApiError } from "./api";
import { McpWorkspace } from "./McpWorkspace";
import { Dialog, PagedList } from "./ui";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => void)[], api: vi.fn(), encrypt: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useCallback(fn: unknown) { return fn; },
    useEffect(effect: () => void) { harness.effects.push(effect); },
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
  };
});
vi.mock("./api", async importOriginal => ({ ...await importOriginal<typeof import("./api")>(), api: harness.api }));
vi.mock("./submissionCrypto", () => ({ encryptSubmission: harness.encrypt, wrapEncryptedSubmission: (envelope: unknown) => ({ encrypted_submission: envelope }) }));
vi.mock("./ui", async importOriginal => ({ ...await importOriginal<typeof import("./ui")>(),
  useAction: (notify: (value: string) => void) => ({ busy: "", run: async (_label: string, job: () => Promise<unknown>) => { try { return await job(); } catch (error) { notify((error as Error).message); } } }),
  useConfirm: () => ({ ask: vi.fn(), dialog: null }),
}));
type Element = ReactElement<Record<string, unknown>>;
function elements(node: ReactNode): Element[] { return Array.isArray(node) ? node.flatMap(elements) : isValidElement<Record<string, unknown>>(node) ? [node, ...elements(node.props.children as ReactNode)] : []; }
function find(node: ReactNode, predicate: (value: Element) => boolean): Element { const item = elements(node).find(predicate); if (!item) throw new Error("Element not found"); return item; }
function render() { harness.cursor = 0; return McpWorkspace({ userId: "owner-user", notify: vi.fn() }); }
function formFor(field: string) { return find(render(), node => node.type === "form" && elements(node.props.children as ReactNode).some(child => child.props.name === field)); }
function submit(field: string, values: Record<string, string>) {
  const form = { values, reset: vi.fn() };
  (formFor(field).props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: form });
  return form;
}
async function load() {
  harness.api.mockResolvedValueOnce({ items: [], supported_scopes: ["mcp:connect", "market:read", "analysis:read", "alerts:read", "research:read", "paper:read", "paper:trade"] });
  render(); harness.effects[0](); await vi.waitFor(() => expect(harness.api).toHaveBeenCalledOnce());
}
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); harness.encrypt.mockReset();
  vi.stubGlobal("FormData", class { constructor(private form: { values: Record<string, string> }) {} get(key: string) { return this.form.values[key] ?? null; } });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("MCP owner creation workflow", () => {
  it("does not preselect private research or alerts on manual tokens", async () => {
    await load();
    for (const name of ["research:read", "alerts:read", "paper:read", "paper:trade"])
      expect(find(render(), node => node.props.name === name).props.checked).toBe(false);
    for (const name of ["mcp:connect", "market:read", "analysis:read"])
      expect(find(render(), node => node.props.name === name).props.checked).toBe(true);
    expect(find(render(), node => node.props.name === "mcp:connect").props.disabled).toBe(true);
  });
  it("links manual paper trading permission to account reading without selecting research", async () => {
    await load();
    (find(render(), node => node.props.name === "paper:trade").props.onChange as (value: unknown) => void)({ target: { checked: true } });
    expect(find(render(), node => node.props.name === "paper:read").props.checked).toBe(true);
    expect(find(render(), node => node.props.name === "paper:trade").props.checked).toBe(true);
    expect(find(render(), node => node.props.name === "research:read").props.checked).toBe(false);
    (find(render(), node => node.props.name === "paper:read").props.onChange as (value: unknown) => void)({ target: { checked: false } });
    expect(find(render(), node => node.props.name === "paper:trade").props.checked).toBe(false);
  });
  it("creates without preview selection and renders an empty token list immediately", async () => {
    await load(); harness.api.mockResolvedValueOnce({ id: "new-client", name: "owner research", status: "ACTIVE", etag: 1 });
    const form = submit("name", { name: "owner research" });
    await vi.waitFor(() => expect(form.reset).toHaveBeenCalledOnce());
    expect(JSON.parse(harness.api.mock.calls[1][1].body)).toEqual({ name: "owner research" });
    const listing = find(render(), node => node.type === PagedList && node.props.label === "MCP 客户端");
    const clients = listing.props.items as { id: string; tokens: unknown[] }[];
    expect(clients[0].tokens).toEqual([]);
    const card = (listing.props.render as (client: unknown) => ReactNode)(clients[0]);
    expect(find(card, node => node.type === PagedList).props.items).toEqual([]);
    expect(find(render(), node => node.type === "select" && !!node.props.required).props.value).toBe("new-client");
    expect(harness.api.mock.calls.every(([path]) => !String(path).includes("market-preview"))).toBe(true);
  });
  it("asks an expired owner for password confirmation without retrying the write", async () => {
    await load(); harness.api.mockRejectedValueOnce(new ApiError(403, "RECENT_AUTH_REQUIRED", "请确认当前密码"));
    submit("name", { name: "owner research" });
    await vi.waitFor(() => expect(elements(render()).some(node => node.type === Dialog && node.props.title === "确认登录身份")).toBe(true));
    expect(harness.api).toHaveBeenCalledTimes(2);
    expect(find(render(), node => node.props.name === "name").props.value).toBeUndefined();
  });
  it("confirms with a user-bound encrypted envelope and never auto-creates afterward", async () => {
    await load();
    (find(render(), node => node.type === "button" && node.props.children === "确认登录身份").props.onClick as () => void)();
    harness.encrypt.mockResolvedValue({ ciphertext: "encrypted-fixture" }); harness.api.mockResolvedValueOnce({ reauthenticated: true });
    const form = submit("password", { password: "password-test-fixture" });
    await vi.waitFor(() => expect(harness.api).toHaveBeenCalledTimes(2));
    expect(form.reset).toHaveBeenCalledOnce();
    expect(harness.encrypt).toHaveBeenCalledWith("user:owner-user:reauthenticate", { password: "password-test-fixture" });
    expect(harness.api.mock.calls[1]).toEqual(["/auth/reauthenticate", { method: "POST", body: JSON.stringify({ encrypted_submission: { ciphertext: "encrypted-fixture" } }) }]);
    expect(elements(render()).some(node => node.type === Dialog)).toBe(false);
  });
  it("does not treat role denial as an expired owner password", async () => {
    await load(); harness.api.mockRejectedValueOnce(new ApiError(403, "ROLE_FORBIDDEN", "需要所有者权限"));
    submit("name", { name: "owner research" }); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(elements(render()).some(node => node.type === Dialog)).toBe(false);
  });
});
