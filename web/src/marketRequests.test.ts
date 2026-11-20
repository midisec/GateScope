import { afterEach, expect, it, vi } from "vitest";
import { LatestSelection, startIndicatorRequest } from "./marketRequests";

afterEach(() => vi.useRealTimers());
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
it("keeps the latest search selection when an earlier watchlist lookup completes", async () => {
  const selection = new LatestSelection(), pending = deferred<string>(), shown: string[] = [];
  const first = selection.resolve(() => pending.promise, value => shown.push(value));
  selection.commit(() => shown.push("ETH"));
  pending.resolve("BTC"); await first;
  expect(shown).toEqual(["ETH"]);
});
it("lets a later watchlist selection win and suppresses obsolete request errors", async () => {
  const selection = new LatestSelection(), pending = deferred<string>(), shown: string[] = [];
  const first = selection.resolve(() => pending.promise, value => shown.push(value));
  await selection.resolve(async () => "ETH", value => shown.push(value));
  pending.reject(new Error("old lookup failed")); await first;
  expect(shown).toEqual(["ETH"]);
  await expect(selection.resolve(async () => { throw new Error("current failure"); }, () => {})).rejects.toThrow("current failure");
});
it("does not apply an async choice after leaving the workspace", async () => {
  const selection = new LatestSelection(), pending = deferred<string>(), apply = vi.fn();
  const first = selection.resolve(() => pending.promise, apply); selection.invalidate();
  pending.resolve("BTC"); await first; expect(apply).not.toHaveBeenCalled();
});

const callbacks = () => ({ success: vi.fn(), error: vi.fn(), loading: vi.fn(), visible: () => true });
it("retries a transient indicator failure without waiting for another closed bar", async () => {
  vi.useFakeTimers();
  const read = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue({ items: [1] });
  const cb = callbacks(), request = startIndicatorRequest(read, cb);
  await vi.advanceTimersByTimeAsync(2000);
  expect(read).toHaveBeenCalledTimes(2); expect(cb.success).toHaveBeenCalledWith({ items: [1] });
  request.dispose();
});
it("bounds automatic retries and lets explicit retry recover after exhaustion", async () => {
  vi.useFakeTimers();
  const read = vi.fn().mockRejectedValue(new Error("network")), cb = callbacks(), request = startIndicatorRequest(read, cb);
  await vi.advanceTimersByTimeAsync(30000);
  expect(read).toHaveBeenCalledTimes(3); expect(cb.error).toHaveBeenLastCalledWith(expect.any(Error), false);
  read.mockResolvedValue({ items: [2] }); request.retry(); await vi.advanceTimersByTimeAsync(0);
  expect(cb.success).toHaveBeenCalledWith({ items: [2] }); request.dispose();
});
it("retries on recovery with unchanged bars without overlapping requests", async () => {
  vi.useFakeTimers();
  const pending = deferred<object>(), read = vi.fn().mockRejectedValueOnce(new Error("offline")).mockReturnValueOnce(pending.promise);
  const cb = callbacks(), request = startIndicatorRequest(read, cb);
  await vi.advanceTimersByTimeAsync(0); request.resume(); request.resume();
  expect(read).toHaveBeenCalledTimes(2);
  pending.resolve({ items: [] }); await vi.advanceTimersByTimeAsync(10000);
  expect(read).toHaveBeenCalledTimes(2); expect(cb.success).toHaveBeenCalledOnce(); request.dispose();
});
it("aborts old indicator work and prevents updates after symbol changes", async () => {
  vi.useFakeTimers();
  const pending = deferred<object>(), read = vi.fn(() => pending.promise), cb = callbacks();
  const request = startIndicatorRequest(read, cb); request.dispose(); pending.resolve({ items: ["BTC"] });
  await vi.advanceTimersByTimeAsync(10000);
  expect(cb.success).not.toHaveBeenCalled(); expect(read).toHaveBeenCalledOnce();
});
it("does not automatically repeat an invalid indicator configuration", async () => {
  vi.useFakeTimers();
  const read = vi.fn().mockRejectedValue({ status: 422 }), request = startIndicatorRequest(read, callbacks());
  await vi.advanceTimersByTimeAsync(30000); expect(read).toHaveBeenCalledOnce(); request.dispose();
});
