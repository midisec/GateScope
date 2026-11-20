import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { ApiError } from "./api";
import { PortfolioBuilder } from "./PortfolioResearchWorkspace";
import { Pagination } from "./ui";

const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0, api: vi.fn(), resource: vi.fn(), created: vi.fn(), busy: vi.fn() }));
vi.mock("react", async original => ({ ...await original<typeof import("react")>(), useEffect() {},
  useRef(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = { current: value }; return harness.slots[index]; },
  useState(value: unknown) { const index = harness.cursor++; if (!(index in harness.slots)) harness.slots[index] = value; return [harness.slots[index], (next: unknown) => { harness.slots[index] = typeof next === "function" ? next(harness.slots[index]) : next; }]; },
}));
vi.mock("./api", async original => ({ ...await original<typeof import("./api")>(), api: harness.api }));
vi.mock("./useResearchResource", () => ({ useResearchResource: harness.resource }));
type Element = ReactElement<Record<string, unknown>>;
function nodes(value: ReactNode): Element[] { return Array.isArray(value) ? value.flatMap(nodes) : isValidElement<Record<string, unknown>>(value) ? [value, ...nodes(value.props.children as ReactNode)] : []; }
function render() { harness.cursor = 0; return PortfolioBuilder({ onBusy: harness.busy, onCreated: harness.created }); }
function find(predicate: (element: Element) => boolean) { return nodes(render()).find(predicate)!; }
const source = { id: "source-a", symbol: "SOL_USDT", timeframe: "1h", state: "SUCCEEDED", created_at: "2026-09-14T00:00:00Z" };
function select() { (find(node => node.type === "input" && node.props.type === "checkbox").props.onChange as () => void)(); }
function submit() { (find(node => node.type === "form").props.onSubmit as (event: unknown) => void)({ preventDefault() {}, currentTarget: {} }); }
beforeEach(() => {
  harness.slots = []; harness.cursor = 0; harness.api.mockReset(); harness.resource.mockReset(); harness.created.mockReset(); harness.busy.mockReset();
  harness.resource.mockReturnValue({ data: { items: [source, { ...source, id: "failed-source", state: "FAILED" }], total: 25, has_more: true }, loading: false, error: "", retry: vi.fn() });
  vi.stubGlobal("FormData", class { get() { return null; } });
});
afterEach(() => vi.unstubAllGlobals());
describe("portfolio builder lifecycle", () => {
  it("offers only completed sources and keeps selections when moving to another source page", () => {
    expect(nodes(render()).filter(node => node.type === "input" && node.props.type === "checkbox")).toHaveLength(1);
    select(); expect(harness.api).not.toHaveBeenCalled();
    (find(node => node.type === Pagination).props.onChange as (page: number) => void)(1);
    render(); expect(harness.resource.mock.calls.at(-1)?.[0]).toContain("offset=20");
    expect(find(node => node.props["aria-label"] === "移除 SOL_USDT 1h")).toBeDefined();
  });
  it("creates only after explicit submit and blocks double clicks while pending", async () => {
    select(); let finish!: (value: unknown) => void;
    harness.api.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    submit(); submit(); expect(harness.api).toHaveBeenCalledOnce();
    expect(JSON.parse(harness.api.mock.calls[0][1].body).source_run_ids).toEqual(["source-a"]);
    expect(harness.busy).toHaveBeenCalledWith(true);
    finish({ id: "new-run", state: "QUEUED" }); await vi.waitFor(() => expect(harness.created).toHaveBeenCalledWith({ id: "new-run", state: "QUEUED" }));
  });
  it("does not automatically repeat a timeout that may already have created a job", async () => {
    select(); harness.api.mockRejectedValueOnce(new ApiError(408, "REQUEST_TIMEOUT", "timeout"));
    submit(); await vi.waitFor(() => expect(find(node => node.type === "fieldset").props.disabled).toBe(true));
    await vi.waitFor(() => expect(nodes(render()).some(node => typeof node.props.children === "string" && node.props.children.includes("刷新任务列表核对"))).toBe(true));
    submit(); expect(harness.api).toHaveBeenCalledOnce(); expect(harness.created).not.toHaveBeenCalled();
  });
});
