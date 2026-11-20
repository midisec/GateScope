import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

beforeEach(() => { vi.stubGlobal("window", { setTimeout, clearTimeout, dispatchEvent: vi.fn() }); vi.stubGlobal("document", { cookie: "gatescope_csrf=test-csrf" }); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe("HTTP client", () => {
  it("sends CSRF and credentials without altering caller headers", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"ok":true}')); vi.stubGlobal("fetch", fetch);
    expect(await api("/write", { method: "POST", body: "{}", headers: { "If-Match": '"2"' } })).toEqual({ ok: true });
    const options = fetch.mock.calls[0][1];
    expect(options.headers.get("X-CSRF-Token")).toBe("test-csrf");
    expect(options.headers.get("If-Match")).toBe('"2"');
    expect(options.credentials).toBe("same-origin");
  });
  it("rejects proxy HTML, malformed JSON and validation errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>Bad Gateway</html>", { status: 502 })));
    await expect(api("/read")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"detail":[{"msg":"missing"}]}', { status: 422 })));
    await expect(api("/read")).rejects.toMatchObject({ code: "HTTP_422" });
  });
  it("signals expired sessions but not incorrect MFA", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"error":{"code":"AUTH_REQUIRED"}}', { status: 401 })));
    await expect(api("/read")).rejects.toMatchObject({ status: 401 });
    expect(window.dispatchEvent).toHaveBeenCalledOnce();
    vi.mocked(window.dispatchEvent).mockClear();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"error":{"code":"INVALID_MFA"}}', { status: 401 })));
    await expect(api("/read")).rejects.toMatchObject({ status: 401 });
    expect(window.dispatchEvent).not.toHaveBeenCalled();
  });
  it("cancels in-flight reads without retrying writes", async () => {
    const fetch = vi.fn((_path, options) => new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController(), result = api("/read", { signal: controller.signal });
    controller.abort(); await expect(result).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("times out a hung write and warns about its unknown outcome", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn((_path, options) => new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")))));
    vi.stubGlobal("fetch", fetch);
    const failure = expect(api("/write", { method: "POST", body: "{}" })).rejects.toMatchObject({ code: "REQUEST_TIMEOUT", message: expect.stringContaining("提交状态尚未确认") });
    await vi.advanceTimersByTimeAsync(30000);
    await failure;
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("does not encourage blind resubmission after a broken write connection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api("/write", { method: "POST", body: "{}" })).rejects.toMatchObject({ code: "NETWORK_ERROR", message: expect.stringContaining("避免重复提交") });
  });
});
