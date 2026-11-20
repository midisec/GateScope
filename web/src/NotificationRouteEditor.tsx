import { useState, type FormEvent } from "react";
import { api, jsonBody } from "./api";
import { Dialog, PageState } from "./ui";
import type { NotificationChannel, NotificationRoute } from "./types";

const labels: Record<string, string> = { RESEARCH_SIGNAL: "研究信号", RISK_CRITICAL: "关键风险", PROPOSAL: "交易提案", FILL: "成交", TP_SL: "止盈止损", SYSTEM: "系统消息" };
export function NotificationRouteEditor({ route, channels, onClose, onSaved }: { route?: NotificationRoute; channels: NotificationChannel[]; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [events, setEvents] = useState(route?.event_types ?? ["RESEARCH_SIGNAL", "RISK_CRITICAL"]);
  const [quiet, setQuiet] = useState(Boolean(route?.quiet_hours));
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); if (busy) return;
    const form = new FormData(e.currentTarget);
    if (!events.length) { setError("至少选择一种消息事件"); return; }
    setBusy(true); setError("");
    try {
      await api(`/api/v2/notification-routes${route ? `/${route.id}` : ""}`, { method: route ? "PATCH" : "POST", headers: route ? { "If-Match": `"${route.etag}"` } : undefined, body: jsonBody({
        schema_version: 1, name: String(form.get("name")).trim(), channel_id: form.get("channel"), event_types: events,
        symbols: String(form.get("symbols") ?? "").split(/[\s,，]+/).filter(Boolean),
        strategy_ids: route?.strategy_ids ?? [], exchange_account_ids: route?.exchange_account_ids ?? [], environments: route?.environments ?? [],
        minimum_severity: route?.minimum_severity ?? "INFO", digest_mode: route?.digest_mode ?? false,
        quiet_hours: quiet ? { timezone: route?.quiet_hours?.timezone ?? "Asia/Shanghai", start: form.get("quiet_start"), end: form.get("quiet_end") } : null,
        max_per_minute: Number(form.get("rate")), dedupe_seconds: route?.dedupe_seconds ?? 60,
        bypass_quiet_rules: route?.bypass_quiet_rules ?? [{ event_type: "RISK_CRITICAL", environments: [] }], enabled: form.get("enabled") === "on",
      }) }); onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); } finally { setBusy(false); }
  }
  return <Dialog title={route ? "编辑推送路由" : "新建推送路由"} busy={busy} onClose={onClose}><form className="stack-form" onSubmit={save}><label>路由名称<input name="name" required maxLength={120} defaultValue={route?.name ?? ""} /></label><label>通知通道<select name="channel" required defaultValue={route?.channel_id ?? ""}><option value="">请选择有效通道</option>{channels.filter(c => c.status === "ACTIVE").map(c => <option key={c.id} value={c.id}>{c.alias}</option>)}</select></label><fieldset><legend>消息类型</legend>{Object.entries(labels).map(([id, label]) => <label className="checkline" key={id}><input type="checkbox" checked={events.includes(id)} onChange={e => setEvents(old => e.target.checked ? [...old, id] : old.filter(v => v !== id))} />{label}</label>)}</fieldset><label>币种过滤（留空为全部）<input name="symbols" defaultValue={route?.symbols.join(", ")} placeholder="BTC_USDT, ETH_USDT" /></label><label>每分钟上限<input name="rate" type="number" min={1} max={60} required defaultValue={route?.max_per_minute ?? 10} /></label><label className="checkline"><input type="checkbox" checked={quiet} onChange={e => setQuiet(e.target.checked)} />设置免打扰时间（上海时区）</label>{quiet && <div className="form-two"><label>开始<input name="quiet_start" type="time" required defaultValue={route?.quiet_hours?.start ?? "23:00"} /></label><label>结束<input name="quiet_end" type="time" required defaultValue={route?.quiet_hours?.end ?? "07:00"} /></label></div>}<label className="checkline"><input name="enabled" type="checkbox" defaultChecked={route?.enabled ?? true} />启用此路由</label><PageState error={error} /><button className="primary" disabled={busy || !channels.some(c => c.status === "ACTIVE")}>{busy ? "保存中…" : "保存路由"}</button></form></Dialog>;
}
