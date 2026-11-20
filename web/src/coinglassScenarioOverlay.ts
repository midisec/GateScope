import type { IChartApi, ISeriesApi, ISeriesPrimitive, Logical } from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { CoinglassScenario } from "./coinglassModel";
import type { Bar } from "./types";

// A primitive does not add synthetic candles or change the original time scale.
export function attachCoinglassScenario(chart:IChartApi,candles:ISeriesApi<"Candlestick">,bars:Bar[],scenario:CoinglassScenario,timeframeSeconds:number) {
  const renderer={draw(target:CanvasRenderingTarget2D){
    if(!bars.length||!timeframeSeconds)return;
    target.useMediaCoordinateSpace(({context:ctx,mediaSize})=>{
      const latest=Date.parse(bars.at(-1)!.open_time)/1000;
      const logical=bars.length-1+(scenario.issued_at-latest)/timeframeSeconds;
      const start=chart.timeScale().logicalToCoordinate(logical as Logical);
      const low=candles.priceToCoordinate(scenario.target_lower),high=candles.priceToCoordinate(scenario.target_upper);
      if(start==null||low==null||high==null||start>=mediaSize.width)return;
      const x=Math.max(0,start),top=Math.min(low,high),height=Math.abs(high-low);
      ctx.save();ctx.fillStyle="#c897f520";ctx.fillRect(x,top,mediaSize.width-x,height);
      ctx.strokeStyle="#c897f5";ctx.lineWidth=1;ctx.setLineDash([5,4]);
      for(const y of [low,high]){ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(mediaSize.width,y);ctx.stroke();}
      const entry=candles.priceToCoordinate(scenario.entry);
      if(entry!=null){ctx.strokeStyle="#e3bd73";ctx.beginPath();ctx.moveTo(x,entry);ctx.lineTo(mediaSize.width,entry);ctx.stroke();}
      ctx.setLineDash([]);ctx.font="11px system-ui";ctx.fillStyle="#c897f5";
      ctx.fillText("本次观察情景 · 非止盈",x+6,Math.max(14,Math.min(mediaSize.height-6,top-6)));
      ctx.restore();
    });
  }};
  const primitive:ISeriesPrimitive={paneViews:()=>[{renderer:()=>renderer,zOrder:()=>"top" as const}]};
  candles.attachPrimitive(primitive);
  return()=>candles.detachPrimitive(primitive);
}
