import { useEffect, useRef, useState } from "react";
import { AreaSeries, BaselineSeries, CandlestickSeries, ColorType, LineSeries, createChart, createSeriesMarkers, type IChartApi, type SeriesMarker, type UTCTimestamp } from "lightweight-charts";
import { alignedCurve, chartPriceFormat, priceText, drawdownSummary, reportTimeIndex, timeNumber, tradeBarTimes } from "./chartModel";
import { fmt, selectedRules, type Report, type Trade } from "./researchModel";

export function ResearchChart({ report, trade, mode = "equity", onTradeSelect }: { report: Report; trade?: Trade; mode?: "equity" | "drawdown"; onTradeSelect?: (index: number) => void }) {
  const element = useRef<HTMLDivElement>(null), chartRef = useRef<IChartApi | null>(null);
  const selectRef = useRef(onTradeSelect); selectRef.current = onTradeSelect;
  const [hover, setHover] = useState("");
  useEffect(() => {
    if (!element.current) return;
    setHover("");
    const chart = createChart(element.current, { autoSize: true, layout: { background: { type: ColorType.Solid, color: "#101722" }, textColor: "#92a0b6", panes: { separatorColor: "#364358" } }, grid: { vertLines: { color: "#1b2532" }, horzLines: { color: "#1b2532" } }, timeScale: { timeVisible: true }, handleScroll: { vertTouchDrag: false }, localization: { locale: "zh-CN" } });
    chartRef.current = chart;
    const ts = (time: string) => timeNumber(time) as UTCTimestamp;
    const priceFormat = chartPriceFormat(report.candles.map(b => b.low));
    const series = chart.addSeries(CandlestickSeries, { priceFormat, upColor: "#22c997", downColor: "#ef5c75", wickUpColor: "#22c997", wickDownColor: "#ef5c75", borderVisible: false });
    const timeIndex = reportTimeIndex(report);
    const trades = trade ? [trade] : report.full.trades, bounds = trade ? tradeBarTimes(report, trade, timeIndex) : undefined;
    const entryIndex = bounds?.entry === undefined ? 0 : report.candles.findIndex(b => ts(b.time) === bounds.entry);
    const exitIndex = bounds?.exit === undefined ? report.candles.length - 1 : report.candles.findIndex(b => ts(b.time) === bounds.exit);
    const patternEvents = trade ? (report.pattern_events ?? []).filter(e => trade.pattern_refs?.includes(e.evidence_id)) : [];
    const patternStart = patternEvents.flatMap(e => e.points.map(p => report.candles.findIndex(b => ts(b.time) === p.time))).filter(i => i >= 0);
    const candles = trade ? report.candles.slice(Math.max(0, Math.min(entryIndex - 20, ...patternStart)), exitIndex + 16) : report.candles;
    const visible = new Set(candles.map(b => ts(b.time))), byTime = new Map(candles.map(b => [ts(b.time), b]));
    series.setData(candles.map(b => ({ ...b, time: ts(b.time) })));
    const ruleIds = selectedRules(report.spec);
    const overlays = ruleIds.includes("reversion") && ruleIds.length === 1
      ? [["boll.upper", "#b492ee", "布林上轨"], ["boll.lower", "#b492ee", "布林下轨"]]
      : [["fast.ema", "#f4bd66", `EMA ${report.spec.fast_period}`], ["slow.ema", "#b492ee", `EMA ${report.spec.slow_period}`]];
    if (ruleIds.includes("reversion") && ruleIds.length > 1) overlays.push(["boll.upper", "#b492ee", "布林上轨"], ["boll.lower", "#b492ee", "布林下轨"]);
    if (ruleIds.includes("breakout")) overlays.push(["channel.high", "#22c997", "前期高点"], ["channel.low", "#ef5c75", "前期低点"]);
    for (const event of patternEvents) for (const edge of event.lines ?? []) {
      const points = [edge.from, edge.to].sort((a, b) => a.time - b.time);
      if (points[0].time === points[1].time || points.some(p => !visible.has(p.time as UTCTimestamp) || !Number.isFinite(p.price))) continue;
      chart.addSeries(LineSeries, { priceFormat, color: event.direction === "bearish" ? "#f690a6" : "#f4bd66", lineWidth: 2, lineStyle: edge.style === "dashed" ? 2 : 0, title: "", priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }).setData(points.map(p => ({ time: p.time as UTCTimestamp, value: p.price })));
    }
    const indicatorMap = new Map((report.indicator_series ?? []).map(p => [ts(p.time), p.values]));
    for (const [key, color, title] of overlays) chart.addSeries(LineSeries, { priceFormat, color, title, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(candles.map(b => {
      const time = ts(b.time), value = indicatorMap.get(time)?.[key];
      return typeof value === "number" && Number.isFinite(value) ? { time, value } : { time };
    }));
    if (trade) for (const [price, color, title] of [[trade.entry, "#78a6ff", "入场"], [trade.stop, "#ef5c75", "止损"], [trade.target, "#22c997", "止盈"]] as const) series.createPriceLine({ price, color, title, lineWidth: 1, axisLabelVisible: true });
    const markers: SeriesMarker<UTCTimestamp>[] = [], tradeAtTime = new Map<number, number>();
    const showLabels = !!trade || (element.current.clientWidth >= 600 && trades.length <= 30);
    trades.forEach((t, i) => {
      const times = tradeBarTimes(report, t, timeIndex), long = t.side > 0;
      if (times.entry !== undefined && visible.has(times.entry as UTCTimestamp)) { markers.push({ time: times.entry as UTCTimestamp, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: "#78a6ff", text: showLabels ? `${trade ? "" : `${i + 1} `}${long ? "多" : "空"}入` : "" }); tradeAtTime.set(times.entry, i); }
      if (times.exit !== undefined && visible.has(times.exit as UTCTimestamp)) { markers.push({ time: times.exit as UTCTimestamp, position: long ? "aboveBar" : "belowBar", shape: "circle", color: t.net_pnl > 0 ? "#22c997" : "#ef5c75", text: showLabels ? `${trade ? "" : `${i + 1} `}退出` : "" }); tradeAtTime.set(times.exit, i); }
    });
    createSeriesMarkers(series, markers.sort((a, b) => a.time - b.time));
    const curve = alignedCurve(report), curveByTime = new Map(curve.map(p => [p.chartTime, p]));
    if (!trade) {
      if (mode === "drawdown") {
        const depth = Math.min(-.1, ...curve.map(p => p.drawdown));
        const underwater = chart.addSeries(BaselineSeries, { baseValue: { type: "price", price: 0 }, topLineColor: "#ef5c75", bottomLineColor: "#ef5c75", topFillColor1: "#ef5c7500", topFillColor2: "#ef5c7500", bottomFillColor1: "#ef5c7535", bottomFillColor2: "#ef5c7510", title: "回撤 %", priceFormat: { type: "custom", minMove: .01, formatter: (v: number) => `${v.toFixed(2)}%` }, autoscaleInfoProvider: () => ({ priceRange: { minValue: depth * 1.12, maxValue: 0 } }) }, 1);
        underwater.setData(curve.map(p => ({ time: p.chartTime as UTCTimestamp, value: p.drawdown })));
        underwater.createPriceLine({ price: 0, color: "#96a6be", title: "历史峰值 0%", lineWidth: 1 });
      } else chart.addSeries(AreaSeries, { lineColor: "#6c9dff", topColor: "#6c9dff35", bottomColor: "#6c9dff03", title: "账户净值", lineWidth: 2 }, 1).setData(curve.map(p => ({ time: p.chartTime as UTCTimestamp, value: p.equity })));
      chart.panes()[0]?.setStretchFactor(2); chart.panes()[1]?.setStretchFactor(1);
    }
    chart.subscribeCrosshairMove(e => {
      if (!e.time || typeof e.time !== "number") return;
      const candle = byTime.get(e.time), point = curveByTime.get(e.time);
      if (candle) setHover(`${new Date(candle.time).toLocaleString()} · O ${priceText(candle.open)} H ${priceText(candle.high)} L ${priceText(candle.low)} C ${priceText(candle.close)}${point ? ` ｜ 收盘净值 ${fmt(point.equity)} · 峰值 ${fmt(point.peak)} · 回撤 ${point.drawdown.toFixed(2)}%` : ""}`);
    });
    chart.subscribeClick(e => { if (!trade && typeof e.time === "number") { const index = tradeAtTime.get(e.time); if (index !== undefined) selectRef.current?.(index); } });
    chart.timeScale().fitContent();
    return () => { chartRef.current = null; chart.remove(); };
  }, [report, trade, mode]);
  function focusDrawdown() {
    const summary = drawdownSummary(report);
    if (!summary || summary.trough.drawdown >= 0) return;
    const start = timeNumber(summary.peak.bar_open_time ?? summary.peak.time), end = timeNumber(summary.recovery?.bar_open_time ?? summary.recovery?.time ?? report.range.to);
    if (end > start) chartRef.current?.timeScale().setVisibleRange({ from: start as UTCTimestamp, to: end as UTCTimestamp });
  }
  return <div className="research-chart-wrap"><div className="chart-readout" aria-live="off">{hover || "同周期行情 · 入场 / 退出标记"}</div><div className="button-row chart-mini-tools"><span>{trade ? "冻结行情与入场条件" : `行情 / ${mode === "drawdown" ? "峰值回撤（%）" : "账户净值（USDT）"} · 同时间轴`}</span><button onClick={() => chartRef.current?.timeScale().fitContent()}>显示全部</button>{!trade && mode === "drawdown" && <button disabled={!drawdownSummary(report)?.trough.drawdown} onClick={focusDrawdown}>定位最大回撤</button>}</div><div ref={element} className={`research-chart ${trade ? "trade-candle-chart" : "linked-report-chart"}`} aria-label={trade ? "历史交易 K 线" : mode === "equity" ? "回测净值曲线" : "回测回撤曲线"} /></div>;
}
