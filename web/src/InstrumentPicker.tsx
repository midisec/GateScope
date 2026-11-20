import { useEffect, useId, useRef, useState } from "react";
import { api } from "./api";
import type { Instrument, Watchlist } from "./types";
import "./instrumentPicker.css";

export type InstrumentChoice = { id: string; symbol: string; market?: string };
const marketLabel = (market?: string) => market?.toLowerCase() === "spot" ? "现货" : market?.toLowerCase() === "futures_btc" ? "BTC 合约" : market ? "USDT 合约" : "自选";
export function InstrumentPicker({ value, selectedLabel, onChange }: { value: string; selectedLabel?: string; onChange: (item: InstrumentChoice) => void }) {
  const id = useId(), root = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false), [query, setQuery] = useState("");
  const [watched, setWatched] = useState<InstrumentChoice[]>([]), [results, setResults] = useState<InstrumentChoice[]>([]);
  const [picked, setPicked] = useState<InstrumentChoice>(), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [cursor, setCursor] = useState(0), [attempt, setAttempt] = useState(0);
  useEffect(() => { void api<{ items: Watchlist[] }>("/api/v2/watchlists").then(r => {
    const unique = new Map(r.items.flatMap(w => w.items).map(i => [i.instrument_id, { id: i.instrument_id, symbol: i.symbol }]));
    setWatched([...unique.values()]);
  }).catch(() => undefined); }, []);
  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const outside = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside); return () => document.removeEventListener("mousedown", outside);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); setCursor(0); setError("");
    if (!query.trim() && watched.length) { setResults(watched.slice(0, 12)); setLoading(false); return () => controller.abort(); }
    setLoading(true); setResults([]);
    const timer = window.setTimeout(() => {
      void api<{ items: Instrument[] }>(`/api/v2/instruments?q=${encodeURIComponent(query.trim() || "BTC")}&limit=12`, { signal: controller.signal }).then(r => {
        if (!controller.signal.aborted) { setResults(r.items.filter(i => !i.status || i.status.toUpperCase() === "ACTIVE")); setLoading(false); }
      }).catch(e => { if (!controller.signal.aborted) { setError(e.message); setLoading(false); } });
    }, 200);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [open, query, watched, attempt]);
  function choose(item: InstrumentChoice) { setPicked(item); onChange(item); setOpen(false); setQuery(""); root.current?.querySelector<HTMLButtonElement>(".instrument-trigger")?.focus(); }
  const selected = picked?.id === value ? picked : watched.find(w => w.id === value);
  const label = selected?.symbol ?? selectedLabel;
  return <div ref={root} className="instrument-picker"><span className="instrument-label">交易标的</span><button className="instrument-trigger" type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(v => !v)}><span>{value ? label ?? "已选择标的" : "搜索并选择币种"}{selected?.market && <small> · {marketLabel(selected.market)}</small>}</span><span aria-hidden="true">⌕</span></button>
    {open && <div className="instrument-popover"><input ref={input} role="combobox" aria-label="搜索币种" aria-controls={id} aria-expanded={open} aria-autocomplete="list" aria-activedescendant={results[cursor] ? `${id}-${cursor}` : undefined} placeholder="输入 BTC、ETH 或币种名称" value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setOpen(false); }
      if (e.key === "ArrowDown") { e.preventDefault(); setCursor(c => Math.min(results.length - 1, c + 1)); }
      if (e.key === "ArrowUp") { e.preventDefault(); setCursor(c => Math.max(0, c - 1)); }
      if (e.key === "Enter") { e.preventDefault(); if (results[cursor]) choose(results[cursor]); }
    }} /><small>{query.trim() ? "搜索结果 · 最多 12 个" : watched.length ? "我的自选 · 输入可搜索全部币种" : "常用 BTC 标的 · 输入可搜索其他币种"}</small>
      {loading && <p role="status">正在搜索…</p>}{error && <p role="alert">{error}<button type="button" onClick={() => setAttempt(v => v + 1)}>重试</button></p>}
      <div id={id} role="listbox" aria-label="币种搜索结果">{!loading && results.map((item, index) => <button type="button" role="option" id={`${id}-${index}`} aria-selected={value === item.id} className={index === cursor ? "highlight" : ""} key={item.id} onClick={() => choose(item)}><strong>{item.symbol}</strong><small>{marketLabel(item.market)}{value === item.id ? " ✓" : ""}</small></button>)}</div>
      {!loading && !error && !results.length && <p>没有找到币种，试试 BTC 或 ETH。</p>}
    </div>}
  </div>;
}
