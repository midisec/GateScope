import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, jsonBody } from "./api";
import { InstrumentPicker, type InstrumentChoice } from "./InstrumentPicker";
import type { Spec } from "./researchModel";

export type StructureCatalogItem = { id: string; name: string; group: string; family?: string };
export type StructureRequest = { instrument_id: string; timeframe: string; bar_limit: number; from_time: string | null; to_time: string | null; spec: Spec };
export const yearBarCounts: Record<string, number> = { "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520, "1h": 8760, "4h": 2190, "8h": 1095, "1d": 365 };

export function structureRequestError(request: StructureRequest): string | null {
  if (!request.instrument_id) return "请搜索并选择交易标的";
  if (!request.spec.structure_patterns?.length) return "请至少选择一个形态或波浪";
  if (!Number.isInteger(request.bar_limit) || request.bar_limit < 300 || request.bar_limit > 550000) return "同源回测支持 300–550000 根 K 线";
  if ([request.from_time, request.to_time].some(value => value !== null && !Number.isFinite(Date.parse(value)))) return "请选择有效的开始或结束时间";
  if (request.from_time && request.to_time && Date.parse(request.from_time) >= Date.parse(request.to_time)) return "开始时间必须早于结束时间";
  return null;
}

export function StructureBacktestBuilder({ defaults, catalog, onCreated, onBusy }: { defaults: Spec; catalog: StructureCatalogItem[]; onCreated: (run: { id: string }) => void; onBusy: (value: boolean) => void }) {
  const [instrument, setInstrument] = useState(""), [timeframe, setTimeframe] = useState("4h"), [limit, setLimit] = useState(yearBarCounts["4h"]);
  const [market, setMarket] = useState(""), [marketLoading, setMarketLoading] = useState(false), [marketChoice, setMarketChoice] = useState<InstrumentChoice>();
  const marketRequest = useRef<AbortController | null>(null);
  const [start, setStart] = useState(""), [end, setEnd] = useState(""), [query, setQuery] = useState(""), [group, setGroup] = useState("");
  const [spec, setSpec] = useState<Spec>(() => ({ ...defaults, engine_mode: "STRUCTURE", strategy: "trend", rule_ids: [], direction: "BOTH", structure_patterns: catalog.map(item => item.id), structure_entry_phase: "CONFIRMED", pattern_confidence: 0, entry_delay_bars: 0, walk_forward_folds: 4, availability_mode: "HISTORICAL_CLOSE" }));
  const [error, setError] = useState(""), [busy, setBusy] = useState(false), lock = useRef(false);
  useEffect(() => () => marketRequest.current?.abort(), []);
  const spot = market === "SPOT";
  async function chooseInstrument(item: InstrumentChoice) {
    marketRequest.current?.abort();
    setInstrument(item.id); setMarketChoice(item); setMarket(""); setError("");
    const applyMarket = (value: string) => { const normalized = value.toUpperCase(); setMarket(normalized); if (normalized === "SPOT") setSpec(current => ({ ...current, direction: "LONG" })); };
    if (item.market) { setMarketLoading(false); applyMarket(item.market); return; }
    const controller = new AbortController(); marketRequest.current = controller; setMarketLoading(true);
    try {
      const data = await api<{ items: InstrumentChoice[] }>(`/api/v2/instruments?q=${encodeURIComponent(item.symbol)}&limit=100`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const matched = data.items.find(candidate => candidate.id === item.id);
      if (!matched?.market) throw new Error("无法确认该标的的市场类型，请重试或重新搜索选择");
      applyMarket(matched.market);
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "标的信息读取失败，请重试"); }
    finally { if (!controller.signal.aborted) setMarketLoading(false); }
  }
  const visible = catalog.filter(item => (!group || item.group === group) && `${item.name} ${item.id}`.toLowerCase().includes(query.trim().toLowerCase()));
  const number = (key: keyof Spec, label: string, min: number, max: number, step = 1) => <label>{label}<input type="number" required min={min} max={max} step={step} value={Number(spec[key])} onChange={event => setSpec(current => ({ ...current, [key]: Number(event.target.value) }))} /></label>;
  async function submit(event: FormEvent) {
    event.preventDefault(); if (lock.current || marketLoading) return;
    if (instrument && !market) { setError("请先确认标的的市场类型，再开始回测"); return; }
    if ([start, end].some(value => value && !Number.isFinite(Date.parse(value)))) { setError("请选择有效的开始或结束时间"); return; }
    const request: StructureRequest = { instrument_id: instrument, timeframe, bar_limit: limit, from_time: start ? new Date(start).toISOString() : null, to_time: end ? new Date(end).toISOString() : null, spec };
    const invalid = structureRequestError(request); if (invalid) { setError(invalid); return; }
    lock.current = true; setBusy(true); onBusy(true); setError("");
    try { onCreated(await api<{ id: string }>("/api/v2/research/backtests", { method: "POST", body: jsonBody(request) })); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "提交失败，请检查后重试"); }
    finally { lock.current = false; setBusy(false); onBusy(false); }
  }
  return <form onSubmit={submit} aria-label="同源形态回测配置"><fieldset disabled={busy} className="structure-builder-fields"><div className="research-form">
    <div className="research-wide"><InstrumentPicker value={instrument} onChange={chooseInstrument} />{marketLoading && <p className="muted-copy" role="status">正在确认标的市场类型…</p>}{instrument && !market && !marketLoading && marketChoice && <button type="button" onClick={() => void chooseInstrument(marketChoice)}>重试读取标的信息</button>}</div>
    <label>K 线周期<select aria-label="K 线周期" value={timeframe} onChange={event => { const next = event.target.value; setTimeframe(next); setLimit(yearBarCounts[next]); }}>{Object.keys(yearBarCounts).map(period => <option key={period}>{period}</option>)}</select></label>
    <label>最多读取 K 线<input type="number" required min={300} max={550000} value={limit} onChange={event => setLimit(Number(event.target.value))} /><small>一年 {yearBarCounts[timeframe].toLocaleString()} 根</small></label>
    <label>开始时间（可选）<input type="datetime-local" value={start} onChange={event => setStart(event.target.value)} /></label><label>结束时间（可选）<input type="datetime-local" value={end} onChange={event => setEnd(event.target.value)} /></label>
    <fieldset className="research-wide research-rule-picker"><legend>同源形态与波浪 · 已选 {spec.structure_patterns?.length ?? 0} / {catalog.length}</legend><div className="rule-picker-toolbar"><button type="button" onClick={() => setSpec(current => ({ ...current, structure_patterns: catalog.map(item => item.id) }))}>全选形态</button><button type="button" onClick={() => setSpec(current => ({ ...current, structure_patterns: [] }))}>清空选择</button><span>每个候选保留原始识别和结果</span></div><div className="rule-picker-search"><input aria-label="搜索同源形态" placeholder="搜索双底、旗形、波浪…" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="同源形态分类" value={group} onChange={event => setGroup(event.target.value)}><option value="">全部分类</option>{[...new Set(catalog.map(item => item.group))].map(name => <option key={name}>{name}</option>)}</select></div>
      <div className="structure-patterns">{visible.map(item => <label key={item.id}><input type="checkbox" checked={spec.structure_patterns?.includes(item.id) ?? false} onChange={event => setSpec(current => ({ ...current, structure_patterns: event.target.checked ? [...current.structure_patterns ?? [], item.id] : current.structure_patterns?.filter(id => id !== item.id) }))} /><span>{item.name}<small>{item.group}</small></span></label>)}</div>{!visible.length && <p className="muted-copy">没有匹配项，已选形态仍保留。</p>}</fieldset>
    <label>入场事件阶段<select aria-label="入场事件阶段" value={spec.structure_entry_phase} onChange={event => setSpec(current => ({ ...current, structure_entry_phase: event.target.value as "FORMED" | "CONFIRMED" }))}><option value="CONFIRMED">突破已确认</option><option value="FORMED">结构已形成（未必突破）</option></select></label>
    <label>模拟方向<select aria-label="模拟方向" value={spec.direction} onChange={event => setSpec(current => ({ ...current, direction: spot ? "LONG" : event.target.value }))}><option value="BOTH" disabled={spot}>按形态方向，多空均观察</option><option value="LONG">仅做多</option><option value="SHORT" disabled={spot}>仅做空</option></select>{spot && <small>现货只做多，不模拟借币做空</small>}</label>
    {number("pattern_confidence", "最低几何评分（非胜率）", 0, 1, .01)}{number("entry_delay_bars", "额外入场延迟（根）", 0, 10)}{number("max_holding_bars", "最长持有（根）", 1, 200)}{number("walk_forward_folds", "滚动验证分段", 2, 8)}
    <fieldset className="research-wide rule-regimes"><legend>入场环境 · 不选即全部保留</legend>{[["UP", "上涨"], ["DOWN", "下跌"], ["RANGE", "震荡"]].map(([value, label]) => <label key={value}><input type="checkbox" checked={spec.regimes?.includes(value) ?? false} onChange={event => setSpec(current => ({ ...current, regimes: event.target.checked ? [...current.regimes ?? [], value] : current.regimes?.filter(item => item !== value) }))} />{label}</label>)}</fieldset>
    <div className="research-wide structure-cost-grid">{number("fee_bps", "单边手续费（bp）", 0, 100, .1)}{number("slippage_bps", "单边滑点（bp）", 0, 100, .1)}{number("funding_bps_per_8h", "每 8 小时资金费估算（bp）", 0, 100, .1)}</div>
    <p className="research-wide research-context-note">按历史每根收盘回放。选择「已形成」仍需满足原始突破确认规则后才模拟入场。止损按冻结价格触及执行（非结构收盘失效）；成本和成交均为模拟假设。</p>
  </div></fieldset>{error && <div className="error-banner" role="alert">{error}</div>}<div className="button-row"><span className="muted-copy">提交后由后台分批处理，可离开页面</span><button className="primary" disabled={busy || marketLoading || (!!instrument && !market)}>{busy ? "提交中…" : marketLoading ? "确认标的信息…" : "开始同源回测"}</button></div></form>;
}
