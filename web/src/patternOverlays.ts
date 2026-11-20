import { LineSeries, type IChartApi, type ISeriesApi, type ISeriesPrimitive, type UTCTimestamp } from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import { chartPriceFormat } from "./chartModel";
import { overlayColors, overlayGeometry, sampledGuide, type PatternEvent, type PatternPoint } from "./patternModel";
import type { Bar } from "./types";

function anchorLabels(chart: IChartApi, series: ISeriesApi<"Line">, points: PatternPoint[], color: string): ISeriesPrimitive {
  const renderer = { draw(target: CanvasRenderingTarget2D) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const placed: { x: number; y: number; w: number }[] = [];
      ctx.save(); ctx.font = "600 11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      for (const p of points) {
        const x = chart.timeScale().timeToCoordinate(p.time as UTCTimestamp), y = series.priceToCoordinate(p.price);
        if (x == null || y == null || x < 0 || x > mediaSize.width || y < 0 || y > mediaSize.height) continue;
        ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fill();
        const w = ctx.measureText(p.label).width+10, sign = p.basis === "LOW" ? 1 : -1;
        let ly = y+sign*17;
        for (let i=0;i<6 && placed.some(a => Math.abs(a.x-x)<(a.w+w)/2+3 && Math.abs(a.y-ly)<17);i++) ly += sign*18;
        ly = Math.max(10,Math.min(mediaSize.height-10,ly));
        const lx = Math.max(w/2+2,Math.min(mediaSize.width-w/2-2,x));
        placed.push({x:lx,y:ly,w});
        ctx.strokeStyle = color; ctx.globalAlpha = .55; ctx.beginPath(); ctx.moveTo(x,y+sign*5); ctx.lineTo(lx,ly-sign*7); ctx.stroke();
        ctx.globalAlpha = 1; ctx.fillStyle = "#101722"; ctx.fillRect(lx-w/2,ly-8,w,16); ctx.fillStyle = color; ctx.fillText(p.label,lx,ly);
      }
      ctx.restore();
    });
  } };
  const view = { renderer: () => renderer, zOrder: () => "top" as const };
  return { paneViews: () => [view] };
}

export function attachPatternOverlays(chart: IChartApi, events: PatternEvent[], bars: Bar[]) {
  const cleanups: (() => void)[] = [];
  for (const [index, event] of events.entries()) {
    const geometry = overlayGeometry(event, bars), color = overlayColors[index % overlayColors.length];
    const options = { priceScaleId:"right", autoscaleInfoProvider:()=>null, priceFormat: chartPriceFormat(bars.map(b => +b.low)), color, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const edges = [...geometry.lines];
    if (geometry.points.length && event.levels?.invalid_price != null) {
      const from = {...geometry.points[0],price:event.levels.invalid_price,label:""}, to = {...geometry.points[geometry.points.length-1],time:Math.min(event.end_time??geometry.points.at(-1)!.time,Date.parse(bars.at(-1)!.open_time)/1000),price:event.levels.invalid_price,label:""};
      edges.push({from,to,style:"dashed",role:"invalidation",points:[from,to]});
    }
    if (geometry.lines.length && event.levels?.invalid_edge) {
      const e=event.levels.invalid_edge;
      edges.push({...e,role:"invalidation",points:[e.from,e.to]});
    }
    for (const edge of edges) {
      if (edge.from.time===edge.to.time) continue;
      const guide = edge.role && edge.role !== "leg" && edge.role !== "curve";
      const series = chart.addSeries(LineSeries, { ...options, color: edge.role==="invalidation" ? "#e07b85" : color,
        lineWidth: guide ? 1 : 2, lineStyle: edge.role==="invalidation" ? 1 : edge.style === "dashed" ? 2 : 0 }, 0);
      const values = sampledGuide(edge,bars);
      series.setData(values.map(p => ({...p,time:p.time as UTCTimestamp})));
      cleanups.push(() => chart.removeSeries(series));
    }
    if (geometry.points.length) {
      const labels = chart.addSeries(LineSeries, { ...options, lineVisible: false, pointMarkersVisible: false }, 0);
      labels.setData([...new Map(geometry.points.map(p => [p.time, p])).values()].map(p => ({ time: p.time as UTCTimestamp, value: p.price })));
      const primitive = anchorLabels(chart,labels,geometry.points,color);
      labels.attachPrimitive(primitive);
      const forecasts=[event.initial_forecast??event.forecast,event.forecast];
      const seen=new Set<string>();
      for(const [i,f] of forecasts.entries()) {
        if(!f?.target||seen.has(f.id))continue;seen.add(f.id);
        labels.createPriceLine({price:f.target.price,color:i===0?"#a7afbf":color,lineWidth:1,lineStyle:2,
          axisLabelVisible:true,title:i===0?(f.kind==="CONDITIONAL_MOVE"?"形成后目标":"首次预测"):"修订目标"});
      }
      cleanups.push(() => { labels.detachPrimitive(primitive); chart.removeSeries(labels); });
    }
  }
  return () => cleanups.reverse().forEach(fn => fn());
}
