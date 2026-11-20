import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, jsonBody } from "./api";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";
import type { Session } from "./types";
import { Dialog, PageState } from "./ui";
import "./oauthConsent.css";

export const OAUTH_CONSENT_PATH = "/integrations/mcp/authorize";

export type OAuthConsentDetails = {
  client_name: string;
  scopes: string[];
  resource: string;
  username?: string;
  expires_at: string;
};

const permissionLabels: Record<string, { title: string; description: string }> = {
  "mcp:connect": { title: "连接行情分析服务", description: "使用你的独立授权连接 MCP；访问能力由以下勾选项决定。" },
  "market:read": { title: "行情与 K 线", description: "按每次请求选择币种、市场和周期，读取缓存行情。" },
  "analysis:read": { title: "指标与 Coinglass 数据", description: "读取常用指标、形态、波浪与历史统计，以及已配置的数据源分析结果。" },
  "research:read": { title: "本人私有研究数据", description: "读取当前账号的自选币种、策略参数、回测结果和历史证据。可选，默认不授权。" },
  "paper:read": { title: "本人模拟账户", description: "读取本人模拟资金余额、委托、成交、持仓和开平仓记录。默认不授权。" },
  "paper:trade": { title: "允许模拟交易", description: "允许 ChatGPT 使用模拟资金开仓、平仓和撤单，同时需要读取模拟账户。不会连接实盘或测试网。默认不授权。" },
};

export function oauthConsentRequest(search: string): string | null {
  const values = new URLSearchParams(search).getAll("request");
  return values.length === 1 && /^[A-Za-z0-9_-]{32,256}$/.test(values[0]) ? values[0] : null;
}

// The server owns redirect validation. Keep a second, narrow browser boundary so
// a malformed response cannot navigate the user to an arbitrary destination.
export function chatGptOAuthRedirect(value: string): string {
  let url: URL;
  try { url = new URL(value); }
  catch { throw new Error("授权返回地址无效，请回到 ChatGPT 重新连接。"); }
  if (url.origin !== "https://chatgpt.com" || url.username || url.password || url.hash
    || !(url.pathname === "/connector_platform_oauth_redirect" || /^\/connector\/oauth\/[A-Za-z0-9_-]+$/.test(url.pathname))) {
    throw new Error("授权返回地址无效，请回到 ChatGPT 重新连接。");
  }
  return url.href;
}

function terminalError(error: unknown): boolean {
  return error instanceof ApiError && ([400, 404, 409, 410, 422].includes(error.status) || error.code === "ROLE_FORBIDDEN");
}

function consentError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "ROLE_FORBIDDEN") return "当前账号没有 MCP 授权权限，请使用有权限的账号重新连接。";
    if ([400, 404, 409, 410, 422].includes(error.status)) return "授权请求已失效或已处理，请回到 ChatGPT 重新发起连接。";
    if (error.status === 401) return "登录已过期，请重新登录后继续。";
    if (error.status === 429) return "操作过于频繁，请稍后重试。";
    if (error.code === "NETWORK_ERROR" || error.code === "REQUEST_TIMEOUT") return error.message;
  }
  return "暂时无法完成授权，请稍后重试。";
}

export function OAuthConsent({ session, requestId, onSessionExpired }: {
  session: Session;
  requestId: string | null;
  onSessionExpired: () => void;
}) {
  const [details, setDetails] = useState<OAuthConsentDetails>();
  const [selectedScopes, setSelectedScopes] = useState<string[]>([]);
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState("");
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false), [terminal, setTerminal] = useState(false);
  const [uncertain, setUncertain] = useState(false), [reauthenticate, setReauthenticate] = useState(false);
  const [passwordError, setPasswordError] = useState("");
  const lock = useRef(false);
  const pendingRead = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    if (!requestId) return;
    pendingRead.current?.abort();
    const controller = new AbortController(); pendingRead.current = controller;
    setLoading(true); setError(""); setDetails(undefined); setSelectedScopes([]); setExpired(false); setTerminal(false);
    try {
      const result = await api<OAuthConsentDetails>(`/api/v2/mcp/oauth/consent?${new URLSearchParams({ request: requestId })}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const expiresAt = Date.parse(result.expires_at);
      if (!Array.isArray(result.scopes) || !result.scopes.length || new Set(result.scopes).size !== result.scopes.length
        || result.scopes.some(scope => typeof scope !== "string" || !Object.prototype.hasOwnProperty.call(permissionLabels, scope))
        || !result.scopes.includes("mcp:connect") || !Number.isFinite(expiresAt)
        || (result.scopes.includes("paper:trade") && !result.scopes.includes("paper:read"))) {
        setTerminal(true); setError("授权请求包含不支持的权限或参数，请回到 ChatGPT 重新连接。"); return;
      }
      setDetails(result); setSelectedScopes(result.scopes.filter(scope => ["mcp:connect", "market:read", "analysis:read"].includes(scope))); setExpired(expiresAt <= Date.now());
    } catch (cause) {
      if (controller.signal.aborted) return;
      setError(consentError(cause)); setTerminal(terminalError(cause));
      if (cause instanceof ApiError && cause.status === 401) onSessionExpired();
    } finally {
      if (pendingRead.current === controller) { pendingRead.current = null; setLoading(false); }
    }
  }, [requestId, onSessionExpired]);
  useEffect(() => { void load(); return () => pendingRead.current?.abort(); }, [load]);
  useEffect(() => {
    if (!details) return;
    const timeout = window.setTimeout(() => setExpired(true), Math.max(0, Math.min(Date.parse(details.expires_at) - Date.now(), 2147483647)));
    return () => window.clearTimeout(timeout);
  }, [details]);

  async function decide(decision: "approve" | "deny") {
    if (lock.current || !requestId || !details || expired || terminal || uncertain) return;
    lock.current = true; setBusy(decision === "approve" ? "授权中…" : "返回中…"); setError(""); setNotice("");
    try {
      const result = await api<{ redirect_url: string }>("/api/v2/mcp/oauth/consent", {
        method: "POST", body: jsonBody({ request_id: requestId, decision,
          ...(decision === "approve" ? { approved_scopes: details.scopes.filter(scope => selectedScopes.includes(scope)) } : {}) }),
      });
      let destination: string;
      try { destination = chatGptOAuthRedirect(result.redirect_url); }
      catch { setTerminal(true); setError("授权返回地址无效，请回到 ChatGPT 重新连接。"); return; }
      setTerminal(true); setNotice("正在返回 ChatGPT…"); window.location.assign(destination);
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "RECENT_AUTH_REQUIRED" && decision === "approve") {
        setPasswordError(""); setReauthenticate(true);
      } else {
        setError(consentError(cause)); setTerminal(terminalError(cause));
        if (!(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500 || cause.code === "REQUEST_TIMEOUT" || cause.code === "INVALID_RESPONSE") setUncertain(true);
        if (cause instanceof ApiError && cause.status === 401) onSessionExpired();
      }
    } finally { lock.current = false; setBusy(""); }
  }

  async function confirmIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (lock.current) return;
    const form = event.currentTarget, password = String(new FormData(form).get("password") ?? "");
    if (!password) return;
    form.reset(); lock.current = true; setBusy("验证中…"); setPasswordError("");
    try {
      const envelope = await encryptSubmission(`user:${session.user.id}:reauthenticate`, { password });
      await api("/auth/reauthenticate", { method: "POST", body: jsonBody(wrapEncryptedSubmission(envelope)) });
      setReauthenticate(false); setError(""); setNotice(`身份已确认，请再次点击「${selectedScopes.includes("paper:trade") ? "允许模拟交易" : "允许只读访问"}」完成授权。`);
    } catch (cause) {
      setPasswordError(cause instanceof ApiError && cause.status === 401 ? "密码验证失败，或登录已过期。" : "无法验证当前密码，请检查后重试。");
      if (cause instanceof ApiError && cause.code === "AUTH_REQUIRED") onSessionExpired();
    } finally { lock.current = false; setBusy(""); }
  }

  const invalid = !requestId;
  const blocked = invalid || expired || terminal || uncertain;
  const trading = selectedScopes.includes("paper:trade");
  return <main className="auth-shell oauth-shell">
    <section className="auth-card oauth-card" aria-labelledby="oauth-consent-heading" aria-busy={loading || !!busy}>
      <header className="oauth-brand"><a href="/markets/chart" aria-label="GateScope 首页">GateScope</a><span className="status">{trading ? "模拟交易授权" : "只读授权"}</span></header>
      <h1 id="oauth-consent-heading">连接 ChatGPT</h1>
      <p className="oauth-account">当前账号 <strong>{session.user.username}</strong></p>
      {invalid ? <p role="alert" className="error-banner">授权链接无效，请回到 ChatGPT 重新发起连接。</p> : <PageState loading={loading} error={error} onRetry={!blocked && !busy ? () => { void load(); } : undefined} />}
      {details && <>
        <p className="oauth-client"><strong>{details.client_name}</strong> 请求以下权限：</p>
        <ul className="oauth-permissions">{details.scopes.map(scope => <li key={scope}><label>
          <input type="checkbox" name={scope} checked={selectedScopes.includes(scope)} disabled={scope === "mcp:connect" || !!busy || blocked}
            aria-label={permissionLabels[scope].title} aria-describedby={`oauth-permission-${scope.replace(":", "-")}`}
            onChange={event => { if (lock.current || scope === "mcp:connect" || blocked) return; const checked = event.target.checked;
              setSelectedScopes(current => checked ? [...new Set([...current, scope, ...(scope === "paper:trade" ? ["paper:read"] : [])])]
                : current.filter(item => item !== scope && !(scope === "paper:read" && item === "paper:trade"))); }} />
          <span><strong>{permissionLabels[scope].title}{scope === "mcp:connect" ? "（必需）" : "（可选）"}</strong><p id={`oauth-permission-${scope.replace(":", "-")}`}>{permissionLabels[scope].description}</p></span>
        </label></li>)}</ul>
        {!details.scopes.includes("research:read") && <p className="oauth-revoke">本次连接未请求个人研究数据。需要读取自选或回测时，请在 ChatGPT 刷新工具并重新授权；旧连接仍缺少该选项时，需要重新添加连接。</p>}
        <p className="oauth-boundary">{trading ? "允许使用本人模拟账户提交开仓、平仓及撤单。提交不等于成交，成交依据行情与可用深度判断；所有记录可在 PAPER 交易中心查询。不能进行实盘或测试网交易。" : "不能开单或修改设置。"} 不会获得密码、交易所密钥或 Coinglass API Key，也不能读取通知配置。</p>
        <p className="oauth-revoke">可随时在 <a href="/integrations/mcp" target="_blank" rel="noopener noreferrer">MCP 接入管理</a> 吊销此连接。</p>
        {expired && <p role="alert" className="error-banner">授权请求已过期，请回到 ChatGPT 重新发起连接。</p>}
        {uncertain && <p role="alert" className="error-banner">提交状态尚未确认。请返回 ChatGPT 重新连接，或在 MCP 接入管理查看授权；请勿重复提交。</p>}
        <div className="oauth-actions"><button type="button" disabled={!!busy || blocked} onClick={() => { void decide("deny"); }}>拒绝并返回</button><button type="button" className="primary" disabled={!!busy || blocked} onClick={() => { void decide("approve"); }}>{busy || (trading ? "允许模拟交易" : "允许只读访问")}</button></div>
      </>}
      {notice && <p role="status" className="oauth-notice">{notice}</p>}
      {blocked && <a className="oauth-home" href="/integrations/mcp">返回 MCP 接入管理</a>}
    </section>
    {reauthenticate && <Dialog title="确认登录身份" busy={!!busy} onClose={() => { setReauthenticate(false); setPasswordError(""); }}><p>为保护账号，请验证当前密码。验证后仍需由你确认授权。</p><form className="stack-form" onSubmit={event => { void confirmIdentity(event); }}><label>当前密码<input name="password" type="password" required autoComplete="current-password" autoFocus /></label>{passwordError && <p role="alert">{passwordError}</p>}<button className="primary" disabled={!!busy}>{busy || "验证密码"}</button></form></Dialog>}
  </main>;
}
