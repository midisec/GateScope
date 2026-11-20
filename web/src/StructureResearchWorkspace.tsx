import { useEffect, useState } from "react";
import type { Session } from "./types";
import type { Spec } from "./researchModel";
import { stateLabel } from "./researchModel";
import { Dialog, PageState, Pagination } from "./ui";
import { useResearchResource } from "./useResearchResource";
import { StructureBacktestBuilder, type StructureCatalogItem } from "./StructureBacktestBuilder";
import { StructureCases, StructureReportView } from "./StructureResearchReport";
import { StructureEvidence } from "./StructureEvidence";
import { progressPercent, type StructureRun } from "./structureResearchModel";
import { researchDate } from "./researchV2Model";
import "./researchV2.css";

const fromUrl = () => new URLSearchParams(location.search).get("structure_run") ?? "";
export function StructureResearchWorkspace({ session }: { session: Session }) {
  const [runId, setRunId] = useState(fromUrl), [page, setPage] = useState(0), [refresh, setRefresh] = useState(0);
  const [builder, setBuilder] = useState(false), [busy, setBusy] = useState(false), [notice, setNotice] = useState("");
  const [tab, setTab] = useState<"report" | "cases" | "events">("report");
  const list = useResearchResource<{ items: StructureRun[]; total: number }>(`/api/v2/research/backtests?engine_mode=STRUCTURE&limit=10&offset=${page * 10}`, refresh);
  const run = useResearchResource<StructureRun>(runId ? `/api/v2/research/backtests/${encodeURIComponent(runId)}` : null, refresh);
  const catalog = useResearchResource<{ defaults: Spec; structure_catalog: StructureCatalogItem[]; structure_engine_version: string }>("/api/v2/research/strategies");
  const running = ["QUEUED", "RUNNING"].includes(run.data?.state ?? "") || list.data?.items.some(item => ["QUEUED", "RUNNING"].includes(item.state));
  useEffect(() => { if (!runId && list.data?.items[0]) setRunId(list.data.items[0].id); }, [list.data, runId]);
  useEffect(() => { if (list.data && page > 0 && page * 10 >= list.data.total) setPage(Math.max(0, Math.ceil(list.data.total / 10) - 1)); }, [list.data, page]);
  useEffect(() => { const handler = () => { setRunId(fromUrl()); setTab("report"); }; window.addEventListener("popstate", handler); return () => window.removeEventListener("popstate", handler); }, []);
  useEffect(() => { if (runId) { const url = new URL(location.href); url.searchParams.set("structure_run", runId); history.replaceState({}, "", url); } }, [runId]);
  useEffect(() => { if (!running) return; const timer = window.setInterval(() => { if (!document.hidden) setRefresh(n => n + 1); }, 4000); return () => window.clearInterval(timer); }, [running]);
  function download() {
    if (!run.data?.report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(run.data, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `structure-research-${run.data.id}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div className="structure-workspace"><div className="structure-toolbar"><span className="research-context-note">实时识别同源 · 默认一年缓存 · 后台分片回放</span><div className="button-row"><a href="/trading/shadow">通知影子账本 →</a><button disabled={list.loading || run.loading} onClick={() => setRefresh(n => n + 1)}>刷新进度</button><button className="primary" disabled={session.user.role === "VIEWER"} onClick={() => setBuilder(true)}>＋ 同源形态回测</button></div></div>
    {notice && <div className="research-feedback" role="status">{notice}<button onClick={() => setNotice("")}>关闭</button></div>}
    <section className="panel research-report"><header><h2>同源研究记录</h2><span className="simulation-badge">与旧指标报告分开保存</span></header><PageState loading={list.loading} error={list.error} onRetry={list.retry} />
      {list.data && <>{list.data.items.length ? <div className="structure-run-select"><label>选择回测报告<select value={list.data.items.some(item => item.id === runId) ? runId : ""} onChange={event => { setRunId(event.target.value); setTab("report"); }}><option value="" disabled>选择当前页报告</option>{list.data.items.map(item => <option key={item.id} value={item.id}>{item.symbol} · {item.timeframe} · {stateLabel[item.state] ?? item.state} · {researchDate(item.created_at)}</option>)}</select></label></div> : <p className="research-context-note">暂无同源回测。提交后会保存进度、全部候选及逐笔结果；旧指标报告仍在其他标签页。</p>}<Pagination label="同源研究" page={page} pages={Math.max(1, Math.ceil(list.data.total / 10))} total={list.data.total} busy={list.loading} onChange={setPage} /></>}
    </section>
    <PageState loading={run.loading && !run.data} error={run.error} onRetry={run.retry} />
    {run.data && <><div className="research-result-heading"><div><h2>{run.data.symbol} · {run.data.timeframe}</h2><p>{run.data.report ? `${researchDate(run.data.report.range.from)} — ${researchDate(run.data.report.range.to)} · ${run.data.report.bar_count.toLocaleString()} 根` : `提交于 ${researchDate(run.data.created_at)} · 最多 ${run.data.bar_limit.toLocaleString()} 根`}</p><small>识别模型 {run.data.report?.detector_version ?? "后台正在确认"} · {run.data.report?.engine_version ?? catalog.data?.structure_engine_version ?? "同源研究"}</small></div>{run.data.report && <button onClick={download}>下载验证报告</button>}</div>
      {["QUEUED", "RUNNING"].includes(run.data.state) && <StructureProgress run={run.data} />}
      {run.data.state === "FAILED" && <section className="panel research-warning" role="alert"><h3>本次研究未完成</h3><p>{run.data.error ?? "后台计算失败，已保存的证据仍保留。"}</p><button disabled={session.user.role === "VIEWER"} onClick={() => setBuilder(true)}>新建研究</button></section>}
      {run.data.spec.engine_mode !== "STRUCTURE" ? <p className="research-warning">这是旧指标报告，请从「指标结果概览」打开；不会使用新引擎改写旧结果。</p> : <><nav className="research-tabs" aria-label="同源研究结果"><button className={tab === "report" ? "active" : ""} aria-pressed={tab === "report"} onClick={() => setTab("report")}>验证报告</button><button className={tab === "cases" ? "active" : ""} aria-pressed={tab === "cases"} onClick={() => setTab("cases")}>候选与逐笔计划</button><button className={tab === "events" ? "active" : ""} aria-pressed={tab === "events"} onClick={() => setTab("events")}>原始识别事件</button></nav>
        {tab === "events" ? <StructureEvidence key={runId} runId={runId} refresh={refresh} names={Object.fromEntries((catalog.data?.structure_catalog ?? []).map(item => [item.id, item.name]))} /> : tab === "cases" ? <StructureCases key={runId} runId={runId} refresh={refresh} names={Object.fromEntries((catalog.data?.structure_catalog ?? []).map(item => [item.id, item.name]))} /> : run.data.report?.structure_research ? <StructureReportView key={runId} report={run.data.report} /> : run.data.state === "SUCCEEDED" ? <PageState error="报告尚未就绪，请重新读取。" onRetry={run.retry} /> : <p className="research-context-note">完成后展示 TP / SL 净收益、环境分层、滚动验证和成本压力测试；处理中可先查看已保存候选。</p>}
      </>}
    </>}
    {builder && <Dialog title="新建同源形态回测" busy={busy} onClose={() => setBuilder(false)}><PageState loading={catalog.loading} error={catalog.error} onRetry={catalog.retry} />{catalog.data?.structure_catalog?.length ? <StructureBacktestBuilder defaults={catalog.data.defaults} catalog={catalog.data.structure_catalog} onBusy={setBusy} onCreated={created => { setRunId(created.id); setPage(0); setTab("report"); setBuilder(false); setRefresh(n => n + 1); setNotice("同源回测已提交，后台会持续处理并保存进度。"); }} /> : !catalog.loading && !catalog.error && <PageState error="同源形态目录尚未就绪。" onRetry={catalog.retry} />}</Dialog>}
  </div>;
}

export function StructureProgress({ run }: { run: StructureRun }) {
  const percent = progressPercent(run.progress);
  return <section className="panel research-report" role="status"><header><h3>{stateLabel[run.state] ?? run.state}</h3><span>{percent === null ? "正在准备数据快照" : `${percent.toFixed(1)}%`}</span></header>{percent !== null && <progress className="structure-progress" aria-label="同源回测处理进度" max={100} value={percent} />}<p className="research-context-note">已处理 {run.progress?.processed_bars?.toLocaleString() ?? "0"} / {run.progress?.total_bars?.toLocaleString() ?? "待确定"} 根 · 截至 {researchDate(run.progress?.through_time)}。可以离开页面，后台继续计算。</p>{run.error && <p className="research-context-note">{run.error}</p>}</section>;
}
