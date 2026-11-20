import { useEffect, useState } from "react";
import { Dialog, Pagination, pageSlice } from "./ui";
import { sampleCountLabel as count, type CompletionRow, type PatternMeta } from "./patternModel";
import { probabilityInterval, probabilityLabel } from "./StructureConfidence";
import { ProbabilityReliability } from "./ProbabilityReliability";

export function CompletionStats({ rows, metas, partial = false }: { rows?: CompletionRow[]; metas: PatternMeta[]; partial?: boolean }) {
  const [phase,setPhase] = useState("FORMED"), [bucket,setBucket] = useState("ALL"), [page,setPage] = useState(0);
  const [comparison, setComparison] = useState<{ name: string; row: CompletionRow }>();
  useEffect(()=>setPage(0),[phase,bucket,metas.map(m=>m.id).join(":")]);
  const paged = pageSlice(metas,page,8);
  if (!rows) return <div className="pattern-caption">旧版本报告不包含失败候选，请更新历史统计生成新版档案。</div>;
  return <section aria-label="候选完成统计"><div className="pattern-filters">
    <label>首次阶段<select aria-label="候选统计阶段" value={phase} onChange={e=>setPhase(e.target.value)}><option value="FORMING">形成中候选 → 最终确认</option><option value="FORMED">已形成结构 → 最终确认</option></select></label>
    <label>初始匹配分组<select aria-label="初始匹配分组" value={bucket} onChange={e=>setBucket(e.target.value)}><option value="ALL">全部评分</option><option value="UNSCORED">未完成 · 无评分</option><option value="LOW">0–69 分</option><option value="MID">70–84 分</option><option value="HIGH">85–100 分</option></select></label></div>
    <div className="pattern-stats" role="region" aria-label="候选完成统计表" tabIndex={0}><table><thead><tr><th>形态 / 波浪</th><th>该阶段候选</th><th>曾形成 / 确认</th><th>失效 / 到期</th><th>待定 / 中断</th><th>有效样本 / 失败</th><th>重叠排除</th><th>形成率 / 区间</th><th>历史确认率</th><th>确认 95% 校正区间</th><th>校准 Brier</th></tr></thead><tbody>
      {paged.items.map(m => <CompletionStatRow key={m.id} meta={m} phase={phase} partial={partial} row={rows.find(v => v.pattern === m.id && v.phase === phase && v.score_bucket === bucket)} onReliability={row=>setComparison({ name:m.name, row })} />)}
      {!paged.items.length&&<tr><td colSpan={11}>没有匹配的形态类型</td></tr>}</tbody></table></div>
    {paged.pages>1&&<Pagination label="候选完成统计" page={paged.page} pages={paged.pages} total={metas.length} onChange={setPage}/>}
    <div className="pattern-caption">同一已结束样本：形成后再失效计入形成成功、确认失败。待定、中断和重叠样本排除。区间越宽，历史支持越不确定；确认不等于盈利。</div>
    {comparison?.row.reliability && <Dialog title={`${comparison.name} · 概率可靠性`} onClose={()=>setComparison(undefined)}><ProbabilityReliability value={comparison.row.reliability} phase={comparison.row.phase} /></Dialog>}
  </section>;
}

export function CompletionStatRow({ meta, phase, row, partial = false, onReliability }: { meta: PatternMeta; phase: string; row?: CompletionRow; partial?: boolean; onReliability?: (row: CompletionRow) => void }) {
  const unavailable = phase === "FORMING" && meta.supports_forming === false;
  if (unavailable || !row) return <tr><th>{meta.name}<small>{meta.group}</small></th><td colSpan={10} className="pattern-sample-empty">— · {unavailable ? "此形态不支持形成中预判，请查看已形成阶段" : partial ? "回放进行中，该阶段暂无统计" : "该阶段暂无统计"}</td></tr>;
  const failures = row.sample_count != null && row.successes != null && row.successes >= 0 && row.successes <= row.sample_count ? row.sample_count - row.successes : null;
  return <tr><th>{meta.name}<small>{meta.group}</small></th><td>{count(row.total)}</td><td>{count(row.formed_count)} / {count(row.CONFIRMED)}</td><td className="negative">{count(row.INVALIDATED)} / {count(row.EXPIRED)}</td><td>{count(row.pending)} / {count(row.CENSORED)}</td><td>{count(row.sample_count)} / {count(failures)}</td><td>{count(row.overlap_excluded)}</td><td>{phase === "FORMING" ? probabilityLabel(row.formation) : "已形成"}{row.formation?.interval && <small>{probabilityInterval(row.formation)}</small>}</td><td>{probabilityLabel(row)}{row.sample_count === 0 && partial && <small>回放尚未完成</small>}</td><td>{probabilityInterval(row)}</td><td>{row.brier == null ? "—" : `${row.brier.toFixed(3)} / n=${count(row.calibration_count)}`}{row.reliability && onReliability && <button type="button" aria-label={`${meta.name}概率可靠性对比`} onClick={()=>onReliability(row)}>近期对比</button>}</td></tr>;
}
