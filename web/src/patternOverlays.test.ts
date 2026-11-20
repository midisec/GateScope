import { describe, expect, it, vi } from "vitest";
import { sampledGuide, type PatternEvent } from "./patternModel";
import { attachPatternOverlays } from "./patternOverlays";
import type { Bar } from "./types";
vi.mock("lightweight-charts",()=>({LineSeries:"line"}));

describe("precise structure overlays",()=>{
  it("samples analytical prices, not the log-axis straight-line interpolation",()=>{
    const bars=[100,200,300].map(t=>({open_time:new Date(t*1000).toISOString()})) as Bar[];
    const from={time:100,price:100,label:""},to={time:300,price:200,label:""};
    expect(sampledGuide({from,to,style:"dashed"},bars)).toEqual([{time:100,value:100},{time:200,value:150},{time:300,value:200}]);
  });
  it("keeps the anchor dot at exact XY while moving labels away",()=>{
    let primitive:any;
    const series={setData:vi.fn(),attachPrimitive:(p:any)=>{primitive=p},detachPrimitive:vi.fn(),priceToCoordinate:(p:number)=>p*2};
    const chart={addSeries:()=>series,removeSeries:vi.fn(),timeScale:()=>({timeToCoordinate:(t:number)=>t})};
    const point={time:100,price:50,label:"H",basis:"HIGH" as const};
    const event={points:[point],lines:[]} as unknown as PatternEvent;
    const cleanup=attachPatternOverlays(chart as any,[event],[{open_time:new Date(100000).toISOString(),low:"49"}] as Bar[]);
    const ctx:any={save:vi.fn(),restore:vi.fn(),beginPath:vi.fn(),arc:vi.fn(),fill:vi.fn(),moveTo:vi.fn(),lineTo:vi.fn(),stroke:vi.fn(),fillRect:vi.fn(),fillText:vi.fn(),measureText:()=>({width:10})};
    primitive.paneViews()[0].renderer().draw({useMediaCoordinateSpace:(fn:any)=>fn({context:ctx,mediaSize:{width:400,height:300}})});
    expect(ctx.arc.mock.calls[0].slice(0,2)).toEqual([100,100]);
    expect(ctx.fillText.mock.calls[0]).toEqual(["H",100,83]);
    cleanup();expect(chart.removeSeries).toHaveBeenCalled();
  });
  it("uses the candle price scale without distorting it, samples legs too",()=>{
    const setData=vi.fn();
    const series={setData,attachPrimitive:vi.fn(),detachPrimitive:vi.fn(),createPriceLine:vi.fn()};
    const chart={addSeries:vi.fn((_type:unknown,_options:any,_pane:number)=>series),removeSeries:vi.fn()};
    const points=[{time:100,price:100,label:"A"},{time:300,price:200,label:"B"}];
    const event={points,lines:[{from:points[0],to:points[1],style:"solid",role:"leg"}]} as PatternEvent;
    const bars=[100,200,300].map(t=>({open_time:new Date(t*1000).toISOString(),low:"90"})) as Bar[];
    attachPatternOverlays(chart as any,[event],bars);
    expect(chart.addSeries.mock.calls[0][1].priceScaleId).toBe("right");
    expect(chart.addSeries.mock.calls[0][1].autoscaleInfoProvider()).toBe(null);
    expect(setData.mock.calls[0][0][1]).toEqual({time:200,value:150});
  });
});
