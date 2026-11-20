import { Component, useEffect, useId, useRef, useState, type ReactNode } from "react";
import "./ui.css";

export function Dialog({ title, children, onClose, busy = false }: { title: string; children: ReactNode; onClose: () => void; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null), heading = useId();
  useEffect(() => {
    const dialog = ref.current!, previous = document.body.style.overflow, focused = document.activeElement as HTMLElement | null;
    dialog.showModal(); document.body.style.overflow = "hidden";
    return () => { dialog.close(); document.body.style.overflow = previous; focused?.focus(); };
  }, []);
  return <dialog ref={ref} className="app-dialog" aria-labelledby={heading} onCancel={e => { e.preventDefault(); if (!busy) onClose(); }} onClick={e => { if (!busy && e.target === ref.current) onClose(); }}><div className="app-dialog-body"><header><h2 id={heading}>{title}</h2><button type="button" disabled={busy} aria-label="关闭弹窗" onClick={onClose}>✕</button></header>{children}</div></dialog>;
}

export function useConfirm() {
  const [question, setQuestion] = useState<{ title: string; text: string }>();
  const resolver = useRef<((answer: boolean) => void) | undefined>(undefined);
  useEffect(() => () => { resolver.current?.(false); }, []);
  const answer = (value: boolean) => { resolver.current?.(value); resolver.current = undefined; setQuestion(undefined); };
  return {
    ask: (title: string, text: string) => new Promise<boolean>(resolve => { resolver.current?.(false); resolver.current = resolve; setQuestion({ title, text }); }),
    dialog: question ? <Dialog title={question.title} onClose={() => answer(false)}><p>{question.text}</p><div className="dialog-actions"><button autoFocus onClick={() => answer(false)}>取消</button><button className="danger" onClick={() => answer(true)}>确认操作</button></div></Dialog> : null,
  };
}

export function useAction(notify: (message: string) => void) {
  const lock = useRef(false), [busy, setBusy] = useState("");
  return { busy, run: async (label: string, job: () => Promise<unknown>) => {
    if (lock.current) return;
    lock.current = true; setBusy(label);
    try { return await job(); } catch (error) { notify(error instanceof Error ? error.message : "操作失败，请重试"); }
    finally { lock.current = false; setBusy(""); }
  } };
}

export function PageState({ loading, error, onRetry }: { loading?: boolean; error?: string; onRetry?: () => void }) {
  if (error) return <div className="page-error" role="alert"><span>{error}</span>{onRetry && <button onClick={onRetry}>重新加载</button>}</div>;
  return loading ? <div className="page-loading" role="status"><span className="ui-spinner" />正在加载…</div> : null;
}

export function pageSlice<T>(items: T[], requested: number, size: number) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const page = Math.max(0, Math.min(pages - 1, requested));
  return { page, pages, items: items.slice(page * size, (page + 1) * size) };
}

export function PagedList<T>({ items, render, label, size = 8, empty = "暂无记录" }: { items: T[]; render: (item: T, index: number) => ReactNode; label: string; size?: number; empty?: string }) {
  const [requested, setPage] = useState(0);
  const result = pageSlice(items, requested, size);
  useEffect(() => { if (result.page !== requested) setPage(result.page); }, [result.page, requested]);
  return <>{result.items.length ? result.items.map(render) : <div className="empty">{empty}</div>}{items.length > size && <Pagination label={label} page={result.page} pages={result.pages} onChange={setPage} total={items.length} />}</>;
}

export function Pagination({ label, page, pages, onChange, total, hasNext, busy }: { label: string; page: number; pages?: number; onChange: (page: number) => void; total?: number; hasNext?: boolean; busy?: boolean }) {
  return <nav className="pagination" aria-label={`${label}分页`}><span>{total === undefined ? "" : `共 ${total} 条 · `}第 {page + 1} {pages === undefined ? "页" : `/ ${pages} 页`}</span><div><button disabled={busy || page <= 0} onClick={() => onChange(0)} aria-label={`${label}首页`}>首页</button><button disabled={busy || page <= 0} onClick={() => onChange(page - 1)} aria-label={`${label}上一页`}>上一页</button><button disabled={busy || (pages === undefined ? !hasNext : page + 1 >= pages)} onClick={() => onChange(page + 1)} aria-label={`${label}下一页`}>下一页</button></div></nav>;
}

export class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <main className="settings-page"><h1>页面暂时无法显示</h1><p>已保存的数据不会因此丢失。</p><button onClick={() => location.reload()}>重新加载页面</button><a href="/markets/chart">返回行情首页</a></main> : this.props.children; }
}
