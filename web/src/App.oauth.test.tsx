import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ApiError } from "./api";
import { OAuthConsent, OAUTH_CONSENT_PATH } from "./OAuthConsent";
import { AdminConsole } from "./AdminConsole";
import type { Session } from "./types";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, effects: [] as (() => void)[], api: vi.fn(), replace: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual,
    useCallback(fn: unknown) { return fn; },
    useEffect(effect: () => void) { harness.effects.push(effect); },
    useState(initial: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = initial; return [harness.slots[index], (value: unknown) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }]; },
  };
});
vi.mock("./api", async importOriginal => ({ ...await importOriginal<typeof import("./api")>(), api: harness.api }));

const requestId = "oauth-request-fixture-12345678901234567890123";
const session: Session = { user: { id: "owner", tenant_id: "tenant", username: "owner", role: "OWNER" }, expires_at: "2099-01-01T00:00:00Z", mfa_required: false, auth_strength: "MFA" };
function render() { harness.cursor = 0; return App(); }
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.effects = []; harness.api.mockReset(); harness.replace.mockReset();
  vi.stubGlobal("location", { pathname: OAUTH_CONSENT_PATH, search: `?request=${requestId}` });
  vi.stubGlobal("history", { replaceState: harness.replace });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("OAuth consent application authentication gate", () => {
  it.each(["/admin/overview", "/admin/users", "/admin/resources", "/admin/audit"])("uses a separate admin shell on %s after authentication", async pathname => {
    vi.stubGlobal("location", { pathname, search: "" });
    harness.api.mockResolvedValueOnce(session); render(); harness.effects[0]();
    await vi.waitFor(() => expect(render().type).toBe(AdminConsole));
    expect(harness.replace).not.toHaveBeenCalled();
  });
  it("keeps the MFA gate before the admin shell", async () => {
    vi.stubGlobal("location", { pathname: "/admin/users", search: "" });
    harness.api.mockResolvedValueOnce({ ...session, mfa_required: true }); render(); harness.effects[0]();
    await vi.waitFor(() => expect((render().type as { name: string }).name).toBe("MfaGate"));
    expect(harness.replace).not.toHaveBeenCalled();
  });
  it("keeps the consent path and request through password login and MFA without opening the workbench", async () => {
    harness.api.mockRejectedValueOnce(new ApiError(401, "AUTH_REQUIRED", "auth")); render(); harness.effects[0]();
    await vi.waitFor(() => expect(render().props.oauth).toBe(true));
    const login = render(); expect((login.type as { name: string }).name).toBe("Login");
    harness.api.mockResolvedValueOnce({ ...session, mfa_required: true });
    await login.props.onLogin();
    const mfa = render(); expect((mfa.type as { name: string }).name).toBe("MfaGate");
    harness.api.mockResolvedValueOnce(session); await mfa.props.onVerified();
    const consent = render(); expect(consent.type).toBe(OAuthConsent); expect(consent.props.requestId).toBe(requestId);
    expect(harness.replace).not.toHaveBeenCalled(); expect(location.pathname).toBe(OAUTH_CONSENT_PATH); expect(location.search).toContain(requestId);
  });
  it("opens a signed-in full-MFA session directly on explicit consent", async () => {
    harness.api.mockResolvedValueOnce(session); render(); harness.effects[0]();
    await vi.waitFor(() => expect(render().type).toBe(OAuthConsent));
    expect(harness.replace).not.toHaveBeenCalled();
  });
  it("still sends an ordinary login to the market chart", async () => {
    vi.stubGlobal("location", { pathname: "/login", search: "" });
    harness.api.mockResolvedValueOnce(session); render(); harness.effects[0]();
    await vi.waitFor(() => expect(harness.replace).toHaveBeenCalledWith({}, "", "/markets/chart"));
    expect(render().type).not.toBe(OAuthConsent);
  });
  it("does not transform a malformed consent request into another navigation target", async () => {
    vi.stubGlobal("location", { pathname: OAUTH_CONSENT_PATH, search: "?request=https://evil.test/&return_to=https://evil.test" });
    harness.api.mockResolvedValueOnce(session); render(); harness.effects[0]();
    await vi.waitFor(() => expect(render().type).toBe(OAuthConsent));
    expect(render().props.requestId).toBeNull(); expect(harness.replace).not.toHaveBeenCalled();
  });
});
