import { coinglassNumber } from "./coinglassModel";
import { priceText } from "./chartModel";
import type { CoinglassChartStudy } from "./curatedCoinglass";
import type { VolumeProfileResult } from "./volumeProfile";
import type { FineVolumeProfileView } from "./useFineVolumeProfile";
import { fineCoverageLabel, fineProfileIsPartial } from "./fineVolumeProfileModel";
import "./chartStudyReadouts.css";

export function profilePriceLabel(value: number) {
  return value !== 0 && Math.abs(value) < 1e-8 ? value.toExponential(6) : priceText(value);
}

export function ChartStudyReadouts({ studies, profileEnabled, profile: localProfile, fineProfile, onManage }: {
  studies: CoinglassChartStudy[]; profileEnabled?: boolean; profile?: VolumeProfileResult; fineProfile?: FineVolumeProfileView; onManage?: () => void;
}) {
  if (!studies.length && !profileEnabled) return null;
  const detail = fineProfile?.bundle, profile = fineProfile ? detail?.profile : localProfile;
  const partial = !!detail && fineProfileIsPartial(detail);
  const unknownVolumeUnit = detail?.quality_flags.includes("SOURCE_VOLUME_UNIT_UNVERIFIED");
  const retrySeconds = Math.max(0, Math.ceil(((fineProfile?.retryAt ?? 0) - Date.now()) / 1000));
  const fineMessage = fineProfile?.phase === "ERROR" ? fineProfile.error : fineProfile?.phase === "COOLDOWN"
    ? retrySeconds > 0 ? `${retrySeconds} 秒后可重试` : "限流等待结束，可重试" : fineProfile?.phase === "PAUSED" ? "页面恢复后读取"
    : fineProfile?.phase === "WAITING" ? "可视区间暂无已收盘 K 线" : "读取可视区间细周期数据…";
  return <div className="chart-study-readouts" aria-label="精选指标读数">
    {studies.map(study => <div className={`study-readout ${study.status.toLowerCase()}`} key={study.id}
      data-study-id={study.id} data-study-status={study.status} data-study-points={study.validPoints} data-study-instrument={study.instrumentId ?? ""}
      title={`${study.description} ${study.statusReason}`}>
      <i style={{ background: study.color }} /><strong>{study.title}</strong>
      <span>{study.latest ? coinglassNumber(study.latest.value, study.unit) : "—"}</span>
      <small>{study.status === "READY" ? "已收盘" : study.status === "HISTORICAL" ? "历史参考" : study.statusReason}</small>
    </div>)}
    {profileEnabled && <div className={`study-readout vpvr-readout${partial ? " partial" : ""}${fineProfile?.phase === "ERROR" ? " unavailable" : ""}`}
      data-vpvr-status={detail?.status ?? fineProfile?.phase ?? profile?.status ?? "CALCULATING"}
      data-vpvr-first={profile?.firstTime ?? ""} data-vpvr-last={profile?.lastTime ?? ""}
      data-vpvr-bars={detail?.used_bars ?? profile?.includedCount ?? 0} data-vpvr-poc={profile?.poc ?? ""} data-vpvr-bins={profile?.effectiveBins ?? 0}
      data-vpvr-source={detail?.source_timeframe ?? ""} data-vpvr-time-coverage={detail?.coverage ?? ""}
      data-vpvr-instrument={detail?.instrument_id ?? ""} data-vpvr-from={detail?.from_time ?? ""} data-vpvr-to={detail?.to_time ?? ""}
      data-vpvr-request-key={fineProfile?.key ?? ""}>
      <i style={{ background: "#f5c56a" }} /><strong>VPVR · {fineProfile ? "细周期估算" : "K线估算"}</strong>
      {detail && <><small>来源 {detail.source_timeframe}{detail.source_timeframe === detail.timeframe ? detail.timeframe === "1m" ? " · 当前最小周期" : " · 与图表同周期" : detail.quality_flags.includes("ADAPTIVE_SOURCE_TIMEFRAME") ? " · 自适应明细" : ""}</small>
        <small>{detail.used_bars.toLocaleString()} / {detail.expected_bars.toLocaleString()} 根</small>
        <small className="vpvr-time-coverage">时间覆盖 {fineCoverageLabel(detail.coverage)}{partial ? " · 部分数据" : ""}</small></>}
      {profile?.status === "AVAILABLE" ? <>
        <span>POC {profilePriceLabel(profile.poc!)}</span><span>VAH {profilePriceLabel(profile.vah!)}</span><span>VAL {profilePriceLabel(profile.val!)}</span>
        <small>{fineProfile ? "" : `${profile.includedCount} 根 · `}VA {Math.round(profile.valueArea * 100)}%</small>
      </> : <span role={fineProfile?.phase === "ERROR" ? "alert" : undefined}>{profile?.status === "ZERO_VOLUME" ? "已有样本成交量为零" : detail?.status === "NO_DATA" ? unknownVolumeUnit ? "已有源数据成交量单位未核实，暂不混合计算" : "该区间暂无可用细周期数据"
        : fineProfile ? fineMessage : profile?.status === "NO_VALID_BARS" ? "可视区间无有效 K 线" : profile?.status === "NO_VISIBLE_BARS" ? "可视区间暂无 K 线" : "计算可视区间…"}</span>}
      {unknownVolumeUnit && detail?.status !== "NO_DATA" && <small>部分源数据成交量单位未核实，已排除</small>}
      {!!profile?.excludedUnclosedCount && <small>未收盘已排除</small>}
      {!!profile?.excludedInvalidCount && <small>{profile.excludedInvalidCount} 根无效数据已排除</small>}
      {!!profile?.unknownFinalityCount && <small>含收盘状态未标记的旧数据</small>}
      {fineProfile && ["ERROR", "COOLDOWN"].includes(fineProfile.phase) && <button type="button" className="vpvr-retry" disabled={retrySeconds > 0} onClick={fineProfile.reload}>重试</button>}
      <details><summary>口径</summary><p>{fineProfile
        ? "按可视已收盘区间读取服务器细周期缓存，优先 1m，宽区间采用有界自适应周期；不以粗周期补造缺口。时间覆盖率是有效时间槽占比，不是计算准确率；1m 图使用当前最小周期。"
        : "仅统计当前已加载的可视 K 线。"}成交量按每根源 K 线的高低价格范围均匀分配，横柱表示相对成交量。POC 为最大量价区中心，VAH / VAL 为价值区边界；不是逐笔成交、买卖盘或 Coinglass 原生分布。</p></details>
    </div>}
    {onManage && <button type="button" className="study-readout-manage" onClick={onManage}>管理指标</button>}
  </div>;
}
