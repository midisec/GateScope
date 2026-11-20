import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, jsonBody } from "./api";
import { InstrumentPicker, type InstrumentChoice } from "./InstrumentPicker";
import type { McpClient } from "./types";
import { Dialog, PageState, PagedList, useAction, useConfirm } from "./ui";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";
import { coinglassBundleStatus, coinglassMetricLabels, coinglassMetricReadiness, coinglassRefreshState, coinglassRetrySeconds,
  coinglassSeriesUsable, coinglassStatus, type CoinglassBundle, type CoinglassMetricId } from "./coinglassModel";
import { CoinglassCacheStatus } from "./CoinglassContext";
import { useCoinglassClock } from "./useCoinglassClock";
import { patternTime } from "./patternModel";
import "./mcpMarketPreview.css";
import "./oauthConsent.css";

export type McpMarketSnapshot = {
  instrument: { id: string; symbol: string; market: string }; timeframe: string; as_of: string;
  candles: unknown[]; latest_closed: { close: string; close_time: string } | null;
  realtime: { status: string; used_for_indicators: false };
  indicators: { indicator_id: string; ready: boolean; parameters: Record<string, number>; values: Record<string, number | null>; bars_evaluated: number }[];
  quality: { stale: boolean; missing_bars: number; contiguous_indicator_bars: number };
  quality_flags: string[];
  coinglass: Partial<CoinglassBundle> & { status: string };
};

export function mcpSnapshotArguments(instrumentId: string, timeframe: string, barLimit: number) {
  return { name: "market_analysis_snapshot", arguments: { instrument_id: instrumentId, timeframe, bar_limit: barLimit } };
}
type McpClientResponse = Omit<McpClient, "tokens"> & { tokens?: McpClient["tokens"] };
export function normalizeMcpClient(client: McpClientResponse): McpClient {
  return { ...client, tokens: client.tokens ?? [] };
}
export const MCP_CALL_EXAMPLE = { name: "market_analysis_snapshot", arguments: { symbol: "BTC_USDT", market: "futures:usdt", timeframe: "15m", bar_limit: 500 } };
const displayValue = (value: number | string | null | undefined) => value == null ? "—" : typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 6 }) : value;

export function McpOAuthGuide({ notify }: { notify: (value: string) => void }) {
  const endpoint = typeof window !== "undefined" ? `${window.location.origin}/mcp` : "/mcp";
  return <section className="panel mcp-oauth-guide" aria-label="ChatGPT OAuth 连接方式"><header><h2>连接 ChatGPT 网页</h2><span className="status">OAuth · 按需授权</span></header><ol><li>在 ChatGPT 添加 MCP 连接，填入下方服务器地址。</li><li>认证方式选择 OAuth；客户端 ID 和密钥留空，使用动态注册。</li><li>跳转到 GateScope 后，登录、完成验证器验证并选择权限。自选与回测需勾选「本人私有研究数据」；模拟交易需另行勾选「本人模拟账户」和「允许模拟交易」。</li></ol><div className="mcp-oauth-address"><code>{endpoint}</code><button type="button" onClick={async () => { try { await navigator.clipboard.writeText(endpoint); notify("MCP 地址已复制"); } catch { notify("无法访问剪贴板，请手动复制 MCP 地址"); } }}>复制 MCP 地址</button></div><p>默认仅分析行情。模拟权限使用同一个 PAPER 账户，开平仓记录在交易中心可见；不开放实盘、测试网、密钥或通知配置。</p><p>旧连接不会自动增加权限。没有新权限选项时，请在 ChatGPT 刷新工具、重新授权；旧客户端仍未请求该权限时，需重新添加连接。</p><p>OAuth 连接会出现在下方客户端列表，可随时吊销。自建脚本或支持 Bearer Token 的客户端，仍可使用下面的手动令牌。</p></section>;
}

export function McpMarketPreview({ notify }: { notify: (value: string) => void }) {
  const [instrument, setInstrument] = useState<InstrumentChoice>();
  const [timeframe, setTimeframe] = useState("15m"), [barLimit, setBarLimit] = useState(500);
  const [snapshot, setSnapshot] = useState<McpMarketSnapshot>(), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => { pending.current?.abort(); }, []);
  function invalidate() { pending.current?.abort(); pending.current = null; setSnapshot(undefined); setLoading(false); setError(""); }
  async function read(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!instrument || loading) return;
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    setSnapshot(undefined); setLoading(true); setError("");
    const params = new URLSearchParams({ instrument_id: instrument.id, timeframe, limit: String(barLimit) });
    try {
      const result = await api<McpMarketSnapshot>(`/api/v2/mcp/market-preview?${params}`, { signal: controller.signal });
      if (!controller.signal.aborted && pending.current === controller) setSnapshot(result);
    } catch (cause) { if (!controller.signal.aborted && pending.current === controller) setError(cause instanceof Error ? cause.message : "读取失败，请重试"); }
    finally { if (pending.current === controller) { pending.current = null; setLoading(false); } }
  }
  async function copy(value: unknown, message: string) {
    try { await navigator.clipboard.writeText(JSON.stringify(value, null, 2)); notify(message); }
    catch { notify("无法访问剪贴板，请展开 JSON 后手动复制"); }
  }
  const parameters = instrument ? mcpSnapshotArguments(instrument.id, timeframe, barLimit) : undefined;
  return <section className="panel mcp-market-preview" aria-label="MCP 行情数据预览">
    <header><div><h2>单次调用预览</h2><small>仅调试这次请求，不保存币种或周期，不限制其他 MCP 调用。</small></div><span className="status">只读</span></header>
    <form className="mcp-preview-form" onSubmit={event => { void read(event); }}>
      <InstrumentPicker value={instrument?.id ?? ""} selectedLabel={instrument?.symbol} onChange={choice => { invalidate(); setInstrument(choice); }} />
      <label>周期<select aria-label="MCP 输出周期" value={timeframe} onChange={event => { invalidate(); setTimeframe(event.target.value); }}>{["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>K 线条数<select aria-label="MCP 输出条数" value={barLimit} onChange={event => { invalidate(); setBarLimit(Number(event.target.value)); }}>{[50, 100, 500, 1000, 2000].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <button className="primary" disabled={!instrument || loading}>{loading ? "读取中…" : "读取最新缓存"}</button>
      <button type="button" disabled={!parameters} onClick={() => { void copy(parameters, "MCP 调用参数已复制"); }}>复制调用参数</button>
    </form>
    {loading && <p role="status">正在读取所选币种的行情、指标和 Coinglass 缓存…</p>}
    {error && <p role="alert">{error} · 可点击「读取最新缓存」重试。</p>}
    {!snapshot && !loading && !error && <p className="mcp-preview-empty">先选择币种。预览不签发令牌，也不会向 OpenAI 发送数据。</p>}
    {snapshot && <McpSnapshotView snapshot={snapshot} onCopy={() => { void copy(snapshot, "分析数据已复制"); }} />}
    {parameters && <details className="mcp-preview-json"><summary>调用参数 JSON</summary><pre>{JSON.stringify(parameters, null, 2)}</pre></details>}
  </section>;
}

export function McpSnapshotView({ snapshot, onCopy }: { snapshot: McpMarketSnapshot; onCopy: () => void }) {
  const context = snapshot.coinglass, now = useCoinglassClock(context.retry_at, ["QUEUED", "COLLECTING"].includes(coinglassRefreshState(context)));
  const exact = context.match?.status === "EXACT" && context.instrument_id === snapshot.instrument.id && context.timeframe === snapshot.timeframe;
  const status = !exact && ["READY", "PARTIAL", "STALE"].includes(context.status) ? "MISMATCH" : coinglassBundleStatus(context, now);
  return <div className="mcp-preview-result">
    <header><strong>{snapshot.instrument.symbol} · {snapshot.timeframe}</strong><button type="button" onClick={onCopy}>复制分析数据</button></header>
    <dl className="mcp-preview-metrics"><div><dt>最近闭合价</dt><dd>{snapshot.latest_closed?.close ?? "暂无闭合 K 线"}</dd></div><div><dt>闭合样本</dt><dd>{snapshot.candles.length} 根</dd></div><div><dt>连续指标样本</dt><dd>{snapshot.quality.contiguous_indicator_bars} 根</dd></div><div><dt>Coinglass</dt><dd>{coinglassStatus(status)}</dd></div></dl>
    <p className="mcp-preview-time">{snapshot.latest_closed ? `闭合至 ${new Date(snapshot.latest_closed.close_time).toLocaleString()}` : "没有可计算的闭合数据"} · {snapshot.quality.stale ? "行情缓存已过期" : "行情缓存有效"}{snapshot.quality.missing_bars > 0 ? ` · 缺失 ${snapshot.quality.missing_bars} 根` : ""} · 未闭合观察：{snapshot.realtime.status === "CURRENT" ? "实时，仅作观察" : snapshot.realtime.status === "STALE" ? "已过期" : "暂无"}</p>
    <div className="mcp-preview-indicators">{snapshot.indicators.map(item => <article key={item.indicator_id}><header><strong>{item.indicator_id.toUpperCase()}</strong><small>{item.ready ? "就绪" : "样本不足"}</small></header><dl>{Object.entries(item.values).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{displayValue(value)}</dd></div>)}</dl><small>{Object.entries(item.parameters).map(([key, value]) => `${key}=${value}`).join(" · ") || "无可选参数"}</small></article>)}</div>
    <details><summary>Coinglass 数据状态</summary><CoinglassCacheStatus bundle={{ ...context, status, match: exact ? context.match : { status: "MISMATCH" } }} now={now} />
      <div className="mcp-preview-context">{(Object.keys(coinglassMetricLabels) as CoinglassMetricId[]).map(id => {
        const series = context.series?.find(item => item.id === id);
        const usable = exact && !!series && coinglassSeriesUsable(series, snapshot.timeframe), latest = usable ? series.points.at(-1) : undefined;
        const cached = coinglassMetricReadiness(context, id, now) === "HISTORICAL";
        const metricStatus = series?.status === "RATE_LIMITED" && !coinglassRetrySeconds(context, now) ? "等待重试" : coinglassStatus(series?.status ?? "EMPTY");
        return <div key={id}><strong>{coinglassMetricLabels[id]}</strong><span>{latest ? `${displayValue(latest.value)} ${series!.unit}${cached ? " · 缓存参考" : ""}` : "暂无已核实数据"}</span>
          <small>{metricStatus}{series?.unit_status === "UNKNOWN" ? " · 单位待核实" : ""}</small>
          {latest && <small>数据截至 {patternTime(latest.close_time)} · 获取于 {patternTime(series!.fetched_at ?? latest.available_at)}</small>}</div>;
      })}</div>
      <small>读取缓存并请求后台更新；冷却期间无需连续调用，历史缓存不能当作最新交易证据。</small></details>
    <details className="mcp-preview-json"><summary>完整分析数据 JSON</summary><pre>{JSON.stringify(snapshot, null, 2)}</pre></details>
    <small>Web 会话预览；外部调用仍需 mcp:connect、market:read、analysis:read 令牌。指标和涨跌不等于策略胜率。</small>
  </div>;
}

export function McpWorkspace({ userId, notify }: { userId: string; notify: (value: string) => void }) {
  const [clients, setClients] = useState<McpClient[]>([]), [scopes, setScopes] = useState<string[]>([]);
  const [selected, setSelected] = useState(""), [mfa, setMfa] = useState("");
  const [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [issued, setIssued] = useState<{ token: string; expires_at: string }>();
  const [saved, setSaved] = useState(false);
  const [reauthenticate, setReauthenticate] = useState(false);
  const [tokenScopes, setTokenScopes] = useState(["mcp:connect", "market:read", "analysis:read"]);
  const action = useAction(text => { setError(text); notify(text); }), confirm = useConfirm();
  const load = useCallback(async () => {
    try {
      const result = await api<{ items: McpClientResponse[]; supported_scopes: string[] }>("/api/v2/mcp/clients");
      setClients(result.items.map(normalizeMcpClient)); setScopes(result.supported_scopes);
      setSelected(current => result.items.some(c => c.id === current && c.status === "ACTIVE") ? current : result.items.find(c => c.status === "ACTIVE")?.id ?? "");
      setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "加载失败"); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function sensitiveRequest<T>(path: string, init: RequestInit): Promise<T> {
    try { return await api<T>(path, init); }
    catch (cause) {
      if (cause instanceof ApiError && cause.code === "RECENT_AUTH_REQUIRED") setReauthenticate(true);
      throw cause;
    }
  }
  function confirmIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget, password = String(new FormData(form).get("password") ?? "");
    if (!password) return;
    form.reset();
    void action.run("验证中…", async () => {
      const envelope = await encryptSubmission(`user:${userId}:reauthenticate`, { password });
      await api("/auth/reauthenticate", { method: "POST", body: jsonBody(wrapEncryptedSubmission(envelope)) });
      setReauthenticate(false); setError(""); setMfa(""); notify("身份已确认，请重新提交刚才的操作");
    });
  }
  function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = e.currentTarget, data = new FormData(form);
    void action.run("创建中…", async () => {
      const client = normalizeMcpClient(await sensitiveRequest<McpClientResponse>("/api/v2/mcp/clients", { method: "POST", body: jsonBody({ name: data.get("name") }) }));
      setClients(old => [client, ...old]); setSelected(client.id); form.reset(); setError(""); notify("MCP 客户端已创建，可继续签发令牌");
    });
  }
  function issue(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const data = new FormData(e.currentTarget);
    if (issued) return;
    void action.run("签发中…", async () => {
      const result = await sensitiveRequest<{ token: string; expires_at: string }>(`/api/v2/mcp/clients/${selected}/tokens`, { method: "POST", body: jsonBody({ label: data.get("label"), scopes: scopes.filter(s => s === "mcp:connect" || data.get(s) === "on"), ttl_hours: Number(data.get("ttl")), mfa_code: mfa }) });
      setIssued(result); setSaved(false); setMfa(""); await load();
    });
  }
  function revoke(client: McpClient, tokenId?: string) {
    if (mfa.length !== 6) { setError("请先输入新的 6 位 MFA 验证码"); return; }
    void action.run("吊销中…", async () => {
      if (!await confirm.ask(tokenId ? "吊销令牌" : "吊销客户端", tokenId ? "该令牌将立即失效，已接入的应用需要更换令牌。" : `将吊销 ${client.name} 及其全部令牌，已接入的应用会断开。`)) return;
      await sensitiveRequest(`/api/v2/mcp/clients/${client.id}${tokenId ? `/tokens/${tokenId}` : ""}:revoke`, { method: "POST", body: jsonBody({ code: mfa }) });
      setMfa(""); await load(); notify("已吊销");
    });
  }
  return <main className="settings-page"><section className="page-title"><h1>MCP 接入管理</h1><button disabled={!!action.busy || loading} onClick={() => void load()}>刷新</button></section><p className="mcp-preview-time">客户端管理访问权限；币种和周期由每次 MCP 调用参数指定，无需在此绑定。</p><McpOAuthGuide notify={notify} /><PageState loading={loading} error={error} onRetry={() => void load()} /><div className="mcp-identity"><label className="compact-mfa">敏感操作 MFA<input value={mfa} onChange={e => setMfa(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" maxLength={6} placeholder="新的 6 位验证码" /></label><button disabled={!!action.busy} onClick={() => { setError(""); setReauthenticate(true); }}>确认登录身份</button></div>
    <div className="settings-grid mcp-grid"><section className="panel"><h2>创建客户端</h2><form className="stack-form" onSubmit={create}><label>客户端名称<input name="name" required maxLength={80} placeholder="我的分析客户端" /></label><button className="primary" disabled={!!action.busy || loading}>{action.busy || "创建隔离客户端"}</button></form></section>
    <section className="panel"><h2>签发访问令牌</h2><form className="stack-form" onSubmit={issue}>
      <label>客户端<select aria-label="客户端" value={selected} onChange={e => setSelected(e.target.value)} required><option value="">请选择</option>{clients.filter(c => c.status === "ACTIVE").map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
      <label>令牌标签<input name="label" defaultValue="我的分析应用" required maxLength={80} /></label>
      <label>有效时长<select aria-label="有效时长" name="ttl" defaultValue="720"><option value="1">1 小时</option><option value="24">24 小时</option><option value="168">7 天</option><option value="720">30 天</option><option value="2160">90 天</option></select></label>
      <fieldset><legend>访问权限</legend>{scopes.map(s => <label key={s} className="checkline"><input name={s} type="checkbox" checked={tokenScopes.includes(s)} disabled={s === "mcp:connect"} onChange={e => setTokenScopes(current => e.target.checked ? [...new Set([...current, s, ...(s === "paper:trade" ? ["paper:read"] : [])])] : current.filter(item => item !== s && !(s === "paper:read" && item === "paper:trade")))} />{{ "research:read": "research:read · 本人自选、策略参数、回测与历史证据（只读）", "paper:read": "paper:read · 本人模拟余额、委托、持仓及成交（默认关闭）", "paper:trade": "paper:trade · 允许模拟开仓、平仓及撤单（非实盘，默认关闭）" }[s] ?? s}</label>)}</fieldset>
      {tokenScopes.includes("paper:trade") && <p className="oauth-boundary">此令牌允许操作本人模拟资金；下单不等于成交。不会授予实盘或测试网权限。</p>}
      <button className="primary" disabled={!!action.busy || loading || !selected || mfa.length !== 6 || !scopes.length}>{action.busy || "用 MFA 签发"}</button>{!selected && <small>请先创建有效客户端。</small>}
    </form></section>
    <section className="panel mcp-clients"><h2>客户端与令牌</h2><PagedList label="MCP 客户端" items={clients} size={5} render={client => <article key={client.id} className="mcp-client"><header><div><strong>{client.name}</strong><span className={`status ${client.status.toLowerCase()}`}>{client.status === "ACTIVE" ? "有效" : "已吊销"}</span></div>{client.status === "ACTIVE" && <button disabled={!!action.busy || mfa.length !== 6} onClick={() => revoke(client)}>吊销客户端</button>}</header><PagedList label={`${client.name}令牌`} items={client.tokens} size={5} empty="尚未签发令牌" render={token => <div className="token-row" key={token.id}><div><code>{token.token_hint}</code><small>{token.label} · {token.scopes.join(" · ")}<br />到期 {new Date(token.expires_at).toLocaleString()}</small></div><span className={`status ${token.status.toLowerCase()}`}>{token.status}</span>{token.status === "ACTIVE" && <button disabled={!!action.busy || mfa.length !== 6} onClick={() => revoke(client, token.id)}>吊销</button>}</div>} /></article>} /></section></div>
    {issued && <Dialog title="保存新令牌" busy={!saved} onClose={() => setIssued(undefined)}><p>令牌只显示这一次，请保存到密码管理器。</p><code className="one-time-secret">{issued.token}</code><p>到期 {new Date(issued.expires_at).toLocaleString()}</p><button onClick={async () => { try { await navigator.clipboard.writeText(issued.token); notify("令牌已复制"); } catch { notify("无法访问剪贴板，请手动复制"); } }}>复制令牌</button><label className="checkline"><input type="checkbox" checked={saved} onChange={e => setSaved(e.target.checked)} />我已安全保存</label><button disabled={!saved} onClick={() => setIssued(undefined)}>清除显示并关闭</button></Dialog>}{confirm.dialog}
    <section className="panel mcp-call-guide" aria-label="MCP 调用方式"><h2>按调用指定币种与周期</h2><p>同一客户端和令牌可先查 BTC 15m，再查 ETH 1h；不需要修改管理页。</p><details className="mcp-preview-json"><summary>market_analysis_snapshot 参数示例</summary><pre>{JSON.stringify(MCP_CALL_EXAMPLE, null, 2)}</pre></details></section>
    <details className="mcp-preview-debug"><summary>调试单次调用（可选）</summary><McpMarketPreview notify={notify} /></details>
    {reauthenticate && <Dialog title="确认登录身份" busy={!!action.busy} onClose={() => { setReauthenticate(false); setError(""); }}><p>输入当前密码后继续操作，不会自动创建客户端或签发令牌。</p><form className="stack-form" onSubmit={confirmIdentity}><label>当前密码<input name="password" type="password" required autoComplete="current-password" autoFocus /></label>{error && <p role="alert">{error}</p>}<button className="primary" disabled={!!action.busy}>{action.busy || "验证密码"}</button></form></Dialog>}
  </main>;
}
