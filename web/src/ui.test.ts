import { describe, it, expect } from "vitest";
import { pageSlice } from "./ui";

describe("list pagination", () => {
  it("clamps empty, negative and now-missing pages", () => {
    expect(pageSlice([], 8, 8)).toEqual({ page: 0, pages: 1, items: [] });
    expect(pageSlice([1, 2, 3], -2, 2)).toEqual({ page: 0, pages: 2, items: [1, 2] });
    expect(pageSlice([1, 2, 3], 8, 2)).toEqual({ page: 1, pages: 2, items: [3] });
  });
});
