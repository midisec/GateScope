import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type FormEvent, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AlertRuleEditor } from "./AlertRuleEditor";
import { PageState } from "./ui";
import type { AlertRule, NotificationChannel, PatternCatalogItem } from "./types";

// This suite drives the real component handlers and useAction lock without adding
// a DOM dependency. Native form serialization is represented by explicit values;
// React state/ref slots persist across each explicit component render.
const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, api: vi.fn() }));
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState(initial: unknown) {
      const index = harness.cursor++;
      if (!(index in harness.slots)) harness.slots[index] = initial;
      return [harness.slots[index], (value: unknown) => {
        harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value;
      }];
    },
    useRef(initial: unknown) {
      const index = harness.cursor++;
      if (!(index in harness.slots)) harness.slots[index] = { current: initial };
      return harness.slots[index];
    },
  };
});
vi.mock("./api", () => ({ api: harness.api, jsonBody: JSON.stringify }));

type Element = ReactElement<Record<string, unknown>>;
type FormValues = Record<string, string>;
const channel: NotificationChannel = { id: "tg-channel", alias: "我的 Telegram", status: "ACTIVE", connection_id: "tg-connection", etag: 1 };
const patterns: PatternCatalogItem[] = [{ pattern_id: "double_bottom", name: "双底", group: "reversal", direction: "bullish", experimental: false }];

function rule(confirmedOnly = true): AlertRule {
  return {
    id: "rule-one", name: "BTC 形态通知", symbol: "BTC_USDT", status: "ACTIVE", etag: 7, current_version: 3,
    version: {
      condition: { instrument_id: "btc-instrument", timeframe: "4h", pattern_ids: ["double_bottom"], match_mode: "ANY", minimum_confidence: .65, confirmed_only: confirmedOnly, coincidence_bars: 3, cooldown_seconds: 600 },
      routing: { channel_ids: [channel.id, "unavailable-original-channel"] },
    },
  };
}

function elements(node: ReactNode): Element[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...elements(node.props.children as ReactNode)];
}

function find(root: ReactNode, predicate: (element: Element) => boolean): Element {
  const element = elements(root).find(predicate);
  if (!element) throw new Error("Expected editor element was not found");
  return element;
}

function render(original = rule(), onSaved = vi.fn()) {
  harness.cursor = 0;
  return AlertRuleEditor({ rule: original, channels: [channel], patterns, onClose: vi.fn(), onSaved });
}

function submit(root: ReactNode, overrides: FormValues = {}) {
  const values: FormValues = {};
  for (const element of elements(root)) {
    if (typeof element.props.name === "string" && element.props.defaultValue !== undefined) {
      values[element.props.name] = String(element.props.defaultValue);
    }
  }
  Object.assign(values, overrides);
  const preventDefault = vi.fn();
  const form = find(root, element => element.type === "form");
  (form.props.onSubmit as (event: FormEvent<HTMLFormElement>) => void)({
    preventDefault, currentTarget: { values },
  } as unknown as FormEvent<HTMLFormElement>);
  expect(preventDefault).toHaveBeenCalledOnce();
}

function payload(callIndex = 0) {
  const [, init] = harness.api.mock.calls[callIndex] as [string, RequestInit];
  return JSON.parse(String(init.body));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.api.mockReset();
  harness.api.mockResolvedValue(rule());
  vi.stubGlobal("FormData", class {
    constructor(private form: { values: FormValues }) {}
    get(name: string) { return this.form.values[name] ?? null; }
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("saved pattern notification editor", () => {
  it("keeps confirmation-only timing, original conditions and original notification channels by default", async () => {
    const original = rule(), onSaved = vi.fn(), root = render(original, onSaved);
    expect(find(root, element => element.props.name === "phase").props.defaultValue).toBe("confirmed");
    submit(root);
    expect(harness.api).toHaveBeenCalledWith(`/api/v2/alert-rules/${original.id}`, expect.objectContaining({ method: "PUT", headers: { "If-Match": '"7"' } }));
    expect(payload()).toEqual({ name: original.name, condition: { schema_version: 1, ...original.version.condition }, routing: { schema_version: 1, channel_ids: original.version.routing.channel_ids } });
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  });

  it("allows formed previews plus confirmation without changing the existing channel routing", () => {
    const original = rule();
    submit(render(original), { phase: "formed" });
    expect(payload().condition.confirmed_only).toBe(false);
    expect(payload().routing.channel_ids).toEqual(original.version.routing.channel_ids);
  });

  it("preserves an existing formed setting and supports switching back to confirmation-only", () => {
    const root = render(rule(false));
    expect(find(root, element => element.props.name === "phase").props.defaultValue).toBe("formed");
    submit(root, { phase: "confirmed" });
    expect(payload().condition.confirmed_only).toBe(true);
  });

  it("shows progress and blocks repeated submit events while one save is pending", async () => {
    const pending = deferred<AlertRule>(), onSaved = vi.fn(), original = rule();
    harness.api.mockReturnValueOnce(pending.promise);
    const root = render(original, onSaved);
    submit(root); submit(root);
    expect(harness.api).toHaveBeenCalledOnce();
    const busyRoot = render(original, onSaved), button = find(busyRoot, element => element.type === "button" && element.props.className === "primary");
    expect(button.props.disabled).toBe(true); expect(button.props.children).toBe("保存中…");
    expect(busyRoot.props.busy).toBe(true);
    submit(busyRoot); expect(harness.api).toHaveBeenCalledOnce();
    pending.resolve(original);
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(find(render(original, onSaved), element => element.type === "button" && element.props.className === "primary").props.disabled).toBe(false);
  });

  it("displays save errors, keeps unavailable original channels selected, and permits a deliberate retry", async () => {
    const original = rule(), onSaved = vi.fn();
    harness.api.mockRejectedValueOnce(new Error("保存失败，请检查通知通道"));
    submit(render(original, onSaved), { phase: "formed" });
    await vi.waitFor(() => {
      const error = find(render(original, onSaved), element => element.type === PageState);
      expect(renderToStaticMarkup(error)).toContain('role="alert"');
      expect(renderToStaticMarkup(error)).toContain("保存失败，请检查通知通道");
    });
    expect(onSaved).not.toHaveBeenCalled();
    const retryRoot = render(original, onSaved);
    const unavailable = find(retryRoot, element => element.type === "label" && elements(element).some(child => child.type === "input" && child.props.type === "checkbox") && (element.props.children as unknown[])?.includes("原通道已不可用，请取消勾选"));
    expect(find(unavailable, element => element.type === "input").props.checked).toBe(true);
    submit(retryRoot, { phase: "formed" });
    expect(harness.api).toHaveBeenCalledTimes(2);
    expect(payload(1).routing.channel_ids).toEqual(original.version.routing.channel_ids);
    expect(payload(1).condition.confirmed_only).toBe(false);
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  });

  it("removes an unavailable original channel only when the user explicitly deselects it", () => {
    const root = render();
    const unavailable = find(root, element => element.type === "label" && (element.props.children as unknown[])?.includes?.("原通道已不可用，请取消勾选"));
    const checkbox = find(unavailable, element => element.type === "input");
    (checkbox.props.onChange as (event: { target: { checked: boolean } }) => void)({ target: { checked: false } });
    submit(render());
    expect(payload().routing.channel_ids).toEqual([channel.id]);
  });

  it("rejects an empty strategy name with a visible error before any request", () => {
    submit(render(), { name: "  " });
    expect(harness.api).not.toHaveBeenCalled();
    const error = find(render(), element => element.type === PageState);
    expect(renderToStaticMarkup(error)).toContain("请填写名称、选择币种和至少一个形态");
  });
});
