import { beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { PaperOrderHistoryExplorer, PaperOrderTimeline } from "./PaperOrderInsights";
import type { PaperOrder } from "./paperOrdersModel";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, api: vi.fn() }));
vi.mock("react", async original => ({ ...await original<typeof import("react")>(),
  useEffect() {},
  useRef(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: value }; return harness.slots[index]; },
  useState(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = value; return [harness.slots[index], (next: unknown) => { harness.slots[index] = typeof next === "function" ? next(harness.slots[index]) : next; }]; },
}));
vi.mock("./api", () => ({ api: harness.api }));
type Element = ReactElement<Record<string, unknown>>;
function nodes(value: ReactNode): Element[] { return Array.isArray(value) ? value.flatMap(nodes) : isValidElement<Record<string, unknown>>(value) ? [value, ...nodes(value.props.children as ReactNode)] : []; }
const order = { id: "order-a", fills: [{ id: "first-fill" }], events: [{ id: "first-event" }], fills_has_more: true, events_has_more: true, fills_next_cursor: "fill-cursor", events_next_cursor: "event-cursor" } as PaperOrder;
function render() { harness.cursor = 0; return PaperOrderHistoryExplorer({ order }); }
function button(kind: string, label: string) { const nav = nodes(render()).find(node => node.type === "nav" && node.props["aria-label"] === kind)!; return nodes(nav).find(node => node.type === "button" && node.props.children === label)!; }
function timeline() { return nodes(render()).find(node => node.type === PaperOrderTimeline)!.props.order as PaperOrder; }
beforeEach(() => { harness.slots = []; harness.cursor = 0; harness.api.mockReset(); });
describe("bounded immutable order history paging", () => {
  it("continues the exact first-100 cursor and keeps only the visible batch", async () => {
    harness.api.mockResolvedValueOnce({ items: [{ id: "later-fill" }], has_more: false, next_cursor: null });
    (button("订单成交分页", "下一页").props.onClick as () => void)();
    await vi.waitFor(() => expect(timeline().fills?.[0].id).toBe("later-fill"));
    expect(harness.api.mock.calls[0][0]).toBe("/api/v2/paper/orders/order-a/fills?limit=100&cursor=fill-cursor");
    expect(timeline().fills).toHaveLength(1); expect(timeline().events?.[0].id).toBe("first-event");
    expect(button("订单成交分页", "下一页").props.disabled).toBe(true);
    (button("订单成交分页", "上一页").props.onClick as () => void)();
    expect(timeline().fills?.[0].id).toBe("first-fill"); expect(harness.api).toHaveBeenCalledOnce();
  });
  it("does not overlap event history reads or share fill cursors", async () => {
    let finish!: (value: unknown) => void; harness.api.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const next = button("订单事件分页", "下一页").props.onClick as () => void; next(); next();
    expect(harness.api).toHaveBeenCalledOnce(); expect(harness.api.mock.calls[0][0]).toContain("/events?limit=100&cursor=event-cursor");
    finish({ items: [{ id: "later-event" }], has_more: false, next_cursor: null });
    await vi.waitFor(() => expect(timeline().events?.[0].id).toBe("later-event"));
  });
});
