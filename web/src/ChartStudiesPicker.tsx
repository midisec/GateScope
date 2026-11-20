import { useState } from "react";
import { Dialog } from "./ui";
import { coinglassBundleStatus, coinglassRefreshState, coinglassRetrySeconds, coinglassStatus, type CoinglassBundle } from "./coinglassModel";
import { COINGLASS_CHART_CATALOG, coinglassChartMetricStatus } from "./curatedCoinglass";
import { useCoinglassClock } from "./useCoinglassClock";
import {
  VPVR_LIMITS, clearChartStudies, filterChartStudies, normalizeChartStudiesSettings, selectedChartStudies,
  setChartStudyEnabled, toggleChartStudy, type ChartStudiesCategory, type ChartStudiesSettings,
  type ChartStudyId, type ChartStudySearchItem,
} from "./chartStudies";
import "./chartStudies.css";

export type ChartStudiesPickerProps = {
  settings: ChartStudiesSettings;
  onChange: (settings: ChartStudiesSettings) => void;
  onClose: () => void;
  bundle?: CoinglassBundle;
  loading?: boolean;
  error?: string;
  onRefresh: () => void;
};

const categories: { id: ChartStudiesCategory; label: string }[] = [
  { id: "all", label: "全部" }, { id: "coinglass", label: "Coinglass" }, { id: "volume", label: "量价" },
];
const subtitles: Record<ChartStudyId, string> = {
  oi: "美元估值 · 未平仓合约", oi_change: "相邻原生周期的 OI 美元估值变化",
  funding: "百分数 · 已核实单位", lsr: "全账户数量比 · 中性基线 1",
  liquidation_long: "美元 · 多单已发生爆仓", liquidation_short: "美元 · 已发生金额，向下镜像",
  vpvr: "主图侧边 · 优先 1m，细周期 OHLCV 估算",
};
const searchAliases = { oi: "持仓量 未平仓 open interest", oi_change: "OI 持仓量 变化 估值",
  funding: "funding rate", lsr: "long short ratio", liquidation_long: "long liquidation 爆仓 多头",
  liquidation_short: "short liquidation 爆仓 空头" };
const catalog: ChartStudySearchItem[] = [
  ...COINGLASS_CHART_CATALOG.map(item => ({ id: item.id, title: item.title, description: item.description,
    category: "coinglass" as const, keywords: `Coinglass ${item.unit} ${searchAliases[item.id]}` })),
  { id: "vpvr", title: "VPVR 可见区间成交量分布", description: "主图侧边 · 优先 1m，细周期 OHLCV 估算", category: "volume",
    keywords: "成交量 分布 筹码 volume profile visible range POC VAH VAL 量价" },
];

function availability(id: ChartStudyId, bundle: CoinglassBundle | undefined, loading: boolean, now: number) {
  if (id === "vpvr") return { label: "细周期估算", tone: "local", reason: "按可视已收盘区间读取细周期缓存；1m 图使用当前最小周期" };
  if (!bundle) return { label: loading ? "读取中" : "按需读取", tone: "waiting", reason: "勾选后读取当前币种数据" };
  if (["NO_CONFIG", "NOT_CONFIGURED", "DISABLED"].includes(bundle.status)) {
    return { label: bundle.status === "DISABLED" ? "已停用" : "未配置", tone: "waiting", reason: "可先选择，再配置数据源" };
  }
  const state = coinglassChartMetricStatus(bundle, id, now);
  if (state.status === "READY") return { label: "可用", tone: "ready", reason: state.reason };
  if (state.status === "HISTORICAL") return { label: "历史缓存", tone: "historical", reason: state.reason };
  const refresh = coinglassRefreshState(bundle, now);
  const label = refresh === "COLLECTING" ? "采集中" : refresh === "COOLDOWN" ? "等待额度"
    : refresh === "QUEUED" || refresh === "SCHEDULED" ? "已排队" : "暂无数据";
  return { label, tone: "waiting", reason: state.reason };
}

/** Selections update immediately. This component neither fetches data nor saves a layout. */
export function ChartStudiesPicker({ settings, onChange, onClose, bundle, loading = false, error, onRefresh }: ChartStudiesPickerProps) {
  const [query, setQuery] = useState(""), [category, setCategory] = useState<ChartStudiesCategory>("all");
  const value = normalizeChartStudiesSettings(settings), selected = selectedChartStudies(value);
  const visible = filterChartStudies(catalog, query, category);
  const now = useCoinglassClock(bundle?.retry_at, loading);
  const retrySeconds = coinglassRetrySeconds(bundle, now);
  const refreshDisabled = loading || retrySeconds > 0 || (!bundle && value.coinglassMetrics.length === 0);
  const needsConfiguration = Boolean(bundle && ["NO_CONFIG", "NOT_CONFIGURED", "DISABLED"].includes(bundle.status));
  const sourceStatus = bundle ? coinglassStatus(coinglassBundleStatus(bundle, now)) : loading ? "读取中" : "按需读取";
  return <Dialog title="精选指标" onClose={onClose}><section className="chart-studies-picker">
    <div className="chart-studies-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg>
      <input type="search" autoFocus aria-label="搜索精选指标" placeholder="搜索 OI、资金费率、VPVR…" maxLength={120}
        value={query} onChange={event => setQuery(event.target.value)} />
      {query && <button type="button" aria-label="清除指标搜索" onClick={() => setQuery("")}>✕</button>}
    </div>
    <div className="chart-studies-toolbar"><div className="chart-studies-categories" role="group" aria-label="指标分类">
      {categories.map(item => <button type="button" key={item.id} aria-pressed={category === item.id}
        onClick={() => setCategory(item.id)}>{item.label}</button>)}
    </div><span>已选 {selected.length} 项</span></div>
    <div className="chart-studies-selection" aria-label="已选精选指标">
      {selected.length ? <><div className="chart-studies-chips">{selected.map(id => <button type="button" key={id}
        aria-label={`移除 ${catalog.find(item => item.id === id)?.title ?? id}`}
        onClick={() => onChange(setChartStudyEnabled(value, id, false))}>{id === "vpvr" ? "VPVR" : catalog.find(item => item.id === id)?.title ?? id}<span aria-hidden="true">×</span></button>)}</div>
        <button className="chart-studies-clear" type="button" onClick={() => onChange(clearChartStudies(value))}>清空已选</button></>
        : <span className="chart-studies-empty-selection">勾选指标即可显示在图表上</span>}
    </div>
    <div className="chart-studies-source"><div><span>Coinglass · {sourceStatus}</span>
      {bundle?.scope && <small>{bundle.scope.symbol} · {bundle.timeframe} · {bundle.scope.exchange}</small>}</div>
      {needsConfiguration ? <a href="/integrations/data-sources">配置数据源</a> : <button type="button" aria-label="刷新 Coinglass 数据"
        disabled={refreshDisabled} aria-busy={loading}
        onClick={() => { if (!refreshDisabled) onRefresh(); }}>{loading ? "读取中…" : retrySeconds > 0 ? `${retrySeconds} 秒后可刷新` : "刷新数据"}</button>}
    </div>
    {error && <div className="chart-studies-error" role="alert">{error}</div>}
    <div className="chart-studies-options" role="group" aria-label="可选精选指标">
      {visible.map(item => {
        const state = availability(item.id, bundle, loading, now), checked = selected.includes(item.id);
        return <label key={item.id} className={`chart-study-option ${checked ? "selected" : ""}`}>
          <input type="checkbox" aria-label={`显示 ${item.title}`} checked={checked}
            onChange={() => onChange(toggleChartStudy(value, item.id))} />
          <span className="chart-study-copy"><strong>{item.title}</strong><small>{subtitles[item.id]}</small></span>
          <span className={`chart-study-availability ${state.tone}`} title={state.reason}>{state.label}</span>
        </label>;
      })}
      {!visible.length && <p className="chart-studies-no-match">没有匹配的指标，已选项仍保留。<button type="button" onClick={() => { setQuery(""); setCategory("all"); }}>查看全部</button></p>}
    </div>
    {value.vpvr.enabled && <fieldset className="chart-studies-vpvr"><legend>VPVR 显示参数</legend>
      <label><span>价格分层</span><select aria-label="VPVR 价格分层" value={value.vpvr.rows}
        onChange={event => onChange(normalizeChartStudiesSettings({ ...value, vpvr: { ...value.vpvr, rows: Number(event.target.value) } }))}>
        <option value={24}>24 层</option><option value={48}>48 层</option><option value={96}>96 层</option></select></label>
      <label><span>价值区间 <output>{value.vpvr.valueAreaPercent}%</output></span><input type="range" aria-label="VPVR 价值区间百分比"
        min={VPVR_LIMITS.valueArea.min} max={VPVR_LIMITS.valueArea.max} step={1} value={value.vpvr.valueAreaPercent}
        onChange={event => onChange(normalizeChartStudiesSettings({ ...value, vpvr: { ...value.vpvr, valueAreaPercent: Number(event.target.value) } }))} /></label>
      <label><span>侧边宽度 <output>{value.vpvr.widthPercent}%</output></span><input type="range" aria-label="VPVR 侧边宽度百分比"
        min={VPVR_LIMITS.width.min} max={VPVR_LIMITS.width.max} step={1} value={value.vpvr.widthPercent}
        onChange={event => onChange(normalizeChartStudiesSettings({ ...value, vpvr: { ...value.vpvr, widthPercent: Number(event.target.value) } }))} /></label>
    </fieldset>}
    <details className="chart-studies-method"><summary>数据口径与计算方法</summary>
      <p>Coinglass 副图仅使用匹配的合约、结算币、周期和已核实单位；旧数据标为历史缓存。OI 美元估值变化包含价格影响，不代表净资金流入。爆仓为已发生金额，不是未来清算热力图。</p>
      <p>VPVR 按可视已收盘区间读取细周期缓存，优先 1m，宽区间自适应。显示来源周期与时间覆盖率，缺口不以粗周期补造；仍是 OHLCV 估算，不是逐笔成交或真实持仓成本。</p>
    </details>
    <footer className="chart-studies-footer"><span>支持多选 · 勾选后生效</span><button className="primary" type="button" onClick={onClose}>完成</button></footer>
  </section></Dialog>;
}
