import { useState, type FormEvent } from "react";
import { api, jsonBody } from "./api";
import { InstrumentPicker } from "./InstrumentPicker";
import { Dialog, PageState, useAction } from "./ui";
import type { AlertRule, NotificationChannel, PatternCatalogItem } from "./types";

export function AlertRuleEditor({ rule, channels, patterns, onClose, onSaved }: { rule: AlertRule; channels: NotificationChannel[]; patterns: PatternCatalogItem[]; onClose: () => void; onSaved: (rule: AlertRule) => void }) {
  const c = rule.version.condition;
  const [instrument, setInstrument] = useState(c.instrument_id), [selected, setSelected] = useState(c.pattern_ids);
  const [channelIds, setChannelIds] = useState(rule.version.routing.channel_ids);
  const [error, setError] = useState("");
  const action = useAction(setError);
  function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = new FormData(e.currentTarget);
    if (!instrument || !selected.length || !String(form.get("name")).trim()) { setError("请填写名称、选择币种和至少一个形态"); return; }
    setError("");
    void action.run("保存中…", async () => {
      const updated = await api<AlertRule>(`/api/v2/alert-rules/${rule.id}`, { method: "PUT", headers: { "If-Match": `"${rule.etag}"` }, body: jsonBody({
        name: String(form.get("name")).trim(),
        condition: { schema_version: 1, instrument_id: instrument, timeframe: form.get("timeframe"), pattern_ids: selected, match_mode: form.get("match_mode"), minimum_confidence: Number(form.get("confidence")), confirmed_only: form.get("phase") !== "formed", coincidence_bars: Number(form.get("coincidence")), cooldown_seconds: Number(form.get("cooldown")) },
        routing: { schema_version: 1, channel_ids: channelIds },
      }) }); onSaved(updated);
    });
  }
  const choices = [...patterns.filter(p => !p.experimental || selected.includes(p.pattern_id)), ...selected.filter(id => !patterns.some(p => p.pattern_id === id)).map(id => ({ pattern_id: id, name: id }))];
  return <Dialog title="编辑已保存策略" busy={!!action.busy} onClose={onClose}><form className="strategy-form" onSubmit={save}>
    <label className="strategy-name">策略名称<input name="name" required maxLength={120} defaultValue={rule.name} /></label>
    <InstrumentPicker value={instrument} selectedLabel={instrument === c.instrument_id ? rule.symbol || "原有标的（点击可更换）" : undefined} onChange={i => setInstrument(i.id)} />
    <label>周期<select aria-label="周期" name="timeframe" defaultValue={c.timeframe}>{["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"].map(t => <option key={t}>{t}</option>)}</select></label>
    <label>组合<select name="match_mode" defaultValue={c.match_mode}><option value="ANY">任一形态</option><option value="ALL">全部同时</option></select></label>
    <label>通知时机<select name="phase" defaultValue={c.confirmed_only ? "confirmed" : "formed"}><option value="confirmed">突破确认后 · 做单观察</option><option value="formed">已形成预告 + 突破确认</option></select></label>
    <label>形态评分阈值<input name="confidence" type="number" min={0} max={1} step={.01} defaultValue={c.minimum_confidence} required /></label>
    <label>确认窗口（K 线）<input name="coincidence" type="number" min={1} max={100} defaultValue={c.coincidence_bars} required /></label>
    <label>冷却（秒）<input name="cooldown" type="number" min={0} max={604800} defaultValue={c.cooldown_seconds} required /></label>
    <fieldset className="strategy-submit"><legend>形态条件</legend><div className="editor-choice-grid">{choices.map(p => <label key={p.pattern_id} className="checkline"><input type="checkbox" checked={selected.includes(p.pattern_id)} onChange={e => setSelected(old => e.target.checked ? [...old, p.pattern_id] : old.filter(id => id !== p.pattern_id))} />{p.name}</label>)}</div></fieldset>
    <fieldset className="strategy-submit"><legend>通知通道（不选则仅站内）</legend>{[...channels, ...channelIds.filter(id => !channels.some(ch => ch.id === id)).map(id => ({ id, alias: "原通道已不可用，请取消勾选", status: "UNAVAILABLE" }))].map(ch => <label key={ch.id} className="checkline"><input type="checkbox" checked={channelIds.includes(ch.id)} onChange={e => setChannelIds(old => e.target.checked ? [...old, ch.id] : old.filter(id => id !== ch.id))} />{ch.alias}</label>)}</fieldset>
    <div className="strategy-submit"><small>通知附原始形态点位和看图按钮，仅供观察，不自动下单。冷却设为 0 可逐个通知，同一事件仍自动去重。</small><small>保存为 v{rule.current_version + 1} · 保持{rule.status === "ACTIVE" ? "运行" : "暂停"}状态 · 历史信号不变</small><PageState error={error} /><button className="primary" disabled={!!action.busy}>{action.busy || "保存更新"}</button></div>
  </form></Dialog>;
}
