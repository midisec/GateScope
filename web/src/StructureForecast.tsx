import { patternTime as date, type PatternEvent } from "./patternModel";

export const forecastPrice = (value?: number | null) => value == null || !Number.isFinite(value) ? "—" : value.toLocaleString(undefined, { maximumSignificantDigits: 8 });
const outcomes: Record<string,string> = { PENDING:"尚待验证", TOUCHED:"首次目标已触及（不等于确认）", NOT_REACHED:"期限或结构终止前未触及", AMBIGUOUS:"同根触价与失效，先后不明", CENSORED:"数据缺口，无法评估", UNAVAILABLE:"未发布目标", CONDITIONAL_UNSCORED:"条件目标，未纳入触价统计" };

export function StructureForecast({ event, compact=false }: { event:PatternEvent; compact?:boolean }) {
  const f=event.forecast,original=event.initial_forecast??f;
  if(!f)return compact?null:<p>此历史节点未保存点位预测，不能事后补写。</p>;
  const changed=original?.id!==f.id;
  return <div className="structure-forecast">
    <div><span>{f.target?.label??"价格目标"}</span><strong>{forecastPrice(f.target?.price)}</strong></div>
    {f.target?<small>候选区 {forecastPrice(f.target.lower)}–{forecastPrice(f.target.upper)}{f.status==="ALREADY_IN_ZONE"?" · 发布时已在区内，等待枢轴确认":""}</small>:<small>{f.unavailable_reason}</small>}
    {changed&&<div className="forecast-original"><span>首次预测 {forecastPrice(original?.target?.price)}</span><small>{date(original?.issued_at)}</small></div>}
    <small>{f.source==="THEORY_ONLY"?(f.guideline_fit==="STRUCTURAL_FALLBACK"?"结构约束候选区 · 非斐波那契目标":"发布时使用理论区间"):"发布时按历史样本修正"} · 本次预测发布时的目标校准样本 n={f.history_count}</small>
    {!compact&&<>
      {event.pre_formation_forecast&&<p>形成前最后预测：{forecastPrice(event.pre_formation_forecast.target?.price)} · {event.pre_formation_forecast.target?.label}<br/>发布于 {date(event.pre_formation_forecast.issued_at)}</p>}
      {original?.kind==="CONDITIONAL_MOVE"&&<p>首次识别时结构已经形成，没有事前形成点位预测；这里是形成后发布的条件目标。</p>}
      <p>{f.condition}</p>
      {f.confirmation.map((line,i)=><p key={i}>确认参考：{line.side>0?"收盘高于":"收盘低于"} {forecastPrice(line.price)}，连续 {f.confirmation_closes} 根（斜线按每根时间推进）</p>)}
      <p>首次目标验证：{outcomes[event.forecast_outcome?.status??""]??"尚待验证"}{event.forecast_outcome?.observed_time?` · ${date(event.forecast_outcome.observed_time)}`:""}</p>
      {event.formation_observation&&<p>实际后续枢轴 {forecastPrice(event.formation_observation.price)} · {date(event.formation_observation.pivot_time)}<br/>确认可知时间 {date(event.formation_observation.known_at)}{!event.formation_observation.predicted_before_pivot&&" · 拐点早于发布，不能算事前预测命中"}</p>}
      {event.structure_formed_at&&<p>完整结构末点 {forecastPrice(event.structure_formed_at.price)} · 末点时间 {date(event.structure_formed_at.pivot_time)}<br/>结构形成可知时间 {date(event.structure_formed_at.known_at)}</p>}
      <small>本次发布 {date(f.issued_at)} · 期限 {date(f.deadline_time)}。历史修正只使用此前结束的样本；目标价不是保证，也不是目标命中概率。</small>
    </>}
  </div>;
}
