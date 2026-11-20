import { useEffect, useState } from "react";
import { PageState, Pagination } from "./ui";
import { useResearchResource } from "./useResearchResource";
import { JsonEvidence } from "./ShadowWorkspace";
import { researchDate } from "./researchV2Model";
import { phaseLabels } from "./structureResearchModel";

type Transition = { candidate_id: string; sequence: number; observed_time: number; phase: string; from_phase: string | null; reason: string; record: { pattern: string; latest?: { name?: string } } };

export function StructureEvidence({ runId, refresh, names }: { runId: string; refresh: number; names: Record<string, string> }) {
  const [page, setPage] = useState(0);
  const resource = useResearchResource<{ items: Transition[]; total: number; has_more: boolean }>(`/api/v2/research/backtests/${encodeURIComponent(runId)}/events?limit=20&offset=${page * 20}`, refresh);
  useEffect(() => { if (resource.data && page > 0 && page * 20 >= resource.data.total) setPage(Math.max(0, Math.ceil(resource.data.total / 20) - 1)); }, [resource.data, page]);
  return <section className="panel research-report"><header><h2>原始识别事件</h2><span>形成中、失败及过期全部保留</span></header><p className="research-context-note">按当时观测顺序保存。同一候选可能产生多个生命周期事件，事件数不是独立样本数。</p><PageState loading={resource.loading} error={resource.error} onRetry={resource.retry} />{resource.data && <><div className="research-table-scroll"><table><thead><tr><th>观测时间</th><th>形态</th><th>状态变化</th><th>候选 / 序号</th><th>原始依据</th></tr></thead><tbody>{resource.data.items.map(event => <tr key={`${event.candidate_id}:${event.sequence}`}><td>{researchDate(event.observed_time)}</td><td>{names[event.record.pattern] ?? event.record.latest?.name ?? event.record.pattern}</td><td>{phaseLabels[event.from_phase ?? ""] ?? "首次记录"} → {phaseLabels[event.phase] ?? event.phase}</td><td><code>{event.candidate_id.slice(0, 12)}</code> · {event.sequence}</td><td><details><summary>查看冻结证据</summary><JsonEvidence value={event} /></details></td></tr>)}</tbody></table></div>{!resource.data.items.length && <p className="muted-copy">尚无已保存识别事件。</p>}<Pagination label="原始识别事件" page={page} pages={Math.max(1, Math.ceil(resource.data.total / 20))} total={resource.data.total} busy={resource.loading} onChange={setPage} /></>}</section>;
}
