import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type ChartMenuAction = { label: string; run: () => void; disabled?: boolean };
export function ChartContextMenu({ x, y, actions, onClose }: { x: number; y: number; actions: ChartMenuAction[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null), [position, setPosition] = useState({ x, y });
  useLayoutEffect(() => { const box = ref.current!.getBoundingClientRect(); setPosition({ x: Math.max(8, Math.min(x, innerWidth - box.width - 8)), y: Math.max(8, Math.min(y, innerHeight - box.height - 8)) }); }, [x, y]);
  useEffect(() => {
    const focused = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const outside = (e: Event) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    document.addEventListener("pointerdown", outside); window.addEventListener("resize", onClose);
    return () => { document.removeEventListener("pointerdown", outside); window.removeEventListener("resize", onClose); focused?.focus(); };
  }, [onClose]);
  return createPortal(<div ref={ref} className="chart-context-menu" role="menu" aria-label="行情图表菜单" style={{ left: position.x, top: position.y }} onKeyDown={e => {
    const buttons = [...ref.current!.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")], index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) { e.preventDefault(); buttons[e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : (index + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus(); }
    if (e.key === "Escape" || e.key === "Tab") { if (e.key === "Escape") e.preventDefault(); onClose(); }
  }}>{actions.map(action => <button type="button" role="menuitem" disabled={action.disabled} key={action.label} onClick={() => { onClose(); action.run(); }}>{action.label}</button>)}</div>, document.body);
}
