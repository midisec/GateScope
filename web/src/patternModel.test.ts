import { describe, expect, it } from "vitest";
import { guideValue, sampledGuide, overlayGeometry, patternPercent, type PatternEvent } from "./patternModel";
import type { Bar } from "./types";

const points = [{ time: 1000, price: .000052, label: "1" }, { time: 2000, price: .000057, label: "2" }];
const bars = points.map(p => ({ open_time: new Date(p.time * 1000).toISOString() })) as Bar[];
const event = { points, lines: [{ from: points[1], to: points[0], style: "solid" }] } as PatternEvent;
describe("chart structural geometry", () => {
  it("draws and clips a rounded cup through exact anchors, not a straight V",()=>{
    const rim={time:1000,price:110,label:"L"},vertex={time:2000,price:100,label:"B"};
    const edge={from:rim,to:vertex,style:"solid",role:"curve",curve:{vertex}};
    const view=[1500,1750,2000].map(t=>({open_time:new Date(t*1000).toISOString()})) as Bar[];
    expect(guideValue(edge,1500)).toBe(102.5);
    const geometry=overlayGeometry({points:[rim,vertex],lines:[edge]} as PatternEvent,view);
    expect(geometry.lines[0].from.price).toBe(102.5);
    expect(sampledGuide(geometry.lines[0],view).map(p=>p.value)).toEqual([102.5,100.625,100]);
    expect(geometry.points).toEqual([vertex]);
  });
  it("uses exact candle timestamps and original low price precision", () => {
    expect(overlayGeometry(event, bars).lines[0].points).toEqual(points);
    expect(overlayGeometry(event, bars).points[0].price).toBe(.000052);
  });
  it("never snaps an out-of-range or missing candle to a neighbouring candle", () => {
    expect(overlayGeometry(event, bars.slice(1)).lines).toEqual([]);
    expect(overlayGeometry({ ...event, points: [{ ...points[0], time: 1100 }] }, bars).points).toEqual([]);
  });
  it("rejects invalid prices and zero-width segments", () => {
    expect(overlayGeometry({ ...event, points: [{ ...points[0], price: NaN }] }, bars).lines).toEqual([]);
    expect(overlayGeometry({ ...event, lines: [{ from: points[0], to: points[0], style: "solid" }] }, bars).lines).toEqual([]);
  });
  it("does not turn absent statistics into zero wins", () => {
    expect(patternPercent(null)).toBe("—"); expect(patternPercent(undefined)).toBe("—");
    expect(patternPercent(0)).toBe("0.0%");
  });
  it("clips guides at exact boundary prices without manufacturing a pivot",()=>{
    const view=[1500,2000,2500].map(t=>({open_time:new Date(t*1000).toISOString()})) as Bar[];
    const geometry=overlayGeometry(event,view);
    expect(geometry.status).toBe("CLIPPED");
    expect(geometry.hiddenAnchors).toBe(1);
    expect(geometry.points).toEqual([points[1]]);
    expect(geometry.lines[0].from.time).toBe(1500);
    expect(geometry.lines[0].from.price).toBeCloseTo(.0000545,12);
    expect(geometry.lines[0].from.label).toBe("");
    expect(geometry.points.some(p=>p.time===1500)).toBe(false);
    expect(event.points).toEqual(points);
  });
  it("distinguishes unavailable internal candles from harmless clipping",()=>{
    const e={...event,points:[...points,{time:1500,price:.000055,label:"H"}]} as PatternEvent;
    expect(overlayGeometry(e,bars).status).toBe("MISSING_CANDLE");
    expect(overlayGeometry(e,bars).lines).toEqual([]);
  });
  it("never realigns frozen anchors to a corrected OHLC revision",()=>{
    const e={...event,points:points.map(p=>({...p,basis:"HIGH" as const}))};
    const revised=bars.map(b=>({...b,high:"1"}));
    expect(overlayGeometry(e,revised).status).toBe("REVISED_CANDLE");
    expect(overlayGeometry(e,revised).lines).toEqual([]);
  });
});
