import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  PriceScaleMode,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type AutoscaleInfo,
  type UTCTimestamp,
} from "lightweight-charts";
import { ApiError, api, jsonBody } from "./api";
import { ResearchWorkspace } from "./ResearchWorkspace";
import { AdminConsole } from "./AdminConsole";
import { ShadowWorkspace } from "./ShadowWorkspace";
import { AlertRuleEditor } from "./AlertRuleEditor";
import { ChartContextMenu } from "./ChartContextMenu";
import { ChartStatistics } from "./ChartStatistics";
import { useMarketBars } from "./useMarketBars";
import { LatestSelection, startIndicatorRequest } from "./marketRequests";
import { drillNeedsRefresh, drillStateLabel, executionObservation, reconciliationNotice } from "./executionStatus";
import { chartBars, TIMEFRAMES, type WatchQuote } from "./marketCache";
import { MarketCacheStatus } from "./MarketCacheStatus";
import { PatternPanel } from "./PatternPanel";
import { CoinglassCacheStatus, CoinglassPatternReview } from "./CoinglassContext";
import type { CoinglassBundle, CoinglassScenario } from "./coinglassModel";
import { coinglassNumber, coinglassRetrySeconds } from "./coinglassModel";
import { useCoinglassChart } from "./useCoinglassChart";
import { useCoinglassClock } from "./useCoinglassClock";
import { ChartStudiesPicker } from "./ChartStudiesPicker";
import { ChartStudyReadouts } from "./ChartStudyReadouts";
import { normalizeChartStudiesSettings, type ChartStudiesSettings } from "./chartStudies";
import { buildCoinglassChartStudies, type CoinglassChartMetricId } from "./curatedCoinglass";
import { attachVolumeProfile } from "./volumeProfilePrimitive";
import type { VolumeProfileRange } from "./volumeProfile";
import { useFineVolumeProfile } from "./useFineVolumeProfile";
import { captureChartScreenshot } from "./chartScreenshot";
import { attachCoinglassScenario } from "./coinglassScenarioOverlay";
import { overlayGeometry, overlayColors, type PatternEvent } from "./patternModel";
import { attachPatternOverlays } from "./patternOverlays";
import { alignedIndicator, chartPriceFormat, priceText, candleUpdateKind } from "./chartModel";
import type { Spec } from "./researchModel";
import { InstrumentPicker } from "./InstrumentPicker";
import { McpWorkspace } from "./McpWorkspace";
import { PaperOrdersWorkspace } from "./PaperOrdersWorkspace";
import { OAuthConsent, OAUTH_CONSENT_PATH, oauthConsentRequest } from "./OAuthConsent";
import { NotificationRouteEditor } from "./NotificationRouteEditor";
import { Dialog, PageState, PagedList, Pagination, useAction, useConfirm } from "./ui";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";
import { appendTelegramUserId, parseTelegramUserId } from "./telegramUserId";
import type {
  AlertRule,
  Bar,
  Instrument,
  IndicatorCatalogItem,
  IndicatorPoint,
  McpClient,
  NotificationChannel,
  NotificationRoute,
  Operation,
  PaperAccount,
  PaperChart,
  PaperProposal,
  PaperTrade,
  Page,
  Session,
  SignalEvidence,
  StrategyExecutionCertification,
  TestnetDrill,
  ExecutionDecision,
  ExchangeAccount,
  TelegramConnection,
  PatternCatalogItem,
  VersionedResource,
  Watchlist,
} from "./types";

type View = "chart" | "alerts" | "backtests" | "trading" | "shadow" | "exchange" | "telegram" | "mcp" | "data" | "security" | "notifications";
const EMPTY_BARS: Bar[] = [];
const EMPTY_INDICATOR_POINTS: IndicatorPoint[] = [];

const VIEW_PATHS: Record<View, string> = {
  chart: "/markets/chart", alerts: "/strategies/alerts", trading: "/trading/paper",
  backtests: "/strategies/backtests",
  shadow: "/trading/shadow",
  exchange: "/trading/exchange-accounts", mcp: "/integrations/mcp",
  data: "/integrations/data-sources", notifications: "/notifications/inbox",
  security: "/settings/account", telegram: "/settings/notifications",
};

type IndicatorNodeSpec = {
  instance_id: string;
  indicator_id: string;
  params: Record<string, number>;
  depends_on: string[];
};

const INDICATOR_PRESETS: Array<IndicatorNodeSpec & { label: string; color: string }> = [
  { instance_id: "ema20", indicator_id: "ema", params: { period: 20 }, depends_on: [], label: "EMA 20", color: "#4f9cff" },
  { instance_id: "ema50", indicator_id: "ema", params: { period: 50 }, depends_on: [], label: "EMA 50", color: "#f3b45f" },
  { instance_id: "sma20", indicator_id: "sma", params: { period: 20 }, depends_on: [], label: "SMA 20", color: "#b48cff" },
  { instance_id: "boll20", indicator_id: "boll", params: { period: 20, deviations: 2 }, depends_on: [], label: "BOLL 20", color: "#9b87f5" },
  { instance_id: "rsi14", indicator_id: "rsi", params: { period: 14 }, depends_on: [], label: "RSI 14", color: "#e879f9" },
  { instance_id: "macd", indicator_id: "macd", params: { fast_period: 12, slow_period: 26, signal_period: 9 }, depends_on: [], label: "MACD", color: "#38bdf8" },
  { instance_id: "atr14", indicator_id: "atr", params: { period: 14 }, depends_on: [], label: "ATR 14", color: "#fb7185" },
  { instance_id: "adx14", indicator_id: "adx", params: { period: 14 }, depends_on: [], label: "ADX 14", color: "#2dd4bf" },
  { instance_id: "obv", indicator_id: "obv", params: {}, depends_on: [], label: "OBV", color: "#a3e635" },
];

const STRATEGY_PRESETS = [
  { id: "bull-reversal", name: "多头反转", patterns: ["double_bottom", "triple_bottom", "inverse_head_shoulders", "cup_handle", "falling_wedge"] },
  { id: "bear-reversal", name: "空头反转", patterns: ["double_top", "triple_top", "head_shoulders", "inverse_cup_handle", "rising_wedge"] },
  { id: "bull-continuation", name: "多头延续", patterns: ["bull_flag", "bull_pennant", "ascending_triangle", "uptrend_channel"] },
  { id: "bear-continuation", name: "空头延续", patterns: ["bear_flag", "bear_pennant", "descending_triangle", "downtrend_channel"] },
] as const;

export function App() {
  const [session, setSession] = useState<Session | null>();
  const [notice, setNotice] = useState("");
  const refreshSession = useCallback(async () => {
    try {
      setSession(await api<Session>("/auth/me"));
      if (["/", "/login"].includes(location.pathname)) history.replaceState({}, "", VIEW_PATHS.chart);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setSession(null);
      else setNotice(errorMessage(error));
    }
  }, []);

  useEffect(() => void refreshSession(), [refreshSession]);
  useEffect(() => { const expired = () => setSession(null); window.addEventListener("gatescope:session-expired", expired); return () => window.removeEventListener("gatescope:session-expired", expired); }, []);
  if (session === undefined) return notice ? <main className="auth-shell"><section className="auth-card"><Brand /><h1>暂时无法读取会话</h1><PageState error={notice} onRetry={() => { setNotice(""); void refreshSession(); }} /></section></main> : <Splash label="正在建立安全会话…" />;
  if (session === null) return <Login onLogin={refreshSession} oauth={location.pathname === OAUTH_CONSENT_PATH} />;
  if (session.mfa_required) return <MfaGate session={session} onVerified={refreshSession} />;
  if (location.pathname === OAUTH_CONSENT_PATH) return <OAuthConsent session={session} requestId={oauthConsentRequest(location.search)} onSessionExpired={refreshSession} />;
  if (location.pathname === "/admin" || location.pathname.startsWith("/admin/")) return <AdminConsole session={session} />;
  return <Workbench session={session} notice={notice} setNotice={setNotice} />;
}

function Login({ onLogin, oauth = false }: { onLogin: () => Promise<void>; oauth?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [workspaces, setWorkspaces] = useState<Array<{ slug: string; name: string }>>([]);
  const loadWorkspaces = useCallback(() => {
    setError("");
    void api<{ items: Array<{ slug: string; name: string }> }>("/auth/workspaces")
      .then((payload) => setWorkspaces(payload.items))
      .catch((reason) => setError(errorMessage(reason)));
  }, []);
  useEffect(loadWorkspaces, [loadWorkspaces]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    try {
      const encryptedSubmission = await encryptSubmission("auth_login", {
        tenant: data.get("tenant"),
        username: data.get("username"),
        password: data.get("password"),
      });
      await api("/auth/login", {
        method: "POST",
        body: jsonBody(wrapEncryptedSubmission(encryptedSubmission)),
      });
      await onLogin();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      clearSensitiveFields(form);
      setBusy(false);
    }
  }
  return (
    <main className="auth-shell">
      <section className="auth-card">
        <Brand />
        <h1>{oauth ? "登录并连接 ChatGPT" : "登录研究与交易控制台"}</h1>
        {oauth && <p>登录后确认只读授权，不会自动连接或开单。</p>}
        <form onSubmit={submit}>
          <label>工作区<select key={workspaces.map(w => w.slug).join(":")} name="tenant" required autoComplete="organization" defaultValue={workspaces.length === 1 ? workspaces[0].slug : ""}><option value="" disabled>{workspaces.length ? "请选择工作区" : "正在读取工作区…"}</option>{workspaces.map((workspace) => <option value={workspace.slug} key={workspace.slug}>{workspace.name}（{workspace.slug}）</option>)}</select></label>
          <label>用户名<input name="username" required autoComplete="username" /></label>
          <label>密码<input name="password" type="password" required minLength={16} autoComplete="current-password" /></label>
          {error && <div className="error-banner" role="alert">{error}{!workspaces.length && <button type="button" onClick={loadWorkspaces}>重新读取工作区</button>}</div>}
          <button className="primary wide" disabled={busy || !workspaces.length}>{busy ? "验证中…" : "安全登录"}</button>
        </form>
        {!oauth && <small>LIVE 交易在当前阶段由服务端硬门禁禁用。</small>}
      </section>
    </main>
  );
}

type MfaMethod = {
  id: string;
  method_type: string;
  label: string;
  status: "PENDING" | "ACTIVE";
  created_at: string;
  confirmed_at: string | null;
};

type MfaEnrollment = { method_id: string; secret: string; provisioning_uri: string };

function MfaGate({ session, onVerified }: { session: Session; onVerified: () => Promise<void> }) {
  async function logout() {
    await api("/auth/logout", { method: "POST" });
    await onVerified();
  }
  return (
    <div className="app-shell setup-shell">
      <header className="topbar">
        <Brand />
        <nav><NavButton active onClick={() => undefined}>用户设置</NavButton><NavButton active={false} onClick={() => void logout()}>退出登录</NavButton></nav>
        <div className="identity"><span>需完成安全设置</span><strong>{session.user.username}</strong></div>
      </header>
      <main className="settings-page setup-page">
        <section className="page-title"><div><h1>用户设置</h1><span className="status paused">需要验证器</span></div></section>
        <MfaManager authenticated={false} onVerified={onVerified} />
      </main>
    </div>
  );
}

function MfaManager({ authenticated, onVerified }: { authenticated: boolean; onVerified: () => Promise<void> }) {
  const confirmation = useConfirm();
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [methods, setMethods] = useState<MfaMethod[]>([]);
  const [enrollment, setEnrollment] = useState<MfaEnrollment>();
  const [qrCode, setQrCode] = useState("");
  const [recoveryMode, setRecoveryMode] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    const payload = await api<{ items: MfaMethod[] }>("/auth/mfa-methods");
    setMethods(payload.items);
  }, []);
  useEffect(() => { void load().catch((reason) => setError(errorMessage(reason))); }, [load]);
  useEffect(() => {
    let active = true;
    setQrCode("");
    if (enrollment) {
      void QRCode.toDataURL(enrollment.provisioning_uri, {
        errorCorrectionLevel: "M", margin: 2, width: 240,
        color: { dark: "#07101fff", light: "#ffffffff" },
      }).then((result) => { if (active) setQrCode(result); }).catch(() => setError("二维码生成失败，请使用手动密钥"));
    }
    return () => { active = false; };
  }, [enrollment]);
  async function verify() {
    setError(""); setBusy(true);
    try {
      await api("/auth/mfa/verify", { method: "POST", body: jsonBody({ code }) });
      await onVerified();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  async function enroll() {
    setError(""); setBusy(true);
    try {
      setEnrollment(await api<MfaEnrollment>("/auth/mfa-methods:enroll", {
        method: "POST", body: jsonBody({ label: `网页登录验证器 ${new Date().toLocaleString()}` }),
      }));
      setCode(""); await load();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!enrollment) return;
    setError(""); setBusy(true);
    try {
      const isFirstMethod = !methods.some((method) => method.status === "ACTIVE");
      await api(`/auth/mfa-methods/${enrollment.method_id}:confirm`, {
        method: "POST", body: jsonBody({ code }),
      });
      setCode(""); setEnrollment(undefined); await load();
      if (isFirstMethod) {
        const recovery = await api<{ codes: string[] }>("/auth/recovery-codes:regenerate", { method: "POST" });
        setRecoveryCodes(recovery.codes);
      } else {
        setEnrollment(undefined);
        await onVerified();
      }
      await load();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  async function recover() {
    setError(""); setBusy(true);
    try {
      await api("/auth/recovery-codes:consume", { method: "POST", body: jsonBody({ code: recoveryCode }) });
      setRecoveryCode(""); await onVerified();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  async function remove(method: MfaMethod) {
    if (busy) return;
    if (method.status === "ACTIVE" && !await confirmation.ask("移除验证器", "该验证器将无法用于登录。请确认仍有其他可用验证器。")) return;
    setError(""); setBusy(true);
    try {
      await api(`/auth/mfa-methods/${method.id}`, { method: "DELETE" });
      if (enrollment?.method_id === method.id) setEnrollment(undefined);
      await load();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  async function copy(value: string, message: string) {
    try { await navigator.clipboard.writeText(value); setError(message); }
    catch { setError("浏览器拒绝剪贴板访问，请手动复制"); }
  }
  const activeMethods = methods.filter((method) => method.status === "ACTIVE");
  const pendingMethods = methods.filter((method) => method.status === "PENDING");
  if (recoveryCodes.length) return (
    <section className="panel mfa-manager recovery-panel">
      <div className="eyebrow">SHOWN ONCE</div><h2>保存恢复码</h2>
      <p>验证器绑定成功。每个恢复码只能使用一次，请保存到离线密码管理器；离开本页后系统不会再次显示这些明文。</p>
      <div className="recovery-grid">{recoveryCodes.map((item) => <code key={item}>{item}</code>)}</div>
      <div className="button-row"><button onClick={() => void copy(recoveryCodes.join("\n"), "恢复码已复制，请立即离线保存")}>复制全部</button><button className="primary" onClick={() => { setRecoveryCodes([]); setEnrollment(undefined); void onVerified(); }}>我已安全保存，继续</button></div>
      {error && <div className="validation-note">{error}</div>}
    </section>
  );
  return (
    <section className="panel mfa-manager">
      <header><div><div className="eyebrow">MULTI-FACTOR AUTHENTICATION</div><h2>登录验证器</h2><p>支持 Google Authenticator、Microsoft Authenticator、Authy、1Password 等标准 TOTP 验证器。</p></div><span className={`status ${activeMethods.length ? "active" : "paused"}`}>{activeMethods.length ? `${activeMethods.length} 个已启用` : "尚未设置"}</span></header>
      <div className="mfa-method-list">{methods.map((method) => <article key={method.id}><div><strong>{method.label}</strong><small>{method.method_type} · {method.status === "ACTIVE" ? `已确认 ${new Date(method.confirmed_at ?? method.created_at).toLocaleString()}` : "等待确认"}</small></div><span className={`status ${method.status === "ACTIVE" ? "active" : "paused"}`}>{method.status}</span>{(method.status === "PENDING" || authenticated) && <button disabled={busy || (method.status === "ACTIVE" && activeMethods.length <= 1)} onClick={() => void remove(method)}>{method.status === "PENDING" ? "取消" : "移除"}</button>}</article>)}</div>
      {enrollment ? <div className="mfa-enrollment"><div className="qr-wrap">{qrCode ? <img src={qrCode} alt="GateScope 验证器二维码" /> : <div className="qr-loading">正在生成二维码…</div>}</div><div className="enrollment-steps"><h3>扫描二维码</h3><p>在验证器中添加“其他账户/扫描二维码”。二维码只在当前浏览器内生成，不会发送给第三方。</p><label>无法扫码时手动输入<code>{enrollment.secret}</code></label><button onClick={() => void copy(enrollment.secret, "手动密钥已复制")}>复制手动密钥</button><label>输入验证器当前的 6 位数字<input className="mfa-code-inline" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" placeholder="000000" /></label><div className="button-row"><button disabled={busy} onClick={() => { const pending = methods.find((item) => item.id === enrollment.method_id); if (pending) void remove(pending); }}>取消绑定</button><button className="primary" disabled={busy || code.length !== 6} onClick={() => void confirm()}>验证并绑定</button></div></div></div> : <div className="mfa-actions">{!authenticated && activeMethods.length > 0 ? <>{recoveryMode ? <div className="verification-box"><label>输入一次性恢复码<input value={recoveryCode} onChange={(event) => setRecoveryCode(event.target.value)} autoComplete="one-time-code" placeholder="XXXXX-XXXXXXXXXXX" /></label><div className="button-row"><button onClick={() => setRecoveryMode(false)}>返回动态码</button><button className="primary" disabled={busy || recoveryCode.length < 8} onClick={() => void recover()}>使用恢复码登录</button></div></div> : <div className="verification-box"><label>输入验证器当前的 6 位数字<input className="mfa-code-inline" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" placeholder="000000" /></label><div className="button-row"><button onClick={() => setRecoveryMode(true)}>改用恢复码</button><button className="primary" disabled={busy || code.length !== 6} onClick={() => void verify()}>验证并进入系统</button></div></div>}</> : <button className="primary" disabled={busy || pendingMethods.length > 0} onClick={() => void enroll()}>添加验证器</button>}</div>}
      {error && <div className={error.includes("已复制") ? "validation-note" : "error-banner"} role="status">{error}<button disabled={busy} onClick={() => void load().catch(e => setError(errorMessage(e)))}>刷新验证器</button></div>}
      {authenticated && activeMethods.length === 1 && <small>至少保留一个有效验证器；如需替换，请先添加新的。</small>}
      {confirmation.dialog}
    </section>
  );
}

function Workbench({ session, notice, setNotice }: { session: Session; notice: string; setNotice: (value: string) => void }) {
  const view = viewForPath(location.pathname, session.user.role);
  const action = useAction(setNotice);
  const [attentionCount, setAttentionCount] = useState(0);
  const primaryNav = useRef<HTMLElement>(null);
  const profileMenu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => { if (profileMenu.current?.open && !profileMenu.current.contains(event.target as Node)) profileMenu.current.open = false; };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape" && profileMenu.current?.open) { profileMenu.current.open = false; profileMenu.current.querySelector("summary")?.focus(); } };
    document.addEventListener("pointerdown", dismiss); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let pending = false;
    const loadCount = async () => { if (pending || document.hidden || controller.signal.aborted) return; pending = true;
      try { const result = await api<{ attention_count: number }>("/api/v2/notifications?limit=1", { signal: controller.signal }); if (!controller.signal.aborted) setAttentionCount(result.attention_count); } catch { /* Background badge failures do not interrupt work. */ } finally { pending = false; }
    };
    loadCount();
    const timer = window.setInterval(loadCount, 15_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!window.matchMedia("(max-width: 900px)").matches) return;
    primaryNav.current?.querySelector<HTMLElement>("[aria-current='page']")
      ?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }, [view]);
  async function logout() {
    await api("/auth/logout", { method: "POST" });
    location.assign("/login");
  }
  return (
    <div className="app-shell">
      <header className="topbar">
        <Brand />
        <nav ref={primaryNav} aria-label="主要功能">
          <NavLink active={view === "chart"} href={VIEW_PATHS.chart}>行情图表</NavLink>
          <NavLink active={view === "alerts"} href={VIEW_PATHS.alerts}>概率告警</NavLink>
          <NavLink active={view === "backtests"} href={VIEW_PATHS.backtests}>策略回测</NavLink>
          <NavLink active={view === "trading"} href={VIEW_PATHS.trading}>PAPER 交易</NavLink>
          <NavLink active={view === "shadow"} href={VIEW_PATHS.shadow}>通知影子账本</NavLink>
        </nav>
        <div className="top-actions"><a className={`notification-button ${view === "notifications" ? "active" : ""}`} href={VIEW_PATHS.notifications} aria-label="消息通知" title="消息通知"><NotificationIcon />{attentionCount > 0 && <b>{Math.min(attentionCount, 99)}</b>}</a><details ref={profileMenu} className={`profile-menu ${["security", "telegram", "exchange", "mcp", "data"].includes(view) ? "active" : ""}`}><summary aria-label={`${session.user.username} 用户菜单`}><b className="profile-avatar" aria-hidden="true">{session.user.username.slice(0, 1).toUpperCase() || "U"}</b><span>{session.user.role}</span><strong>{session.user.username}</strong><i>⌄</i></summary><div><span className="profile-menu-label">个人</span><a href={VIEW_PATHS.security}>账号与验证器</a><a href={VIEW_PATHS.telegram}>Telegram 与通知</a>{session.user.role === "OWNER" && <><span className="profile-menu-label">接入与管理</span><a href={VIEW_PATHS.exchange}>交易所账户</a><a href={VIEW_PATHS.mcp}>Codex MCP</a><a href={VIEW_PATHS.data}>数据源与运行状态</a><span className="profile-menu-label">工作区管理</span><a href="/admin/overview">管理后台 ↗</a></>}<button className="profile-menu-logout" disabled={!!action.busy} onClick={() => void action.run("退出中…", logout)}>退出登录</button></div></details></div>
      </header>
      {notice && <button role="status" className="toast" onClick={() => setNotice("")}>{notice}</button>}
      {view === "forbidden" && <main className="settings-page"><h1>当前账号无此模块权限</h1><p>此页面由工作区管理员管理。个人 Telegram 和验证器可在用户设置中配置。</p><a href={VIEW_PATHS.security}>进入用户设置</a></main>}
      {view === "notfound" && <main className="settings-page"><h1>页面不存在</h1><a href={VIEW_PATHS.chart}>返回行情图表</a></main>}
      {view === "chart" && <ChartWorkspace notify={setNotice} session={session} />}
      {view === "alerts" && <AlertsWorkspace notify={setNotice} session={session} />}
      {view === "backtests" && <ResearchWorkspace session={session} />}
      {view === "trading" && <TradingWorkspace session={session} notify={setNotice} />}
      {view === "shadow" && <ShadowWorkspace />}
      {view === "exchange" && <ExchangeWorkspace session={session} notify={setNotice} />}
      {view === "telegram" && <TelegramWorkspace session={session} notify={setNotice} />}
      {view === "mcp" && <McpWorkspace userId={session.user.id} notify={setNotice} />}
      {view === "data" && <DataSourcesWorkspace session={session} notify={setNotice} />}
      {view === "security" && <SecurityWorkspace session={session} />}
      {view === "notifications" && <NotificationCenter notify={setNotice} />}
    </div>
  );
}

function ChartWorkspace({ notify, session }: { notify: (value: string) => void; session: Session }) {
  const [statisticsOpen, setStatisticsOpen] = useState(false);
  const action = useAction(notify);
  const [layoutReady, setLayoutReady] = useState(false);
  const [marketError, setMarketError] = useState("");
  const userSelected = useRef(false);
  const selection = useRef(new LatestSelection());
  useEffect(() => () => selection.current.invalidate(), []);
  const coinDialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchResults, setSearchResults] = useState<Instrument[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [activeResult, setActiveResult] = useState(0);
  const [watchBusy, setWatchBusy] = useState<string>();
  const [selected, setSelected] = useState<Instrument>();
  const [timeframe, setTimeframe] = useState("15m");
  const [patternSelection, setPatternSelection] = useState<{ key: string; items: PatternEvent[] }>({ key: "", items: [] });
  const patternKey = `${selected?.id ?? "none"}:${timeframe}`;
  const [coinglassOpen,setCoinglassOpen]=useState(false),[studiesOpen,setStudiesOpen]=useState(false);
  const [chartStudies,setChartStudies]=useState<ChartStudiesSettings>(()=>normalizeChartStudiesSettings(undefined));
  const coinglassMetrics=chartStudies.coinglassMetrics;
  const [coinglassEvent,setCoinglassEvent]=useState<PatternEvent>(),[coinglassScenario,setCoinglassScenario]=useState<CoinglassScenario|null>(null);
  const cg=useCoinglassChart(selected?.id,timeframe,coinglassOpen||coinglassMetrics.length>0,`${session.user.tenant_id}:${session.user.id}`);
  const cgNow=useCoinglassClock(cg.bundle?.retry_at);
  useEffect(()=>{setCoinglassEvent(undefined);setCoinglassScenario(null);},[patternKey]);
  function openCoinglass(event:PatternEvent){setCoinglassOpen(true);setCoinglassEvent(event);setCoinglassScenario(null);setPatternSelection({key:patternKey,items:[event]});}
  const patternOverlays = patternSelection.key === patternKey ? patternSelection.items : [];
  useEffect(() => setPatternSelection({ key: patternKey, items: [] }), [patternKey]);
  const marketData = useMarketBars(selected?.id, timeframe,`${session.user.tenant_id}:${session.user.id}`);
  const bars = marketData.page?.items ?? EMPTY_BARS, quality = marketData.page?.quality_flags ?? [];
  const barsLoading = marketData.loading, dataWatermark = marketData.page?.data_watermark;
  const loadBars = marketData.reload;
  const liveBar = marketData.page?.cache?.live_bar;
  const displayedBars = useMemo(() => chartBars(bars, liveBar), [bars, liveBar]);
  const searchRequest = useRef(0);
  const searchInput = useRef<HTMLInputElement>(null);
  const resultList = useRef<HTMLDivElement>(null);
  const [watchlists, setWatchlists] = useState<Watchlist[]>([]);
  const [watchQuotes, setWatchQuotes] = useState<WatchQuote[]>([]), [watchQuotesFailed, setWatchQuotesFailed] = useState(false);
  const [indicatorCatalog, setIndicatorCatalog] = useState<IndicatorCatalogItem[]>([]);
  const [indicatorTemplates, setIndicatorTemplates] = useState<VersionedResource[]>([]);
  const [indicatorNodes, setIndicatorNodes] = useState<IndicatorNodeSpec[]>(
    INDICATOR_PRESETS.filter((item) => ["ema20", "ema50"].includes(item.instance_id)),
  );
  const [indicatorResult, setIndicatorResult] = useState<{key:string; points:IndicatorPoint[]}>({key:"",points:EMPTY_INDICATOR_POINTS});
  const indicatorKey = `${selected?.id ?? ""}:${timeframe}:${JSON.stringify(indicatorNodes)}`;
  const indicatorPoints = indicatorResult.key === indicatorKey ? indicatorResult.points : EMPTY_INDICATOR_POINTS;
  const [indicatorOpen, setIndicatorOpen] = useState(false);
  const [indicatorLoading, setIndicatorLoading] = useState(false);
  const [indicatorError, setIndicatorError] = useState("");
  const retryIndicators = useRef<() => void>(() => {});
  const loadWatchlists = useCallback(async () => {
    const page = await api<Page<Watchlist>>("/api/v2/watchlists");
    setWatchlists(page.items);
    return page.items;
  }, []);
  useEffect(() => {
    if (!searchOpen) { searchRequest.current += 1; return; }
    const requestId = ++searchRequest.current;
    const timer = setTimeout(() => {
      setSearchLoading(true);
      void api<Page<Instrument>>(`/api/v2/instruments?q=${encodeURIComponent(query.trim())}&limit=24`)
        .then((page) => {
          if (requestId !== searchRequest.current) return;
          setSearchResults(page.items);
          setActiveResult(0);
        })
        .catch((error) => {
          if (requestId === searchRequest.current) notify(errorMessage(error));
        })
        .finally(() => {
          if (requestId === searchRequest.current) setSearchLoading(false);
        });
    }, query.trim() ? 140 : 0);
    return () => clearTimeout(timer);
  }, [query, notify, searchOpen]);
  useEffect(() => {
    if (!searchOpen) return;
    coinDialog.current?.showModal();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => searchInput.current?.focus());
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      coinDialog.current?.close();
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [searchOpen]);
  useEffect(() => {
    if (!searchOpen) return;
    resultList.current?.querySelector<HTMLElement>(`[data-result-index="${activeResult}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeResult, searchOpen]);
  useEffect(() => {
    void loadWatchlists().catch(error => setMarketError(errorMessage(error)));
    const timer = window.setInterval(() => { if (!document.hidden) void loadWatchlists().catch(() => undefined); }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadWatchlists]);
  useEffect(() => {
    const controller = new AbortController(); let pending = false;
    async function load() {
      if (pending || document.hidden || controller.signal.aborted) return;
      pending = true;
      try {
        const result = await api<{ items: WatchQuote[] }>("/api/v2/market/watchlist-cache", { signal: controller.signal });
        if (!controller.signal.aborted) { setWatchQuotes(result.items); setWatchQuotesFailed(false); }
      } catch { if (!controller.signal.aborted) setWatchQuotesFailed(true); }
      finally { pending = false; }
    }
    void load(); const timer = setInterval(() => void load(), 10000);
    const resume = () => void load(); window.addEventListener("online", resume); document.addEventListener("visibilitychange", resume);
    return () => { controller.abort(); clearInterval(timer); window.removeEventListener("online", resume); document.removeEventListener("visibilitychange", resume); };
  }, []);
  useEffect(() => {
    void Promise.allSettled([
      api<{ items: IndicatorCatalogItem[] }>("/api/v2/indicators/catalog"),
      api<Page<VersionedResource>>("/api/v2/indicator-templates"),
      api<Page<VersionedResource>>("/api/v2/chart-layouts"),
    ]).then(async ([catalog, templates, layouts]) => {
      if (catalog.status === "fulfilled") setIndicatorCatalog(catalog.value.items);
      if (templates.status === "fulfilled") setIndicatorTemplates(templates.value.items);
      const saved = templates.status === "fulfilled" ? templates.value.items.find((item) => item.name === "默认指标") : undefined;
      const nodes = saved?.spec.nodes;
      if (Array.isArray(nodes)) {
        const valid = nodes.filter(isIndicatorNodeSpec);
        setIndicatorNodes(valid);
      }
      const layout = layouts.status === "fulfilled" ? layouts.value.items.find(i => i.name === "默认工作台")?.spec : undefined;
      setChartStudies(normalizeChartStudiesSettings(layout?.chart_studies));
      if (layout && typeof layout.instrument_id === "string" && typeof layout.symbol === "string" && !userSelected.current) {
        const found = await api<Page<Instrument>>(`/api/v2/instruments?q=${encodeURIComponent(layout.symbol)}&limit=24`);
        const instrument = found.items.find(i => i.id === layout.instrument_id);
        if (instrument && !userSelected.current) selection.current.commit(() => { setSelected(instrument); if (TIMEFRAMES.includes(String(layout.timeframe))) setTimeframe(String(layout.timeframe)); });
      }
    }).catch((error) => notify(errorMessage(error))).finally(() => setLayoutReady(true));
  }, [notify]);
  const watched = watchlists.flatMap((list) => list.items.map((item) => ({ list, item })));
  const firstWatched = watched[0]?.item;
  useEffect(() => {
    if (layoutReady && !selected && firstWatched) {
      void selectWatched(
        firstWatched.instrument_id, firstWatched.symbol, firstWatched.default_timeframe,
      ).catch(error => setMarketError(errorMessage(error)));
    }
  }, [firstWatched?.instrument_id, selected?.id, layoutReady]);
  const barFingerprint = useMemo(() => bars.map(b => [b.open_time, b.revision, b.open, b.high, b.low, b.close, b.volume].join(",")).join("|"), [bars]);
  useEffect(() => {
    setIndicatorError(""); retryIndicators.current = () => {};
    if (!selected || !indicatorNodes.length || !bars.length) {
      setIndicatorResult({key:indicatorKey,points:EMPTY_INDICATOR_POINTS});
      setIndicatorLoading(false);
      return;
    }
    const request = startIndicatorRequest(signal => api<{ items: IndicatorPoint[] }>("/api/v2/indicators/preview", {
      method: "POST",
      signal,
      body: jsonBody({
        instrument_id: selected.id, timeframe, limit: 1500,
        from_time: bars[0].open_time, to_time: bars.at(-1)!.open_time,
        nodes: indicatorNodes,
      }),
    }), {
      success: result => { setIndicatorResult({key:indicatorKey,points:result.items}); setIndicatorError(""); },
      error: (error, retrying) => setIndicatorError(`指标暂未更新：${errorMessage(error)}。${retrying ? "稍后自动重试" : "可手动重试"}；已有指标仅供缓存参考`),
      loading: setIndicatorLoading,
      visible: () => !document.hidden,
    });
    retryIndicators.current = request.retry;
    window.addEventListener("online", request.resume); document.addEventListener("visibilitychange", request.resume);
    return () => { request.dispose(); window.removeEventListener("online", request.resume); document.removeEventListener("visibilitychange", request.resume); };
  }, [indicatorKey, barFingerprint]);
  const watchedIds = new Set(watched.map(({ item }) => item.instrument_id));
  async function selectWatched(instrumentId: string, symbol: string, defaultTimeframe: string) {
    userSelected.current = true;
    await selection.current.resolve(async () => {
      const cached = selected?.id === instrumentId ? selected : searchResults.find((item) => item.id === instrumentId);
      if (cached) return cached;
      const page = await api<Page<Instrument>>(`/api/v2/instruments?q=${encodeURIComponent(symbol)}&limit=24`);
      return page.items.find((item) => item.id === instrumentId);
    }, instrument => {
      if (!instrument) { notify("自选标的已不可用"); return; }
      setSelected(instrument); setTimeframe(defaultTimeframe);
    });
  }
  async function removeWatched(list: Watchlist, instrumentId: string) {
    const selectionVersion = selection.current.current;
    try {
      await api(`/api/v2/watchlists/${list.id}/items/${instrumentId}`, {
        method: "DELETE", headers: { "If-Match": `"${list.etag}"` },
      });
      const refreshed = await loadWatchlists();
      if (selected?.id === instrumentId && selection.current.current === selectionVersion) {
        const next = refreshed.flatMap((item) => item.items)[0];
        if (next) await selectWatched(next.instrument_id, next.symbol, next.default_timeframe);
        else selection.current.commit(() => setSelected(undefined));
      }
      notify("已移出自选");
    } catch (error) { notify(errorMessage(error)); }
  }
  async function addToWatchlist(instrument: Instrument | undefined = selected) {
    if (!instrument || watchedIds.has(instrument.id)) return;
    setWatchBusy(instrument.id);
    try {
      let target = watchlists[0];
      if (!target) {
        target = await api<Watchlist>("/api/v2/watchlists", { method: "POST", body: jsonBody({ name: "主要观察" }) });
      }
      await api(`/api/v2/watchlists/${target.id}/items`, {
        method: "POST",
        headers: { "If-Match": `"${target.etag}"` },
        body: jsonBody({ instrument_id: instrument.id, default_timeframe: timeframe }),
      });
      notify(`${instrument.symbol} 已加入自选`);
      await loadWatchlists();
    } catch (error) { notify(errorMessage(error)); }
    finally { setWatchBusy(undefined); }
  }
  async function saveLayout() {
    try {
      const layouts = await api<Page<{ id: string; name: string; etag: number }>>("/api/v2/chart-layouts");
      const spec = { schema_version: 1, symbol: selected?.symbol, instrument_id: selected?.id, timeframe, panes: ["price", "volume"], indicator_ids: indicatorNodes.map((item) => item.instance_id), chart_studies: normalizeChartStudiesSettings(chartStudies) };
      const existing = layouts.items.find((item) => item.name === "默认工作台");
      if (existing) await api(`/api/v2/chart-layouts/${existing.id}`, { method: "PATCH", headers: { "If-Match": `"${existing.etag}"` }, body: jsonBody({ name: existing.name, spec }) });
      else await api("/api/v2/chart-layouts", { method: "POST", body: jsonBody({ name: "默认工作台", spec }) });
      const templateSpec = { schema_version: 1, nodes: indicatorNodes };
      const template = indicatorTemplates.find((item) => item.name === "默认指标");
      const savedTemplate = template
        ? await api<VersionedResource>(`/api/v2/indicator-templates/${template.id}`, { method: "PATCH", headers: { "If-Match": `"${template.etag}"` }, body: jsonBody({ name: template.name, spec: templateSpec }) })
        : await api<VersionedResource>("/api/v2/indicator-templates", { method: "POST", body: jsonBody({ name: "默认指标", spec: templateSpec }) });
      setIndicatorTemplates([savedTemplate, ...indicatorTemplates.filter((item) => item.id !== savedTemplate.id)]);
      notify("图表与指标已保存");
    } catch (error) { notify(errorMessage(error)); }
  }
  function toggleIndicator(preset: IndicatorNodeSpec) {
    setIndicatorNodes((current) => current.some((item) => item.instance_id === preset.instance_id)
      ? current.filter((item) => item.instance_id !== preset.instance_id)
      : [...current, preset]);
  }
  function applyStrategyIndicators(spec: Spec) {
    const names = spec.strategy === "reversion" ? ["boll20", "rsi14", "adx14", "atr14"] : ["ema20", "ema50", "rsi14", "adx14", "atr14"];
    setIndicatorNodes(INDICATOR_PRESETS.filter(n => names.includes(n.instance_id)).map(n => ({ ...n, params: { ...n.params, ...(n.instance_id === "ema20" ? { period: spec.fast_period } : n.instance_id === "ema50" ? { period: spec.slow_period } : n.instance_id === "boll20" ? { period: spec.lookback } : {}) } })));
    setStatisticsOpen(false); notify("已应用报告使用的指标参数；完整交易条件请查看报告");
  }
  function chooseInstrument(instrument: Instrument) {
    userSelected.current = true;
    selection.current.commit(() => setSelected(instrument));
    setSearchOpen(false);
  }
  function openSearch() {
    setQuery("");
    setActiveResult(0);
    setSearchOpen(true);
  }
  function searchKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (!searchResults.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveResult((current) => Math.min(current + 1, searchResults.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveResult((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const instrument = searchResults[activeResult];
      if (instrument) chooseInstrument(instrument);
    }
  }
  const latestBar = displayedBars.at(-1);
  const previousBar = displayedBars.at(-2);
  const latestPrice = latestBar ? Number(latestBar.close) : undefined;
  const previousPrice = previousBar ? Number(previousBar.close) : undefined;
  const priceChange = latestPrice !== undefined && previousPrice
    ? ((latestPrice - previousPrice) / previousPrice) * 100
    : undefined;
  return (
    <main className="workspace-grid market-workspace">
      <aside className="symbol-panel panel">
        <header className="watchlist-head">
          <div><small>WATCHLIST</small><h2>我的自选</h2></div>
          <button className="icon-button" aria-label="搜索并添加币种" title="搜索并添加币种" onClick={openSearch}><PlusIcon /></button>
        </header>
        <div className="watchlist-meta"><span>{watched.length} 个标的</span><button onClick={openSearch}><SearchIcon />搜索添加</button></div>
        <div className="watch-symbols">{watched.length ? watched.map(({ list, item }) => {
          const quote = watchQuotes.find(q => q.instrument_id === item.instrument_id && q.timeframe === item.default_timeframe);
          return (
          <div className="watch-symbol" key={`${list.id}:${item.instrument_id}`}>
            <button className={`watch-select ${selected?.id === item.instrument_id ? "selected" : ""}`} onClick={() => void selectWatched(item.instrument_id, item.symbol, item.default_timeframe).catch(error => notify(errorMessage(error)))}>
              <CoinMark symbol={item.symbol} />
              <span className="watch-copy"><strong>{marketSymbol(item.symbol)}</strong><small>Gate · 自选</small></span>
              <span className={`watch-price ${quote?.stale || watchQuotesFailed ? "stale" : ""}`}><b>{quote?.price ? formatMarketPrice(Number(quote.price)) : "同步中"}</b><small>{quote?.stale || watchQuotesFailed ? "缓存 / 延迟" : item.default_timeframe}</small></span>
            </button>
            <button className="watch-remove" disabled={!!action.busy} aria-label={`将 ${item.symbol} 移出自选`} title="移出自选" onClick={() => void action.run("移除中", () => removeWatched(list, item.instrument_id))}>×</button>
          </div>
        ); }) : <div className="watchlist-empty"><CoinOrbitIcon /><strong>建立你的行情桌面</strong><span>搜索币种并加入自选，主页只保留你关注的市场。</span><button className="primary" onClick={openSearch}><SearchIcon />查找币种</button></div>}</div>
        {watched.length > 0 && <button className="watchlist-add" onClick={openSearch}><PlusIcon />添加币种</button>}
      </aside>
      <section className="chart-area">
        <PageState error={marketError || marketData.error} onRetry={() => void Promise.all([loadWatchlists(), loadBars()]).then(() => setMarketError("")).catch(error => setMarketError(errorMessage(error)))} />
        <div className="market-head panel">
          <div className="market-identity">
            <button className="market-symbol-trigger" onClick={openSearch} aria-haspopup="dialog">
              {selected ? <CoinMark symbol={selected.symbol} large /> : <SearchIcon />}
              <span><small>{selected ? `${selected.market} · ${selected.settle || "USDT"} 永续` : "MARKET"}</small><strong>{selected ? marketSymbol(selected.symbol) : "选择币种"}</strong></span>
              <ChevronIcon />
            </button>
            {selected && <div className="market-quote"><strong>{formatMarketPrice(latestPrice)}</strong><span className={priceChange === undefined ? "" : priceChange >= 0 ? "positive" : "negative"}>{priceChange === undefined ? "等待最新行情" : `${priceChange >= 0 ? "+" : ""}${priceChange.toFixed(2)}%`}</span></div>}
          </div>
          <div className="timeframes">{TIMEFRAMES.map((value) => <button disabled={!selected || !layoutReady} className={value === timeframe ? "active" : ""} onClick={() => setTimeframe(value)} key={value}>{value}</button>)}</div>
          <div className="button-row market-tools">
            <button disabled={!selected || !layoutReady} aria-haspopup="dialog" aria-expanded={studiesOpen} onClick={()=>setStudiesOpen(true)}>精选指标{coinglassMetrics.length+Number(chartStudies.vpvr.enabled)>0?` · ${coinglassMetrics.length+Number(chartStudies.vpvr.enabled)}`:""}</button>
            <button disabled={!selected} aria-pressed={coinglassOpen} onClick={()=>setCoinglassOpen(value=>!value)}>Coinglass 数据</button>
            <div className="indicator-control">
              <button className={indicatorOpen ? "active" : ""} onClick={() => setIndicatorOpen((value) => !value)}>指标 · {indicatorNodes.length}</button>
              {indicatorOpen && <Dialog title="技术指标" onClose={() => setIndicatorOpen(false)}><div className="indicator-editor">
                <p className="muted-copy">主图与副图按同一 K 线时间轴展示；修改参数后重新计算。</p>
                <div>{INDICATOR_PRESETS.map((preset) => {
                  const active = indicatorNodes.some((item) => item.instance_id === preset.instance_id);
                  const definition = indicatorCatalog.find((item) => item.indicator_id === preset.indicator_id);
                  const current = indicatorNodes.find(item => item.instance_id === preset.instance_id);
                  return <div className="indicator-editor-row" key={preset.instance_id}><button className={active ? "selected" : ""} onClick={() => toggleIndicator(preset)} key={preset.instance_id}><i style={{ background: preset.color }} /><span><strong>{indicatorNodeLabel(current ?? preset)}</strong><small>{definition ? `预热 ${definition.warmup_bars} 根` : "加载中"}</small></span><b>{active ? "✓" : "+"}</b></button>{current && <div className="indicator-params">{Object.entries(current.params).map(([key, value]) => <label key={key}>{key}<input aria-label={`${current.instance_id} ${key}`} type="number" min={definition?.parameters[key]?.minimum ?? 1} max={definition?.parameters[key]?.maximum ?? 500} step={definition?.parameters[key]?.type === "number" ? .1 : 1} defaultValue={value} onBlur={e => { const next = Number(e.target.value), min = definition?.parameters[key]?.minimum ?? 1, max = definition?.parameters[key]?.maximum ?? 500; if (!e.target.value || !Number.isFinite(next) || next < min || next > max || (definition?.parameters[key]?.type !== "number" && !Number.isInteger(next))) { e.target.value = String(value); notify(`参数应为 ${min}–${max} 范围内的有效数值`); return; } setIndicatorNodes(nodes => nodes.map(n => n.instance_id === current.instance_id ? { ...n, params: { ...n.params, [key]: next } } : n)); }} /></label>)}</div>}</div>;
                })}</div>
                <footer><span>{indicatorLoading ? "计算中…" : "仅使用已终结 K 线"}</span><button onClick={() => setIndicatorNodes([])}>清空</button></footer>
              </div></Dialog>}
            </div>
            <button disabled={!selected || watchedIds.has(selected.id) || watchBusy === selected.id} onClick={() => void addToWatchlist()}>{selected && watchedIds.has(selected.id) ? "✓ 已自选" : watchBusy ? "添加中…" : "+ 自选"}</button>
            <button disabled={!selected || !!action.busy} onClick={() => void action.run("保存中…", saveLayout)}>{action.busy || "保存"}</button>
          </div>
        </div>
        <MarketCacheStatus cache={marketData.page?.cache ? { ...marketData.page.cache, stale: marketData.page.cache.stale || !marketData.lastChecked } : undefined} error={marketData.error} refreshing={marketData.refreshing} onRefresh={() => void loadBars()} />
        {indicatorError && <div className="page-error" role="status"><span>{indicatorError}</span><button disabled={indicatorLoading} onClick={() => retryIndicators.current()}>{indicatorLoading ? "指标重试中…" : "重试指标"}</button></div>}
        {coinglassOpen && <section className="coinglass-controls" aria-label="Coinglass 数据状态"><header><strong>Coinglass · {cg.bundle?.scope ? `${cg.bundle.scope.exchange} · ${cg.bundle.scope.symbol}` : "等待精确合约匹配"}</strong><button disabled={cg.loading||coinglassRetrySeconds(cg.bundle,cgNow)>0} onClick={cg.reload}>{cg.loading?"读取中…":coinglassRetrySeconds(cg.bundle,cgNow)>0?"冷却中":"刷新数据"}</button></header><PageState error={cg.error} loading={cg.loading&&!cg.bundle} />{cg.bundle&&<CoinglassCacheStatus bundle={cg.bundle} now={cgNow} autoPolling />}<details className="coinglass-method"><summary>数据口径</summary><p>仅使用同交易所、同合约与原生周期数据，缺口不补零；最新缓存窗口最多 256 期。资金费率、多空比和实际爆仓量分别展示，不代表未来价格分布。</p></details></section>}
        {coinglassScenario && <div className="quality-strip"><span>独立观察情景 · 从本次研判时刻开始，不改原预测</span><button onClick={()=>setCoinglassScenario(null)}>清除情景</button></div>}
        <MarketChart coinglass={cg.bundle} coinglassMetrics={coinglassMetrics} vpvr={chartStudies.vpvr} dataScope={`${session.user.tenant_id}:${session.user.id}`} onStudies={()=>setStudiesOpen(true)} coinglassScenario={coinglassScenario} patternOverlays={patternOverlays} onIndicators={() => setIndicatorOpen(true)} onStatistics={selected ? () => setStatisticsOpen(true) : undefined} onWatch={selected && !watchedIds.has(selected.id) && !watchBusy ? () => void addToWatchlist() : undefined} notify={notify} bars={displayedBars} indicatorPoints={indicatorPoints} indicatorNodes={indicatorNodes} loading={barsLoading} timeframe={timeframe} chartKey={`${selected?.id ?? "none"}:${timeframe}`} />
        <div className="quality-strip"><span>数据点 {bars.length}</span><span>研究质量：{quality.join(" · ") || (barsLoading ? "正在同步所选周期" : "等待数据")}</span><span>水位 {dataWatermark ? new Date(dataWatermark).toLocaleString() : "--"}</span><strong>LIVE 不可用</strong></div>
        {selected && <PatternPanel key={patternKey} instrument={selected} timeframe={timeframe} bars={bars} onCoinglass={openCoinglass} onSelection={items => setPatternSelection({ key: patternKey, items })} />}
      </section>
      {statisticsOpen && selected && <ChartStatistics key={`${selected.id}:${timeframe}`} instrument={selected} timeframe={timeframe} canCreate={session.user.role !== "VIEWER"} onClose={() => setStatisticsOpen(false)} onApplyIndicators={applyStrategyIndicators} />}
      {studiesOpen && <ChartStudiesPicker settings={chartStudies} onChange={next=>setChartStudies(normalizeChartStudiesSettings(next))} onClose={()=>setStudiesOpen(false)} bundle={cg.bundle} loading={cg.loading} error={cg.error} onRefresh={cg.reload} />}
      {coinglassEvent && selected && <CoinglassPatternReview key={`${patternKey}:${coinglassEvent.evidence_id}`} event={coinglassEvent} instrumentId={selected.id} timeframe={timeframe} onClose={()=>setCoinglassEvent(undefined)} onPreview={value=>{setCoinglassScenario(value);setCoinglassEvent(undefined);window.setTimeout(()=>document.querySelector(".market-workspace .chart-panel")?.scrollIntoView({behavior:"smooth",block:"center"}),0);}} />}
      {searchOpen && <dialog ref={coinDialog} className="coin-picker-native" aria-labelledby="coin-picker-title" onCancel={e => { e.preventDefault(); setSearchOpen(false); }} onClick={event => { if (event.target === event.currentTarget) setSearchOpen(false); }}>
        <section className="coin-picker">
          <header><div><small>MARKET SEARCH</small><h2 id="coin-picker-title">切换或添加币种</h2></div><button className="picker-close" aria-label="关闭币种搜索" onClick={() => setSearchOpen(false)}>×</button></header>
          <div className="coin-search"><SearchIcon /><input ref={searchInput} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={searchKeyDown} placeholder="输入 BTC、ETH、SOL…" autoComplete="off" spellCheck={false} aria-controls="coin-search-results" aria-activedescendant={searchResults[activeResult] ? `coin-result-${searchResults[activeResult].id}` : undefined} /><kbd>ESC</kbd></div>
          <div className="picker-caption"><span>{query.trim() ? `“${query.trim()}” 的搜索结果` : "可交易合约"}</span><small>{searchLoading ? "搜索中…" : `${searchResults.length} 个结果 · ↑↓ 选择 · Enter 打开`}</small></div>
          <div className="coin-results" id="coin-search-results" ref={resultList} role="listbox" aria-label="币种搜索结果">
            {searchResults.map((instrument, index) => {
              const watchedAlready = watchedIds.has(instrument.id);
              return <div className={`coin-result ${index === activeResult ? "active" : ""}`} id={`coin-result-${instrument.id}`} data-result-index={index} role="option" aria-selected={index === activeResult} key={instrument.id} onMouseEnter={() => setActiveResult(index)}>
                <button className="coin-result-main" onClick={() => chooseInstrument(instrument)}>
                  <CoinMark symbol={instrument.symbol} />
                  <span><strong>{marketSymbol(instrument.symbol)}</strong><small>{instrument.display_name || instrument.symbol}</small></span>
                  <em>{instrument.market}<small>{instrument.settle || "USDT"} 永续</small></em>
                </button>
                <button className={`coin-result-watch ${watchedAlready ? "watched" : ""}`} disabled={watchedAlready || watchBusy === instrument.id} aria-label={watchedAlready ? `${instrument.symbol} 已在自选` : `将 ${instrument.symbol} 加入自选`} onClick={() => void addToWatchlist(instrument)}>{watchedAlready ? "✓" : "+"}</button>
              </div>;
            })}
            {!searchLoading && !searchResults.length && <div className="picker-empty"><SearchIcon /><strong>没有找到匹配币种</strong><span>尝试输入交易代码，例如 BTC 或 ETH</span></div>}
            {searchLoading && !searchResults.length && <div className="picker-loading"><i /><span>正在读取市场目录…</span></div>}
          </div>
        </section>
      </dialog>}
    </main>
  );
}

function MarketChart({ bars, coinglass, coinglassMetrics = [], vpvr, dataScope, onStudies, coinglassScenario, patternOverlays = [], indicatorPoints = [], indicatorNodes = [], loading = false, timeframe = "", chartKey = "default", levels = [], onIndicators, onStatistics, onWatch, notify }: { coinglass?:CoinglassBundle; coinglassMetrics?:CoinglassChartMetricId[]; vpvr?:ChartStudiesSettings["vpvr"]; dataScope?:string; onStudies?:()=>void; coinglassScenario?:CoinglassScenario|null; patternOverlays?: PatternEvent[]; onIndicators?: () => void; onStatistics?: () => void; onWatch?: () => void; notify?: (message: string) => void; bars: Bar[]; indicatorPoints?: IndicatorPoint[]; indicatorNodes?: IndicatorNodeSpec[]; loading?: boolean; timeframe?: string; chartKey?: string; levels?: Array<{ title: string; price: string; color: string }> }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeries = useRef<ISeriesApi<"Histogram"> | null>(null);
  const indicatorSeries = useRef<Array<ISeriesApi<"Line"> | ISeriesApi<"Histogram">>>([]);
  const indicatorRegistry = useRef(new Map<string, ISeriesApi<"Line"> | ISeriesApi<"Histogram">>());
  const indicatorSchema = useRef("");
  const previousCandles = useRef<{key:string;bars:Bar[]}>({key:"",bars:[]});
  const fittedKey = useRef("");
  const volumeProfile = useRef<ReturnType<typeof attachVolumeProfile> | null>(null);
  const [profileRange,setProfileRange] = useState<{key:string;range:VolumeProfileRange|null}>();
  const [exporting,setExporting] = useState(false);
  const exportBusy=useRef(false);
  const patternFocus = useRef("");
  const [menu, setMenu] = useState<{ x: number; y: number; price?: number; bar?: Bar }>();
  const [drawn, setDrawn] = useState<number[]>([]), [logarithmic, setLogarithmic] = useState(false), [magnet, setMagnet] = useState(true), [volumeVisible, setVolumeVisible] = useState(true);
  const [feedback, setFeedback] = useState("");
  const closeMenu = useCallback(() => setMenu(undefined), []);
  const inform = (message: string) => { setFeedback(message); notify?.(message); };
  useEffect(() => { setDrawn([]); closeMenu(); }, [chartKey, closeMenu]);
  const effectiveChartKey = chartKey === "default" ? (bars[0]?.open_time ?? chartKey) : chartKey;
  const fineProfile = useFineVolumeProfile({ instrumentId: chartKey === "default" || chartKey.startsWith("none:") ? undefined : chartKey.split(":")[0],
    timeframe, bars, range: profileRange?.key === effectiveChartKey ? profileRange.range : null, enabled: vpvr?.enabled === true,
    rows: vpvr?.rows ?? 48, valueAreaPercent: vpvr?.valueAreaPercent ?? 70, scope: dataScope });
  const screenshotState=useRef({bars,chartKey:effectiveChartKey,instrumentId:chartKey.split(":")[0],timeframe,scope:dataScope,vpvr,fine:fineProfile});
  screenshotState.current={bars,chartKey:effectiveChartKey,instrumentId:chartKey.split(":")[0],timeframe,scope:dataScope,vpvr,fine:fineProfile};
  const coinglassClockBucket=Math.floor(useCoinglassClock()/30);
  const cgStudies=useMemo(()=>buildCoinglassChartStudies(coinglass,coinglassMetrics,bars,timeframe,Date.now()/1000,chartKey==="default"?undefined:chartKey.split(":")[0]),[coinglass,coinglassMetrics.join(":"),bars,timeframe,coinglassClockBucket,chartKey]);
  // A freshness-clock tick may change labels but must not re-feed every chart
  // series when all selected numeric points and display states are unchanged.
  const cgRenderSignature=JSON.stringify(cgStudies.map(study=>[study.id,study.status,study.unit,study.data]));
  useEffect(() => {
    if (!host.current) return;
    const api = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#0b111b" }, textColor: "#93a4ba" },
      grid: { vertLines: { color: "#192231" }, horzLines: { color: "#192231" } },
      rightPriceScale: { borderColor: "#28364a" },
      timeScale: { borderColor: "#28364a", timeVisible: true },
      handleScroll: { vertTouchDrag: false },
      crosshair: { vertLine: { color: "#7797c4" }, horzLine: { color: "#7797c4" } },
    });
    const candles = api.addSeries(CandlestickSeries, { upColor: "#22c997", downColor: "#f0526e", borderVisible: false, wickUpColor: "#22c997", wickDownColor: "#f0526e" });
    const volume = api.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", priceLineVisible: false, lastValueVisible: false });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    candles.setData(bars.map((bar) => ({ time: epoch(bar.open_time), open: +bar.open, high: +bar.high, low: +bar.low, close: +bar.close })));
    volume.setData(bars.map((bar) => ({ time: epoch(bar.open_time), value: +bar.volume, color: +bar.close >= +bar.open ? "#22c99755" : "#f0526e55" })));
    api.timeScale().fitContent();
    chart.current = api; candleSeries.current = candles; volumeSeries.current = volume;
    return () => { volumeProfile.current?.detach(); volumeProfile.current=null; chart.current = null; candleSeries.current = null; volumeSeries.current = null; indicatorSeries.current = []; indicatorRegistry.current.clear(); indicatorSchema.current=""; previousCandles.current={key:"",bars:[]}; api.remove(); };
  }, []);
  useEffect(() => {
    const previous = previousCandles.current;
    const update = previous.key === effectiveChartKey ? candleUpdateKind(previous.bars,bars) : "replace";
    if (update === "none") { previousCandles.current={key:effectiveChartKey,bars}; return; }
    candleSeries.current?.applyOptions({ priceFormat: chartPriceFormat(bars.map(b => +b.low)) });
    const changed = update === "tail" ? bars.slice(Math.max(0,previous.bars.length-1)) : bars;
    const candles = changed.map((bar) => ({ time: epoch(bar.open_time), open: +bar.open, high: +bar.high, low: +bar.low, close: +bar.close }));
    const volumes = changed.map((bar) => ({ time: epoch(bar.open_time), value: +bar.volume, color: +bar.close >= +bar.open ? "#22c99755" : "#f0526e55" }));
    if (update === "replace") { candleSeries.current?.setData(candles); volumeSeries.current?.setData(volumes); }
    else if (update === "tail") { candles.forEach(bar=>candleSeries.current?.update(bar)); volumes.forEach(bar=>volumeSeries.current?.update(bar)); }
    previousCandles.current={key:effectiveChartKey,bars};
    volumeSeries.current?.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    if (bars.length && fittedKey.current !== effectiveChartKey) {
      const visible = Math.min(180, Math.max(60, Math.floor((host.current?.clientWidth ?? 900) / 7)));
      if (onIndicators && bars.length > visible) {
        chart.current?.timeScale().setVisibleLogicalRange({ from: bars.length - visible, to: bars.length + 3 });
      } else chart.current?.timeScale().fitContent();
      fittedKey.current = effectiveChartKey;
    }
  }, [bars, effectiveChartKey]);
  useEffect(()=>{
    const api=chart.current, candles=candleSeries.current;
    if(!api||!candles||!vpvr?.enabled)return;
    const controller=attachVolumeProfile(api,candles,bars,{bins:vpvr.rows,valueArea:vpvr.valueAreaPercent/100,widthPercent:vpvr.widthPercent},undefined,
      {instrumentId:chartKey.split(":")[0],timeframe,onVisibleRange:range=>setProfileRange(previous=>previous?.key===effectiveChartKey&&previous.range?.from===range?.from&&previous.range?.to===range?.to
        ?previous:{key:effectiveChartKey,range})});
    volumeProfile.current=controller;
    return()=>{controller.detach();if(volumeProfile.current===controller)volumeProfile.current=null;};
  },[effectiveChartKey,vpvr?.enabled]);
  useEffect(()=>{if(vpvr?.enabled)volumeProfile.current?.update(bars,{bins:vpvr.rows,valueArea:vpvr.valueAreaPercent/100,widthPercent:vpvr.widthPercent});},[bars,vpvr?.rows,vpvr?.valueAreaPercent,vpvr?.widthPercent]);
  useEffect(()=>{volumeProfile.current?.setProfile(fineProfile.bundle);},[fineProfile.bundle,fineProfile.key,profileRange?.range?.from,profileRange?.range?.to,effectiveChartKey,vpvr?.enabled]);
  useEffect(() => {
    const candles = candleSeries.current;
    if (!candles) return;
    const lines = [...levels, ...drawn.map(price => ({ title: "水平标记", price: String(price), color: "#f4bd66" }))].filter(level => Number.isFinite(Number(level.price))).map(level =>
      candles.createPriceLine({ title: level.title, price: Number(level.price), color: level.color, lineWidth: 1, axisLabelVisible: true }));
    return () => { if (candleSeries.current === candles) lines.forEach(line => candles.removePriceLine(line)); };
  }, [levels, drawn]);
  useEffect(() => { chart.current?.priceScale("right").applyOptions({ mode: logarithmic ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal }); }, [logarithmic]);
  useEffect(() => { chart.current?.applyOptions({ crosshair: { mode: magnet ? CrosshairMode.Magnet : CrosshairMode.Normal } }); }, [magnet]);
  useEffect(() => { volumeSeries.current?.applyOptions({ visible: volumeVisible }); }, [volumeVisible]);
  const indicatorTimeSignature = bars.map(b=>`${b.open_time}:${b.revision??1}:${b.is_final!==false}`).join("|");
  useEffect(() => {
    const api = chart.current;
    if (!api) return;
    const barRevisions = new Map(bars.map(b => [epoch(b.open_time), b.revision]));
    const schema = `${effectiveChartKey}:${JSON.stringify(indicatorNodes)}:${JSON.stringify(cgStudies.map(study=>[study.id,study.type,study.type==="line"?study.segments.length:0]))}`;
    const schemaChanged = indicatorSchema.current !== schema;
    if (schemaChanged) {
      [...indicatorSeries.current].reverse().forEach((series) => api.removeSeries(series));
      indicatorSeries.current = []; indicatorRegistry.current.clear(); indicatorSchema.current = schema;
    }
    let pane = 1;
    const addLine = (node: IndicatorNodeSpec, output: string, color: string, paneIndex = 0, width: 1 | 2 = 1) => {
      const key = `${node.instance_id}:${output}`;
      let series = indicatorRegistry.current.get(key) as ISeriesApi<"Line"> | undefined;
      if (!series) {
        series = api.addSeries(LineSeries, { color, lineWidth: width, title: indicatorOutputLabel(node, output), priceLineVisible: false, lastValueVisible: true, ...(paneIndex === 0 || ["macd", "atr"].includes(node.indicator_id) ? { priceFormat: chartPriceFormat(bars.map(b => +b.low)) } : {}) }, paneIndex);
        indicatorRegistry.current.set(key,series); indicatorSeries.current.push(series);
      }
      series.setData(alignedIndicator(bars, indicatorPoints.filter(p => p.instance_id === node.instance_id && (barRevisions.get(epoch(p.open_time)) ?? p.bar_revision) === p.bar_revision), output).map(p => ({ ...p, time: p.time as UTCTimestamp })));
    };
    for (const node of indicatorNodes) {
      const preset = INDICATOR_PRESETS.find((item) => item.instance_id === node.instance_id);
      const color = preset?.color ?? "#4f9cff";
      if (node.indicator_id === "ema") addLine(node, "ema", color, 0, 2);
      else if (node.indicator_id === "sma") addLine(node, "sma", color, 0, 2);
      else if (node.indicator_id === "boll") {
        addLine(node, "upper", "#8b7cf6"); addLine(node, "middle", "#b6aafc"); addLine(node, "lower", "#8b7cf6");
      } else if (node.indicator_id === "macd") {
        const currentPane = pane++;
        addLine(node, "macd", "#38bdf8", currentPane, 2);
        addLine(node, "signal", "#fb923c", currentPane);
        const key = `${node.instance_id}:histogram`;
        let histogram = indicatorRegistry.current.get(key) as ISeriesApi<"Histogram"> | undefined;
        if (!histogram) {
          histogram = api.addSeries(HistogramSeries, { title: "MACD Hist", priceLineVisible: false, lastValueVisible: false, priceFormat: chartPriceFormat(bars.map(b => +b.low)) }, currentPane);
          indicatorRegistry.current.set(key,histogram); indicatorSeries.current.push(histogram);
        }
        histogram.setData(alignedIndicator(bars, indicatorPoints.filter(p => p.instance_id === node.instance_id && (barRevisions.get(epoch(p.open_time)) ?? p.bar_revision) === p.bar_revision), "histogram").map(p => "value" in p ? { ...p, time: p.time as UTCTimestamp, color: p.value! >= 0 ? "#22c99788" : "#f0526e88" } : { time: p.time as UTCTimestamp }));
      } else if (node.indicator_id === "adx") {
        const currentPane = pane++;
        addLine(node, "adx", color, currentPane, 2); addLine(node, "plus_di", "#22c997", currentPane); addLine(node, "minus_di", "#f0526e", currentPane);
      } else {
        const output = node.indicator_id === "rsi" ? "rsi" : node.indicator_id === "atr" ? "atr" : "obv";
        addLine(node, output, color, pane++, 2);
      }
    }
    const cgPanes=new Map<string,number>();
    for(const study of cgStudies){
      if(!cgPanes.has(study.paneId))cgPanes.set(study.paneId,pane++);
      const paneIndex=cgPanes.get(study.paneId)!;
      const priceFormat=study.unit==="USD"?{type:"volume" as const}:{type:"custom" as const,minMove:.000001,formatter:(value:number)=>coinglassNumber(value,study.unit)};
      const title=`${study.title}${study.status==="HISTORICAL"?" · 历史参考":study.status==="UNAVAILABLE"?" · 暂无数据":""}`;
      if(study.type==="histogram"){
        const key=`coinglass:${study.id}`;
        let series=indicatorRegistry.current.get(key) as ISeriesApi<"Histogram">|undefined;
        if(!series){series=api.addSeries(HistogramSeries,{title,color:study.color,priceFormat,priceLineVisible:false,lastValueVisible:false,base:study.baseline??0},paneIndex);indicatorRegistry.current.set(key,series);indicatorSeries.current.push(series);}
        series.setData(study.data.map(point=>point.value===undefined?{time:point.time as UTCTimestamp}:{time:point.time as UTCTimestamp,value:point.value,color:study.id==="funding"||study.id==="oi_change"?(point.value>=0?"#22c997aa":"#f0526eaa"):study.color}));
        series.applyOptions({title,priceFormat});
      }else{
        // Separate contiguous runs: no lines across missing CG periods or K-line gaps,
        // and no synthetic timestamps which would move the main chart's anchors.
        const segments=study.segments.length?study.segments:[[]];
        for(const [index,segment] of segments.entries()){
          const key=`coinglass:${study.id}:${index}`;
          let series=indicatorRegistry.current.get(key) as ISeriesApi<"Line">|undefined;
          if(!series){series=api.addSeries(LineSeries,{title:index===0?title:"",color:study.color,lineWidth:2,pointMarkersVisible:segment.length===1,priceFormat,priceLineVisible:false,lastValueVisible:false,
              autoscaleInfoProvider:(original:()=>AutoscaleInfo|null)=>{const info=original();return info?.priceRange&&study.baseline!==undefined?{...info,priceRange:{minValue:Math.min(info.priceRange.minValue,study.baseline),maxValue:Math.max(info.priceRange.maxValue,study.baseline)}}:info;}},paneIndex);indicatorRegistry.current.set(key,series);indicatorSeries.current.push(series);
            if(index===0&&study.baseline!==undefined)series.createPriceLine({price:study.baseline,color:"#78869d",lineWidth:1,lineStyle:2,axisLabelVisible:false,title:"参考线"});}
          series.setData(segment.map(point=>({time:point.time as UTCTimestamp,value:point.value!})));
          series.applyOptions({title:index===0?title:"",priceFormat,pointMarkersVisible:segment.length===1});
        }
      }
    }
    if(host.current){
      host.current.style.height=`${Math.max(480,360+(pane-1)*140)}px`;
      // Proportions survive the asynchronous container resize; fixed pixel
      // sizing against the old height could collapse every secondary pane.
      // Preserve user-dragged pane proportions until the study schema changes.
      if(schemaChanged)api.panes().forEach((item,index)=>item.setStretchFactor(index===0?360:140));
    }
  }, [indicatorTimeSignature, effectiveChartKey, indicatorNodes, indicatorPoints, cgRenderSignature]);
  useEffect(()=>{
    const api=chart.current,candles=candleSeries.current;
    if(!api||!candles||!coinglassScenario)return;
    const step:Record<string,number>={"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"8h":28800,"1d":86400};
    const detach=attachCoinglassScenario(api,candles,bars,coinglassScenario,step[timeframe]??0);
    api.timeScale().scrollToRealTime();
    return()=>{if(candleSeries.current===candles)detach();};
  },[coinglassScenario,effectiveChartKey,indicatorTimeSignature]);
  const overlaySignature = patternOverlays.map(e => e.evidence_id).join(":");
  const firstOverlayBar = bars[0]?.open_time, lastOverlayBar = bars.at(-1)?.open_time;
  useEffect(() => {
    const api = chart.current;
    if (!api) return;
    const detach = attachPatternOverlays(api, patternOverlays, bars);
    const focusKey = `${chartKey}:${overlaySignature}`;
    if (patternFocus.current !== focusKey) {
      const last = patternOverlays.at(-1), geometry = last ? overlayGeometry(last, bars) : undefined;
      if (geometry?.points.length) {
        const times = geometry.points.map(p => p.time), from = bars.findIndex(b => epoch(b.open_time) === Math.min(...times)), to = bars.findIndex(b => epoch(b.open_time) === Math.max(...times));
        if (from >= 0 && to >= from) api.timeScale().setVisibleLogicalRange({ from: from - 8, to: to + 8 });
      }
      patternFocus.current = focusKey;
    }
    return () => { if (chart.current === api) detach(); };
  }, [chartKey, overlaySignature, firstOverlayBar, lastOverlayBar, indicatorTimeSignature]);
  const currentRevisions = new Map(bars.map(b => [epoch(b.open_time), b.revision]));
  const indicatorSummary = indicatorNodes.map((node) => {
    const preset = INDICATOR_PRESETS.find((item) => item.instance_id === node.instance_id);
    const latest = indicatorPoints.filter(item => item.instance_id === node.instance_id && item.ready && currentRevisions.has(epoch(item.open_time)) && (currentRevisions.get(epoch(item.open_time)) ?? item.bar_revision) === item.bar_revision).sort((a, b) => epoch(a.open_time) - epoch(b.open_time)).at(-1);
    const firstValue = latest ? Object.values(latest.values).find((value) => typeof value === "number") : null;
    return { id: node.instance_id, label: indicatorNodeLabel(node), color: preset?.color ?? "#4f9cff", value: firstValue };
  });
  function contextMenu(e: React.MouseEvent) {
    e.preventDefault();
    const box = host.current!.getBoundingClientRect(), x = e.clientX - box.left, y = e.clientY - box.top;
    const api = chart.current, candles = candleSeries.current;
    const onPricePane = api && y >= 0 && y < api.panes()[0].getHeight() && x >= 0 && x < box.width - api.priceScale("right").width();
    const price = onPricePane ? candles?.coordinateToPrice(y) : null, time = api?.timeScale().coordinateToTime(x);
    setMenu({ x: e.clientX, y: e.clientY, price: price == null ? undefined : Number(price), bar: typeof time === "number" ? bars.find(b => epoch(b.open_time) === time) : undefined });
  }
  async function copyValue(text: string) {
    try { await navigator.clipboard.writeText(text); inform("已复制"); } catch { inform("剪贴板不可用，请使用图表读数手动复制"); }
  }
  async function screenshot() {
    if(exportBusy.current)return;exportBusy.current=true;setExporting(true);
    try {
      const canvas=await captureChartScreenshot(()=>({...screenshotState.current,chart:chart.current,controller:volumeProfile.current}));
      const blob=await new Promise<Blob|null>(resolve=>canvas.toBlob(resolve));
      if(!blob)throw new Error("图表截图失败");
      const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=`chart-${timeframe||"snapshot"}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){inform(error instanceof Error?error.message:"图表截图失败");}
    finally{exportBusy.current=false;setExporting(false);}
  }
  const menuActions = [
    { label: "重置缩放 / 显示全部", run: () => chart.current?.timeScale().fitContent() },
    { label: "滚动到最新 K 线", run: () => chart.current?.timeScale().scrollToRealTime() },
    ...(onIndicators ? [{ label: "添加 / 设置指标", run: onIndicators }] : []),
    ...(onStudies ? [{ label: "精选指标 / VPVR", run: onStudies }] : []),
    ...(onStatistics ? [{ label: "历史胜率与概率", run: onStatistics }] : []),
    ...(onWatch ? [{ label: "加入我的自选", run: onWatch }] : []),
    { label: logarithmic ? "切换线性价格轴" : "切换对数价格轴", run: () => setLogarithmic(v => !v) },
    { label: magnet ? "关闭十字线吸附" : "开启十字线吸附", run: () => setMagnet(v => !v) },
    { label: volumeVisible ? "隐藏成交量" : "显示成交量", run: () => setVolumeVisible(v => !v) },
    { label: `添加水平线${menu?.price === undefined ? "（请右键主图）" : " · " + formatMarketPrice(menu.price)}`, disabled: menu?.price === undefined || drawn.length >= 8, run: () => { if (menu?.price !== undefined) setDrawn(old => [...old, menu.price!]); } },
    { label: "清除我的水平线", disabled: !drawn.length, run: () => setDrawn([]) },
    { label: "复制所选 K 线 OHLC", disabled: !menu?.bar, run: () => { if (menu?.bar) void copyValue(`${menu.bar.open_time} O ${menu.bar.open} H ${menu.bar.high} L ${menu.bar.low} C ${menu.bar.close} V ${menu.bar.volume}`); } },
    { label: exporting ? "图表导出中…" : "下载图表 PNG", disabled: !bars.length || exporting, run: () => { void screenshot(); } },
  ];
  return <section className={`chart-panel panel${cgStudies.length||vpvr?.enabled?" has-chart-studies":""}`}>
    <div className="chart-interactions"><span>{bars.at(-1)?.is_final === false ? "末根未收盘 · 指标仅按收盘计算" : "已收盘 K 线"} · {drawn.length ? `${drawn.length} 条价格标记` : "右键打开工具"}</span><button aria-haspopup="menu" aria-expanded={!!menu} aria-label="更多图表工具" onClick={e => { const box = e.currentTarget.getBoundingClientRect(); setMenu({ x: box.right - 260, y: box.bottom + 4, price: bars.length ? Number(bars.at(-1)!.close) : undefined, bar: bars.at(-1) }); }}>更多 ⋯</button></div>
    <div className="chart-indicator-legend">{indicatorSummary.map((item) => <span key={item.id}><i style={{ background: item.color }} />{item.label}<b>{typeof item.value === "number" ? formatIndicatorValue(item.value) : "--"}</b></span>)}</div>
    <ChartStudyReadouts studies={cgStudies} profileEnabled={vpvr?.enabled} fineProfile={fineProfile} onManage={onStudies} />
    {!!patternOverlays.length && <div className="chart-pattern-legend" aria-label="图表原理线">{patternOverlays.map((e, i) => <span key={e.id} style={{ color: overlayColors[i] }} data-line-count={overlayGeometry(e, bars).lines.length}>{e.name} · {overlayGeometry(e, bars).status==="CLIPPED"?"局部图 · ":""}{overlayGeometry(e, bars).status==="REVISED_CANDLE"?"K 线已修订，请查看原始档案":`${overlayGeometry(e, bars).lines.length} 条原理线`}</span>)}</div>}
    <div ref={host} className="chart-host" onContextMenu={contextMenu} />
    {!bars.length && <div className="chart-empty">{loading ? `正在同步 ${timeframe} 已终结 K 线…` : "暂无已终结 K 线"}</div>}
    {feedback && <button className="chart-feedback" role="status" onClick={() => setFeedback("")}>{feedback}</button>}
    {menu && <ChartContextMenu x={menu.x} y={menu.y} actions={menuActions} onClose={closeMenu} />}
  </section>;
}

type StrategyEvent = {
  id: string;
  rule_name: string;
  symbol: string;
  timeframe: string;
  available_at: string;
  payload: { matched?: Array<{ pattern_id?: string; confidence?: number }> };
  payload_hash: string;
};

function AlertsWorkspace({ notify, session }: { notify: (value: string) => void; session: Session }) {
  const action = useAction(notify), confirmation = useConfirm();
  const [editing, setEditing] = useState<AlertRule>();
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [instrument, setInstrument] = useState("");
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [patterns, setPatterns] = useState<PatternCatalogItem[]>([]);
  const [events, setEvents] = useState<StrategyEvent[]>([]);
  const [selectedPatterns, setSelectedPatterns] = useState<string[]>([...STRATEGY_PRESETS[0].patterns]);
  const [activePreset, setActivePreset] = useState<string>(STRATEGY_PRESETS[0].id);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const [success, setSuccess] = useState("");
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const load = useCallback(async () => {
      const results = await Promise.allSettled([
        api<Page<AlertRule>>("/api/v2/alert-rules"),
        api<Page<NotificationChannel>>("/api/v2/notification-channels"),
        api<{ items: PatternCatalogItem[] }>("/api/v2/patterns/catalog"),
        api<Page<StrategyEvent>>("/api/v2/strategy-events?limit=30"),
      ]);
      const [rulePage, channelPage, patternPage, eventPage] = results;
      if (rulePage.status === "fulfilled") { setRules(rulePage.value.items); setListError(""); }
      else setListError(errorMessage(rulePage.reason));
      if (channelPage.status === "fulfilled") setChannels(channelPage.value.items.filter(c => c.status === "ACTIVE"));
      if (patternPage.status === "fulfilled") setPatterns(patternPage.value.items);
      if (eventPage.status === "fulfilled") setEvents(eventPage.value.items);
      setListLoading(false);
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setFormError(""); setSuccess("");
    if (!instrument) { setFormError("请搜索并选择交易标的"); return; }
    if (!selectedPatterns.length) { setFormError("至少选择一个形态"); return; }
    const form = event.currentTarget;
    const data = new FormData(form);
    if (!String(data.get("name") ?? "").trim()) { setFormError("请填写策略名称"); return; }
    setBusy(true);
    try {
      const created = await api<AlertRule>("/api/v2/alert-rules", { method: "POST", body: jsonBody({
        name: String(data.get("name")).trim(),
        condition: { schema_version: 1, instrument_id: instrument, timeframe: data.get("timeframe"), pattern_ids: selectedPatterns, match_mode: data.get("match_mode"), minimum_confidence: Number(data.get("confidence")), confirmed_only: data.get("phase") !== "formed", coincidence_bars: Number(data.get("coincidence")), cooldown_seconds: Number(data.get("cooldown")) * 60 },
        routing: { schema_version: 1, channel_ids: data.get("channel") ? [data.get("channel")] : [] },
      }) });
      setRules(items => [created, ...items.filter(item => item.id !== created.id)]);
      setSuccess(`「${created.name}」已创建并启用，可在已保存策略中查看。`);
      const nameInput = form.elements.namedItem("name") as HTMLInputElement | null;
      if (nameInput) nameInput.value = "";
      void load();
    } catch (error) { setFormError(errorMessage(error)); }
    finally { setBusy(false); }
  }
  async function pause(rule: AlertRule) {
    try { await api(`/api/v2/alert-rules/${rule.id}/${rule.status === "ACTIVE" ? "pause" : "resume"}`, { method: "POST", headers: { "If-Match": `"${rule.etag}"` } }); await load(); }
    catch (error) { notify(errorMessage(error)); }
  }
  async function remove(rule: AlertRule) {
    if (!await confirmation.ask("删除策略", `删除「${rule.name}」并停止今后触发？历史版本、信号及交易证据保留。`)) return;
    await action.run("删除中…", async () => {
      await api(`/api/v2/alert-rules/${rule.id}`, { method: "DELETE", headers: { "If-Match": `"${rule.etag}"` } });
      setRules(items => items.filter(item => item.id !== rule.id)); notify("策略已删除，历史记录保留");
    });
  }
  function choosePreset(preset: typeof STRATEGY_PRESETS[number]) {
    setActivePreset(preset.id); setSelectedPatterns([...preset.patterns]);
  }
  function togglePattern(patternId: string) {
    setActivePreset("");
    setSelectedPatterns((current) => current.includes(patternId)
      ? current.filter((item) => item !== patternId)
      : [...current, patternId]);
  }
  const patternName = (patternId: string) => patterns.find((item) => item.pattern_id === patternId)?.name ?? patternId;
  return (
    <main className="settings-page strategy-page">
      <section className="page-title"><div><h1>策略与概率告警</h1><p>终结 K 线 · 结构确认 · 可追溯通知</p></div><span className="status active">策略引擎</span></section>
      <div className="strategy-layout">
      <section className="panel strategy-builder"><h2>新建策略</h2>
        <div className="strategy-presets">{STRATEGY_PRESETS.map((preset) => <button type="button" className={activePreset === preset.id ? "selected" : ""} onClick={() => choosePreset(preset)} key={preset.id}><strong>{preset.name}</strong><small>{preset.patterns.length} 个结构</small></button>)}</div>
        <form onSubmit={create} className="strategy-form" aria-busy={busy}>
          <label className="strategy-name">策略名称<input name="name" required maxLength={120} placeholder="例如 BTC 15m 多头反转" /></label>
          <InstrumentPicker value={instrument} onChange={item => setInstrument(item.id)} />
          <label>周期<select name="timeframe" defaultValue="15m"><option>1m</option><option>5m</option><option>15m</option><option>30m</option><option>1h</option><option>4h</option><option>8h</option><option>1d</option></select></label>
          <label>组合<select name="match_mode" defaultValue="ANY"><option value="ANY">任一形态</option><option value="ALL">全部同时</option></select></label>
          <label>通知时机<select name="phase" defaultValue="confirmed"><option value="confirmed">突破确认后 · 做单观察</option><option value="formed">已形成预告 + 突破确认</option></select></label>
          <label>形态评分阈值<input name="confidence" type="number" min="0" max="1" step="0.01" defaultValue="0.70" required /></label>
          <label>确认窗口（K 线）<input name="coincidence" type="number" min="1" max="100" defaultValue="3" required /></label>
          <label>冷却（分钟）<input name="cooldown" type="number" min="0" max="10080" defaultValue="15" required /></label>
          <label className="strategy-channel">Telegram 通道<select name="channel" defaultValue=""><option value="">仅站内记录</option>{channels.map((item) => <option key={item.id} value={item.id}>{item.alias}</option>)}</select></label>
          <details className="pattern-options"><summary>形态条件 · 已选 {selectedPatterns.length} 个 · 展开自定义</summary><fieldset className="pattern-picker"><legend>选择形态</legend>{Array.from(new Set(patterns.filter((item) => !item.experimental).map((item) => item.group))).map((group) => <div key={group}><small>{group}</small><div>{patterns.filter((item) => item.group === group && !item.experimental).map((item) => <button type="button" className={`${selectedPatterns.includes(item.pattern_id) ? "selected" : ""} ${item.direction}`} onClick={() => togglePattern(item.pattern_id)} key={item.pattern_id}>{item.name}</button>)}</div></div>)}</fieldset></details>
          {formError && <div className="error-banner strategy-submit" role="alert">{formError}</div>}
          {success && <div className="research-feedback strategy-submit" role="status">{success}</div>}
          <div className="strategy-submit"><span>{channels.length ? "可直接推送 Telegram" : "仅站内记录 · Telegram 可稍后配置"}</span><button className="primary" disabled={busy || !selectedPatterns.length || session.user.role === "VIEWER"}>{busy ? "启用中…" : session.user.role === "VIEWER" ? "观察者无创建权限" : "启用策略"}</button></div>
        </form>
      </section>
      <section className="panel strategy-list"><header><div><h2>已保存策略</h2><small>最近 100 条 · {rules.filter((item) => item.status === "ACTIVE").length} 个运行中</small></div><button onClick={() => void load()}>刷新</button></header>{listError && <div className="error-banner" role="alert">记录加载失败：{listError}</div>}{rules.length ? <PagedList items={rules} label="告警策略" size={6} render={(rule) => <article className="strategy-card" key={rule.id}><header><div><span className={`status ${rule.status.toLowerCase()}`}>{rule.status === "ACTIVE" ? "运行中" : "已暂停"}</span><h3>{rule.name}</h3><small>{rule.symbol ?? ""}</small></div><b>{rule.version.condition.timeframe}</b></header><div className="strategy-patterns">{rule.version.condition.pattern_ids.map((item) => <span key={item}>{patternName(item)}</span>)}</div><footer><small>形态评分 ≥ {Math.round(rule.version.condition.minimum_confidence * 100)}% · v{rule.current_version}{rule.version.routing.channel_ids.length ? " · Telegram" : " · 站内"}</small>{session.user.role !== "VIEWER" && <div className="button-row"><button disabled={!!action.busy} onClick={() => setEditing(rule)}>编辑</button><button disabled={!!action.busy} onClick={() => void action.run("更新策略中…", () => pause(rule))}>{rule.status === "ACTIVE" ? "暂停" : "恢复运行"}</button><button className="danger" disabled={!!action.busy} onClick={() => void remove(rule)}>删除</button></div>}</footer></article>} /> : <Empty label={listLoading ? "正在加载策略…" : listError ? "请刷新重试" : "还没有策略"} />}
        <div className="strategy-events"><h3>最近信号</h3>{events.length ? <PagedList items={events} label="最近信号" size={6} render={(item) => <article key={item.id}><i /><div><strong>{item.symbol} · {item.timeframe}</strong><small>{item.rule_name} · {item.payload.matched?.map((match) => patternName(match.pattern_id ?? "")).join(" / ")}</small></div><time>{new Date(item.available_at).toLocaleString()}</time></article>} /> : <small className="strategy-events-empty">暂无触发</small>}</div>
      </section>
      </div>
      {editing && <AlertRuleEditor rule={editing} channels={channels} patterns={patterns} onClose={() => setEditing(undefined)} onSaved={updated => { setRules(items => items.map(item => item.id === updated.id ? updated : item)); setEditing(undefined); notify(`策略已更新为 v${updated.current_version}`); }} />}{confirmation.dialog}
    </main>
  );
}

function TradingWorkspace({ session, notify }: { session: Session; notify: (value: string) => void }) {
  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [signals, setSignals] = useState<SignalEvidence[]>([]);
  const [proposals, setProposals] = useState<PaperProposal[]>([]);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [selectedTrade, setSelectedTrade] = useState<PaperTrade>();
  const [chart, setChart] = useState<PaperChart>();
  const [timeline, setTimeline] = useState<Array<{ sequence: number; event_type: string; occurred_at: string }>>([]);
  const action = useAction(notify), confirmation = useConfirm();
  const [loading, setLoading] = useState(true), [loadError, setLoadError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false), [detailError, setDetailError] = useState("");
  const [accountId, setAccountId] = useState(""), [accountOpen, setAccountOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false), [detailRefresh, setDetailRefresh] = useState(0);
  const canPropose = ["RESEARCHER", "OPERATOR", "OWNER"].includes(session.user.role);
  const canApprove = ["OPERATOR", "OWNER"].includes(session.user.role);
  const load = useCallback(async () => {
    setLoading(true); setLoadError("");
    try {
      const [accountPage, signalPage, proposalPage, tradePage] = await Promise.all([
        api<Page<PaperAccount>>("/api/v2/paper-accounts"),
        api<Page<SignalEvidence>>("/api/v2/signals?limit=50"),
        api<Page<PaperProposal>>("/api/v2/proposals?limit=50"),
        api<Page<PaperTrade>>("/api/v2/trades?limit=50"),
      ]);
      setAccounts(accountPage.items);
      setAccountId(current => accountPage.items.some(a => a.id === current && a.status === "ACTIVE") ? current : accountPage.items.find(a => a.status === "ACTIVE")?.id ?? "");
      setSignals(signalPage.items);
      setProposals(proposalPage.items);
      setTrades(tradePage.items);
      setSelectedTrade((current) => tradePage.items.find((item) => item.public_trade_id === current?.public_trade_id) ?? tradePage.items[0]);
    } catch (error) { setLoadError(errorMessage(error)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => void load(), [load]);
  useEffect(() => {
    setChart(undefined); setTimeline([]); setDetailError("");
    if (!selectedTrade || !detailOpen) return;
    const controller = new AbortController(); setDetailLoading(true);
    void Promise.all([
      api<PaperChart>(`/api/v2/trades/${selectedTrade.public_trade_id}/chart`, { signal: controller.signal }),
      api<{ items: Array<{ sequence: number; event_type: string; occurred_at: string }> }>(`/api/v2/trades/${selectedTrade.public_trade_id}/timeline`, { signal: controller.signal }),
    ]).then(([chartResult, timelineResult]) => { if (!controller.signal.aborted) { setChart(chartResult); setTimeline(timelineResult.items); } })
      .catch(error => { if (!controller.signal.aborted) setDetailError(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selectedTrade, detailOpen, detailRefresh]);
  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api("/api/v2/paper-accounts", { method: "POST", body: jsonBody({
        name: data.get("name"), base_currency: "USDT", initial_equity: data.get("equity"),
        risk_fraction: "0.0025", max_notional_fraction: "0.10", fee_bps_per_side: "5",
        market_slippage_bps: "3", max_entry_drift_bps: "50",
      }) });
      form.reset(); setAccountOpen(false); await load(); notify("PAPER 模拟账户已创建；它不包含交易所凭据");
    } catch (error) { notify(errorMessage(error)); }
  }
  async function propose(signal: SignalEvidence) {
    const account = accounts.find((item) => item.id === accountId && item.status === "ACTIVE");
    if (!account) { notify("请先创建有效的 PAPER 模拟账户"); return; }
    try {
      const decision = await api<ExecutionDecision>(`/api/v2/signals/${signal.id}/execution-decisions`, {
        method: "POST", body: jsonBody({ paper_account_id: account.id, client_request_id: crypto.randomUUID() }),
      });
      if (decision.decision !== "ELIGIBLE") {
        const failed = decision.runtime_gates.filter((item) => item.result !== "PASS").map((item) => item.gate_code).join("、");
        notify(`服务端决定 ABSTAIN：${failed || "门禁未通过"}`); return;
      }
      await api("/api/v2/proposals", {
        method: "POST", body: jsonBody({ execution_decision_id: decision.id, client_request_id: crypto.randomUUID() }),
      });
      await load(); notify("PAPER 提案已冻结；确认时仍会重新检查价格与风险");
    } catch (error) { notify(errorMessage(error)); }
  }
  async function approve(proposal: PaperProposal) {
    if (!await confirmation.ask("确认 PAPER 模拟交易", `${proposal.symbol} · ${proposal.exact_terms.side} · 数量 ${proposal.exact_terms.quantity}，止盈 ${proposal.exact_terms.take_profit_price}，止损 ${proposal.exact_terms.stop_price}。只使用模拟资金。`)) return;
    try {
      const result = await api<{ outcome: string; failed_gates: string[]; trade: PaperTrade | null }>(`/api/v2/proposals/${proposal.id}/approve`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: jsonBody({ proposal_version: proposal.version }),
      });
      await load();
      notify(result.trade ? `PAPER 交易已创建：${result.trade.public_trade_id}` : `确认被安全拒绝：${result.failed_gates.join("、")}`);
    } catch (error) { notify(errorMessage(error)); }
  }
  async function cancel(proposal: PaperProposal) {
    if (!await confirmation.ask("取消提案", `取消 ${proposal.symbol} 的待确认提案？`)) return;
    try {
      await api(`/api/v2/proposals/${proposal.id}/cancel`, {
        method: "POST", body: jsonBody({ proposal_version: proposal.version, client_request_id: crypto.randomUUID() }),
      });
      await load(); notify("提案已取消，未创建交易");
    } catch (error) { notify(errorMessage(error)); }
  }
  async function refreshTrade(trade: PaperTrade) {
    try {
      const updated = await api<PaperTrade>(`/api/v2/trades/${trade.public_trade_id}:reconcile-paper`, { method: "POST" });
      await load(); setSelectedTrade(updated); notify("已按最新终结 K 线推进 PAPER 状态");
    } catch (error) { notify(errorMessage(error)); }
  }
  const combinedBars = chart ? Array.from(
    new Map([...chart.progress_bars, ...chart.original_bars].map((item) => [item.open_time, item])).values(),
  ).sort((a, b) => new Date(a.open_time).getTime() - new Date(b.open_time).getTime()) : [];
  return (
    <main className="settings-page trade-page">
      <section className="page-title"><div><h1>PAPER 交易中心</h1></div><div className="button-row"><span className="paper-lock">PAPER ONLY</span><button disabled={loading || !!action.busy} onClick={() => void load()}>刷新</button></div></section><PageState loading={loading} error={loadError} onRetry={() => void load()} />
      <PaperOrdersWorkspace accounts={accounts} accountId={accountId} onAccountChange={setAccountId} canTrade={canApprove} userId={session.user.id} notify={notify} />
      <div className="trade-dashboard">
        <section className="panel trade-column"><header><h2>模拟账户</h2>{canPropose && <button disabled={!!action.busy} onClick={() => setAccountOpen(true)}>新增账户</button>}</header><label>提案使用账户<select value={accountId} onChange={e => setAccountId(e.target.value)}><option value="">请选择账户</option>{accounts.filter(a => a.status === "ACTIVE").map(a => <option value={a.id} key={a.id}>{a.name}</option>)}</select></label>{accounts.length ? <PagedList items={accounts} label="模拟账户" size={5} render={(account) => <article className="paper-account" key={account.id}><div><strong>{account.name}</strong><span className={`status ${account.status.toLowerCase()}`}>{account.status}</span></div><b>{account.equity} {account.base_currency}</b><small>已实现 {account.realized_pnl} · 单笔风险 {(Number(account.risk_fraction) * 100).toFixed(2)}% · 名义上限 {(Number(account.max_notional_fraction) * 100).toFixed(0)}%</small></article>} /> : canPropose ? <form className="stack-form" onSubmit={event => { event.preventDefault(); void action.run("创建账户中…", () => createAccount(event)); }}><label>账户名称<input name="name" required defaultValue="默认 PAPER" /></label><label>初始模拟净值<input name="equity" type="number" min="100" max="1000000000" step="0.01" defaultValue="10000" /></label><button className="primary" disabled={!!action.busy}>{action.busy || "创建保守模拟账户"}</button></form> : <Empty label="尚无模拟账户" />}</section>
        <section className="panel trade-column signal-column"><h2>冻结信号证据</h2><small>最近 50 条</small>{signals.length ? <PagedList items={signals} label="冻结信号" size={5} render={(signal) => <article className="signal-card" key={signal.id}><header><div><strong>{signal.symbol}</strong><span>{signal.timeframe} · {signal.side}</span></div><span className={`tier ${signal.evidence_tier.toLowerCase()}`}>{signal.evidence_tier}</span></header><div className="terms-row"><span>参考 {signal.entry_reference}</span><span>TP {signal.take_profit_price}</span><span>SL {signal.stop_price}</span></div><small>胜率 LCB {percent(signal.metrics.win_probability_lcb)} · EV LCB {percent(signal.metrics.expected_net_return_lcb)} · {signal.metrics.independent_blocks ?? 0} 独立块</small><footer><code>{signal.evidence_digest.slice(0, 12)}…</code>{canPropose && <button className="primary" disabled={!!action.busy || !accountId || signal.eligibility_status !== "PAPER_ELIGIBLE"} onClick={() => void action.run("生成提案中…", () => propose(signal))}>{signal.eligibility_status === "PAPER_ELIGIBLE" ? "生成精确提案" : "ABSTAIN"}</button>}</footer></article>} /> : <Empty label="尚无策略 worker 写入的冻结证据；普通告警不会冒充盈利概率" />}</section>
        <section className="panel trade-column"><h2>提案记录</h2><small>最近 50 条</small>{proposals.length ? <PagedList items={proposals} label="提案" size={5} render={(proposal) => <article className="proposal-card" key={proposal.id}><header><div><strong>{proposal.symbol}</strong><span>{proposal.timeframe} · {proposal.exact_terms.side}</span></div><span className={`status ${proposal.status.toLowerCase()}`}>{proposal.status}</span></header><div className="terms-grid"><span>数量<b>{proposal.exact_terms.quantity}</b></span><span>风险预算<b>{proposal.exact_terms.risk_budget}</b></span><span>止盈<b>{proposal.exact_terms.take_profit_price}</b></span><span>止损<b>{proposal.exact_terms.stop_price}</b></span></div><small>到期 {new Date(proposal.expires_at).toLocaleString()} · 同 K 线 {proposal.exact_terms.same_bar_policy}</small>{proposal.status === "AWAITING_CONFIRMATION" && <footer>{canPropose && <button disabled={!!action.busy} onClick={() => void action.run("取消中…", () => cancel(proposal))}>取消</button>}{canApprove && <button className="primary" disabled={!!action.busy} onClick={() => void action.run("确认中…", () => approve(proposal))}>确认冻结条款</button>}</footer>}</article>} /> : <Empty label="尚无提案" />}</section>
      </div>
      <section className="trade-ledger"><div className="panel trade-list"><h2>交易事件账本</h2><small>最近 50 条 · 点击查看不可变证据与后续走势</small>{trades.length ? <PagedList items={trades} label="交易账本" render={(trade) => <button className={selectedTrade?.public_trade_id === trade.public_trade_id ? "selected" : ""} onClick={() => { setSelectedTrade(trade); setDetailOpen(true); }} key={trade.public_trade_id}><div><strong>{trade.symbol} · {trade.side}</strong><code>{trade.public_trade_id}</code></div><span className={`status ${trade.state.toLowerCase()}`}>{trade.state}</span><b className={Number(trade.net_pnl ?? 0) >= 0 ? "positive" : "negative"}>{trade.net_pnl ?? "进行中"}</b></button>} /> : <Empty label="确认合格提案后会创建 PAPER 交易" />}</div></section>{detailOpen && selectedTrade && <Dialog title="交易事件详情" onClose={() => setDetailOpen(false)} busy={!!action.busy}><div className="trade-detail"><header><div><small>{selectedTrade.public_trade_id}</small><h2>{selectedTrade.symbol} · {selectedTrade.side}</h2></div>{canApprove && selectedTrade.state === "OPEN" && <button disabled={!!action.busy} onClick={() => void action.run("更新中…", () => refreshTrade(selectedTrade))}>刷新终结 K 线</button>}</header><div className="trade-metrics"><span>状态<b>{selectedTrade.state} / {selectedTrade.close_reason ?? "等待障碍"}</b></span><span>入场<b>{selectedTrade.entry_fill_price}</b></span><span>净 PnL<b>{selectedTrade.net_pnl ?? "--"}</b></span><span>观察水位<b>{new Date(selectedTrade.last_evaluated_available_at).toLocaleString()}</b></span></div><PageState loading={detailLoading} error={detailError} onRetry={() => setDetailRefresh(x => x + 1)} /><div className="trade-chart"><MarketChart bars={combinedBars} loading={detailLoading} chartKey={selectedTrade.public_trade_id} levels={chart ? [{ title: "入场", price: chart.overlays.entry_fill, color: "#6ca0ff" }, { title: "止盈", price: chart.overlays.take_profit, color: "#22c997" }, { title: "止损", price: chart.overlays.stop, color: "#f0526e" }] : []} /></div><div className="overlay-legend"><span>Entry {chart?.overlays.entry_fill}</span><span>TP {chart?.overlays.take_profit}</span><span>SL {chart?.overlays.stop}</span><strong>原始证据边界 {chart ? new Date(chart.evidence_boundary).toLocaleString() : "--"}</strong></div><div className="timeline">{timeline.map((event) => <div key={event.sequence}><b>{event.sequence}</b><span>{event.event_type}</span><time>{new Date(event.occurred_at).toLocaleString()}</time></div>)}</div></div></Dialog>}
      {accountOpen && <Dialog title="新增 PAPER 账户" busy={!!action.busy} onClose={() => setAccountOpen(false)}><form className="stack-form" onSubmit={event => { event.preventDefault(); void action.run("创建账户中…", () => createAccount(event)); }}><label>账户名称<input name="name" required maxLength={80} /></label><label>初始模拟净值<input name="equity" type="number" min="100" max="1000000000" step="0.01" defaultValue="10000" required /></label><button className="primary" disabled={!!action.busy}>{action.busy || "创建模拟账户"}</button></form></Dialog>}{confirmation.dialog}
      {action.busy && <div role="status" className="action-progress">{action.busy}</div>}
    </main>
  );
}

function ExchangeWorkspace({ session, notify }: { session: Session; notify: (value: string) => void }) {
  const confirmation = useConfirm();
  const busyLock = useRef(false);
  const [loading, setLoading] = useState(true), [loadError, setLoadError] = useState("");
  const [accounts, setAccounts] = useState<ExchangeAccount[]>([]);
  const [paperTrades, setPaperTrades] = useState<PaperTrade[]>([]);
  const [signals, setSignals] = useState<SignalEvidence[]>([]);
  const [certifications, setCertifications] = useState<StrategyExecutionCertification[]>([]);
  const [drills, setDrills] = useState<TestnetDrill[]>([]);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [rotating, setRotating] = useState<string | null>(null);
  const [arming, setArming] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setLoadError("");
    try {
      const [accountPage, tradePage, signalPage, certificationPage, drillPage] = await Promise.all([
        api<{ items: ExchangeAccount[]; credential_admission_configured: boolean }>("/api/v2/exchange-accounts"),
        api<Page<PaperTrade>>("/api/v2/trades?limit=100"),
        api<Page<SignalEvidence>>("/api/v2/signals?limit=100"),
        api<{ items: StrategyExecutionCertification[] }>("/api/v2/execution-certifications"),
        api<{ items: TestnetDrill[] }>("/api/v2/testnet-drills?limit=100"),
      ]);
      setAccounts(accountPage.items);
      setConfigured(accountPage.credential_admission_configured);
      setPaperTrades(tradePage.items);
      setSignals(signalPage.items);
      setCertifications(certificationPage.items);
      setDrills(drillPage.items);
    } catch (error) { setLoadError(errorMessage(error)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => void load(), [load]);
  useEffect(() => {
    if (!accounts.some((account) => account.status === "PENDING_VALIDATION" || account.status === "TESTNET_BUSY")
      && !drills.some((drill) => drillNeedsRefresh(drill.state))) return;
    const timer = window.setTimeout(() => void load(), 2500);
    return () => window.clearTimeout(timer);
  }, [accounts, drills, load]);

  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      const encryptedSubmission = await encryptSubmission(`tenant:${session.user.tenant_id}:exchange_account_create`, {
        alias: data.get("alias"),
        venue: "GATE",
        environment: "TESTNET",
        market_type: "FUTURES_USDT",
        api_key: data.get("api_key"),
        api_secret: data.get("api_secret"),
        acknowledge_no_withdrawal: data.get("acknowledge") === "on",
      });
      await api("/api/v2/exchange-accounts", {
        method: "POST",
        body: jsonBody(wrapEncryptedSubmission(encryptedSubmission)),
      });
      form.reset();
      await load();
      notify("凭据已加密提交；正在由隔离进程执行只读连接测试");
    } catch (error) { notify(errorMessage(error)); }
    finally { clearSensitiveFields(form); busyLock.current = false; setBusy(false); }
  }

  async function rotateCredential(account: ExchangeAccount, event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (managedAccounts.has(account.id)) { notify("账户仍有执行任务或正在对账，暂不能轮换凭据"); return; }
    const form = event.currentTarget;
    const data = new FormData(form);
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      const encryptedSubmission = await encryptSubmission(`tenant:${session.user.tenant_id}:exchange_credential_rotate:${account.id}`, {
        api_key: data.get("api_key"),
        api_secret: data.get("api_secret"),
        acknowledge_no_withdrawal: data.get("acknowledge") === "on",
      });
      await api(`/api/v2/exchange-accounts/${account.id}/credentials:rotate`, {
        method: "POST",
        headers: { "If-Match": `"${account.etag}"` },
        body: jsonBody(wrapEncryptedSubmission(encryptedSubmission)),
      });
      form.reset();
      setRotating(null);
      await load();
      notify("新凭据版本已加密；旧版本将在新版本验证成功后被替换");
    } catch (error) { notify(errorMessage(error)); }
    finally { clearSensitiveFields(form); busyLock.current = false; setBusy(false); }
  }

  async function validate(account: ExchangeAccount) {
    if (managedAccounts.has(account.id)) { notify("账户仍有执行任务或正在对账，暂不能重置验证"); return; }
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      await api(`/api/v2/exchange-accounts/${account.id}/validate`, {
        method: "POST", headers: { "If-Match": `"${account.etag}"` },
      });
      await load();
      notify("已排队重新测试连接");
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setBusy(false); }
  }

  async function disable(account: ExchangeAccount) {
    if (managedAccounts.has(account.id)) { notify("账户仍有执行任务或正在对账，暂不能停用"); return; }
    if (!await confirmation.ask("停用交易所账户", `停用 ${account.alias}？本操作不会替你在 Gate 控制台撤销 API Key。`)) return;
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      await api(`/api/v2/exchange-accounts/${account.id}`, {
        method: "DELETE", headers: { "If-Match": `"${account.etag}"` },
      });
      await load();
      notify("账户已在本系统停用；请继续前往 Gate 控制台撤销原 API Key");
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setBusy(false); }
  }

  async function armTestnet(account: ExchangeAccount, event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (managedAccounts.has(account.id)) { notify("账户仍有执行任务或正在对账，暂不能重新解锁"); return; }
    const form = event.currentTarget;
    const data = new FormData(form);
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      await api(`/api/v2/exchange-accounts/${account.id}/testnet:arm`, {
        method: "POST",
        headers: { "If-Match": `"${account.etag}"` },
        body: jsonBody({
          mfa_code: data.get("mfa_code"),
          acknowledge_testnet_only: data.get("testnet_only") === "on",
          acknowledge_ip_whitelist: data.get("ip_whitelist") === "on",
          acknowledge_no_withdrawal: data.get("no_withdrawal") === "on",
          acknowledge_exclusive_account: data.get("exclusive") === "on",
        }),
      });
      form.reset(); setArming(null); await load();
      notify("Gate TESTNET 执行已解锁；LIVE 仍由服务端硬禁用");
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setBusy(false); }
  }

  async function disarmTestnet(account: ExchangeAccount) {
    if (busyLock.current) return;
    busyLock.current = true; setBusy(true);
    try {
      await api(`/api/v2/exchange-accounts/${account.id}/testnet:disarm`, {
        method: "POST", headers: { "If-Match": `"${account.etag}"` },
      });
      await load(); notify("TESTNET 执行已锁定");
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setBusy(false); }
  }

  async function createDrill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (busyLock.current) return;
    if (!readyAccounts.some(account => account.id === data.get("exchange_account"))) {
      notify("账户当前不可新开仓，请刷新账户与对账状态"); return;
    }
    busyLock.current = true; setBusy(true);
    try {
      const result = await api<TestnetDrill>("/api/v2/testnet-drills", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: jsonBody({
          paper_public_trade_id: data.get("paper_trade"),
          exchange_account_id: data.get("exchange_account"),
          mfa_code: data.get("mfa_code"),
        }),
      });
      form.reset(); await load();
      notify(`TESTNET 演练已排队：${result.public_drill_id}`);
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setBusy(false); }
  }
  const certifiedTrades = paperTrades.filter((trade) => {
    if (trade.state !== "OPEN") return false;
    const signal = signals.find((item) => item.id === trade.signal_evidence_snapshot_id);
    return signal !== undefined && certifications.some(
      (item) => item.strategy_version_ref === signal.strategy_version_ref,
    );
  });
  const reconcilingAccounts = new Set(drills.filter(drill => drill.state === "RECONCILIATION_PENDING").map(drill => drill.exchange_account_id));
  const managedAccounts = new Set(drills.filter(drill => drillNeedsRefresh(drill.state)).map(drill => drill.exchange_account_id));
  const readyAccounts = accounts.filter(account => account.status === "TESTNET_READY" && account.trading_ready && !managedAccounts.has(account.id));

  return (
    <main className="settings-page exchange-page">
      <section className="page-title">
        <div><h1>交易所账户</h1><span className="status paused">GATE TESTNET</span></div>
        <span className="paper-lock">LIVE HARD-DISABLED</span>
      </section>
      <PageState loading={loading} error={loadError} onRetry={() => void load()} />
      <div className="exchange-security-banner">
        <strong>{configured ? "浏览器加密提交已就绪" : configured === false ? "加密公钥尚未就绪" : "正在检查加密配置"}</strong>
        {!window.isSecureContext && <span>应用层加密 · 传输连接未验证</span>}
      </div>
      <div className="settings-grid exchange-grid">
        <section className="panel">
          <h2>新增 TESTNET 账户</h2>
          <p className="muted-copy">请在 Gate TESTNET 创建独立 API Key，设置固定出口 IP，并关闭提现/转账权限。不要使用主站资金账户凭据。</p>
          <form className="stack-form credential-form" onSubmit={createAccount}>
            <label>账户别名<input name="alias" required maxLength={80} placeholder="Gate TESTNET 主策略" /></label>
            <div className="form-two"><label>交易所<input value="Gate.io" disabled /></label><label>环境<input value="TESTNET" disabled /></label></div>
            <label>API Key<input name="api_key" type="password" required minLength={16} maxLength={128} autoComplete="off" spellCheck={false} /></label>
            <label>API Secret<input name="api_secret" type="password" required minLength={16} maxLength={256} autoComplete="new-password" spellCheck={false} /></label>
            <label className="checkline policy-check"><input name="acknowledge" type="checkbox" required />我确认该 Key 已关闭提现和转账权限，并理解系统目前只能验证读取能力。</label>
            <button className="primary" disabled={busy || !configured}>{busy ? "安全提交中…" : "加密保存并测试连接"}</button>
          </form>
        </section>
        <section className="panel exchange-list">
          <h2>已接入账户</h2>
          {accounts.length ? <PagedList items={accounts} label="交易所账户" size={5} render={(account) => (
            <article className="exchange-card" key={account.id}>
              <header><div><strong>{account.alias}</strong><small>{account.venue} · {account.environment} · {account.market_type}</small></div><span className={`status ${account.status.toLowerCase()}`}>{exchangeStatus(account.status)}</span></header>
              <div className="exchange-facts"><span>凭据版本<b>v{account.credential_version}</b></span><span>指纹<b>{account.credential_fingerprint ?? "验证后生成"}</b></span><span>TESTNET 就绪<b className={account.trading_ready ? "positive" : "negative"}>{account.trading_ready ? "是" : "否"}</b></span></div>
              {account.status === "PENDING_VALIDATION" && <p className="pending-note"><i />隔离验证进程正在执行只读探测，请稍候…</p>}
              {account.validated_at && <p className="validation-note">最近验证 {new Date(account.validated_at).toLocaleString()} · 账户读取 {account.capabilities.account_read ? "成功" : "未确认"} · 永续读取 {account.capabilities.futures_usdt_read ? "成功" : "未确认"}</p>}
              {account.last_safe_error_code && <div className="error-banner">连接测试失败：{exchangeError(account.last_safe_error_code)}</div>}
              {account.execution_freeze_code && <div className="error-banner">执行已冻结：{account.execution_freeze_code}。请先在 Gate TESTNET 核对仓位和条件单。</div>}
              {account.warning_codes.length > 0 && <ul className="warning-list">{account.warning_codes.map((code) => <li key={code}>{exchangeWarning(code)}</li>)}</ul>}
              {managedAccounts.has(account.id) && <p className="pending-note">{reconcilingAccounts.has(account.id) ? "对账期间暂停新开仓及凭据变更" : "执行任务期间暂停重置账户及凭据变更"}；后台持续管理已有风险。<button disabled={loading} onClick={() => void load()}>刷新状态</button></p>}
              {arming === account.id && !managedAccounts.has(account.id) && <Dialog title={`解锁 ${account.alias}`} busy={busy} onClose={() => setArming(null)}><form className="stack-form rotate-form" onSubmit={(event) => armTestnet(account, event)}><p>解锁仅适用于 TESTNET；每次演练仍需再次输入 MFA。</p><label className="checkline policy-check"><input name="testnet_only" type="checkbox" required />这是 Gate TESTNET Key，不含真实资金</label><label className="checkline policy-check"><input name="ip_whitelist" type="checkbox" required />固定出口 IP 白名单已配置且与探测结果一致</label><label className="checkline policy-check"><input name="no_withdrawal" type="checkbox" required />提现和转账权限已关闭</label><label className="checkline policy-check"><input name="exclusive" type="checkbox" required />该账户仅由 GateScope 使用，不手工交易</label><label>MFA 动态码<input name="mfa_code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required autoComplete="one-time-code" /></label><div className="button-row"><button type="button" disabled={busy} onClick={() => setArming(null)}>取消</button><button className="primary" disabled={busy}>确认解锁 TESTNET</button></div></form></Dialog>}
              {rotating === account.id && !managedAccounts.has(account.id) ? <Dialog title={`轮换 ${account.alias} 凭据`} busy={busy} onClose={() => setRotating(null)}><form className="stack-form rotate-form" onSubmit={(event) => rotateCredential(account, event)}><label>新 API Key<input name="api_key" type="password" required minLength={16} maxLength={128} autoComplete="off" /></label><label>新 API Secret<input name="api_secret" type="password" required minLength={16} maxLength={256} autoComplete="new-password" /></label><label className="checkline policy-check"><input name="acknowledge" type="checkbox" required />新 Key 已关闭提现和转账权限</label><div className="button-row"><button type="button" disabled={busy} onClick={() => setRotating(null)}>取消</button><button className="primary" disabled={busy}>加密轮换</button></div></form></Dialog> : !managedAccounts.has(account.id) && account.status !== "DISABLED" && account.status !== "TESTNET_BUSY" && <footer>{account.status === "READ_VALIDATED" && account.capabilities.ip_whitelist_configured && <button className="primary" disabled={busy} onClick={() => setArming(account.id)}>解锁 TESTNET 执行</button>}{account.status === "TESTNET_READY" && <button disabled={busy} onClick={() => disarmTestnet(account)}>锁定执行</button>}<button disabled={busy || account.status === "PENDING_VALIDATION" || account.status === "TESTNET_READY"} onClick={() => validate(account)}>重新测试</button><button disabled={busy || account.status === "PENDING_VALIDATION" || account.status === "TESTNET_READY"} onClick={() => setRotating(account.id)}>轮换密钥</button><button className="danger" disabled={busy} onClick={() => disable(account)}>停用</button></footer>}
            </article>
          )} /> : <Empty label="尚未接入交易所账户" />}
        </section>
      </div>
      <div className="settings-grid exchange-grid testnet-drill-grid">
        <section className="panel">
          <h2>发起 TESTNET 演练</h2>
          <p className="muted-copy">方向、数量、入场上限、止盈和止损全部取自已确认且仍 OPEN 的 PAPER 交易，页面不能覆盖。策略版本还必须有 strategy worker 签发且未过期的 LIVE_ELIGIBLE TESTNET 认证。</p>
          {!certifications.length && <div className="error-banner">当前没有有效执行认证。PAPER 证据不能由人工确认提升等级，因此不会产生 TESTNET 订单。</div>}
          <form className="stack-form" onSubmit={createDrill}>
            <label>已认证的冻结 PAPER 交易<select name="paper_trade" required defaultValue=""><option value="" disabled>请选择 LIVE_ELIGIBLE 策略交易</option>{certifiedTrades.map((item) => <option key={item.public_trade_id} value={item.public_trade_id}>{item.symbol} · {item.side} · {item.public_trade_id}</option>)}</select></label>
            <label>空闲 TESTNET 账户<select name="exchange_account" required defaultValue=""><option value="" disabled>请选择已解锁账户</option>{readyAccounts.map((item) => <option key={item.id} value={item.id}>{item.alias} · {item.credential_fingerprint}</option>)}</select></label>
            <label>MFA 动态码<input name="mfa_code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required autoComplete="one-time-code" /></label>
            <button className="primary" disabled={busy || !readyAccounts.length || !certifiedTrades.length}>{busy ? "提交中…" : "按冻结条款执行 TESTNET"}</button>
          </form>
        </section>
        <section className="panel exchange-list"><h2>TESTNET 执行账本</h2>{drills.length ? <PagedList items={drills} label="TESTNET 账本（最近 100 条）" size={5} render={(drill) => <article className="exchange-card" key={drill.public_drill_id}><header><div><strong>{drill.contract} · {drill.side}</strong><small>{drill.public_drill_id}<br />来源 {drill.paper_public_trade_id}</small></div><span className={`status ${drill.state.toLowerCase()}`}>{drillStateLabel(drill.state)}</span></header>{drill.state === "RECONCILIATION_PENDING" && <p className="error-banner">{reconciliationNotice}</p>}<div className="exchange-facts"><span>入场合约数<b>{drill.filled_contracts}</b></span><span>止损单<b>{drill.stop_order_id ?? "--"}</b></span><span>止盈单<b>{drill.take_profit_order_id ?? "--"}</b></span></div>{drill.safe_code && <p className={drill.frozen ? "error-banner" : "validation-note"}>{executionObservation(drill.safe_code)}</p>}<small>条款摘要 {drill.exact_terms_digest.slice(0, 16)}… · {new Date(drill.updated_at).toLocaleString()}</small></article>} /> : <Empty label="尚无 TESTNET 执行记录" />}</section>
      </div>
      {confirmation.dialog}
      {busy && <div role="status" className="action-progress">正在提交，请稍候…</div>}
    </main>
  );
}

type TelegramBotConfig = {
  configured: boolean;
  source: "NONE" | "LEGACY_ENV" | "WEB_ENCRYPTED";
  bot_username: string | null;
  token_configured: boolean;
  token_fingerprint: string | null;
  allowed_user_ids: number[];
  status: string;
  last_safe_error_code: string | null;
  validated_at: string | null;
  webhook_configured_at: string | null;
  secret_write_allowed: boolean;
  public_https: boolean;
  wrapping_configured: boolean;
};

function TelegramWorkspace({ session, notify }: { session: Session; notify: (value: string) => void }) {
  const [loadError, setLoadError] = useState(""), [loading, setLoading] = useState(true);
  const [linkInfo, setLinkInfo] = useState<{ deep_link: string; expires_at: string }>();
  const [linkQr, setLinkQr] = useState(""), [linkOpen, setLinkOpen] = useState(false);
  const [routeEditor, setRouteEditor] = useState<NotificationRoute | "new">();
  const confirmation = useConfirm();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const [connections, setConnections] = useState<TelegramConnection[]>([]);
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [routes, setRoutes] = useState<NotificationRoute[]>([]);
  const [botConfig, setBotConfig] = useState<TelegramBotConfig>();
  const [configured, setConfigured] = useState(false);
  const [botUsername, setBotUsername] = useState<string | null>(null);
  const [mfaCodes, setMfaCodes] = useState<Record<string, string>>({});
  const [allowedUserIds, setAllowedUserIds] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [editingBot, setEditingBot] = useState(false);
  const [awaitingLink, setAwaitingLink] = useState(false);
  const [testProgress, setTestProgress] = useState<Record<string, string>>({});
  const actionLock = useRef(false);
  const awaitingLinkRef = useRef(false);
  const linkExpiresAtRef = useRef(0);
  const linkBaselineIds = useRef<Set<string>>(new Set());
  const allowedUserIdsInput = useRef<HTMLInputElement>(null);
  const load = useCallback(async () => {
    try {
      const [connectionPage, channelPage, routePage, config] = await Promise.allSettled([
        api<{ items: TelegramConnection[]; configured: boolean; bot_username: string | null }>("/api/v2/telegram/connections"),
        api<Page<NotificationChannel>>("/api/v2/notification-channels"),
        api<Page<NotificationRoute>>("/api/v2/notification-routes"),
        session.user.role === "OWNER" ? api<TelegramBotConfig>("/api/v2/telegram/bot-config") : Promise.resolve(undefined),
      ]);
      if (connectionPage.status === "fulfilled") { setConnections(connectionPage.value.items); setConfigured(connectionPage.value.configured); setBotUsername(connectionPage.value.bot_username); }
      if (channelPage.status === "fulfilled") setChannels(channelPage.value.items);
      if (routePage.status === "fulfilled") setRoutes(routePage.value.items);
      if (config.status === "fulfilled") setBotConfig(config.value);
      const failed = [connectionPage, channelPage, routePage, config].find(r => r.status === "rejected");
      setLoadError(failed?.status === "rejected" ? errorMessage(failed.reason) : "");
      if (connectionPage.status === "fulfilled" && awaitingLinkRef.current && connectionPage.value.items.some((item) => !linkBaselineIds.current.has(item.id))) {
        awaitingLinkRef.current = false;
        setAwaitingLink(false);
        notify("Telegram 已回传，请完成 Web 确认");
      }
    } catch (error) { setLoadError(errorMessage(error)); } finally { setLoading(false); }
  }, [notify, session.user.role]);
  useEffect(() => void load(), [load]);
  useEffect(() => { let live = true; setLinkQr(""); if (linkInfo) void QRCode.toDataURL(linkInfo.deep_link, { width: 220, margin: 2 }).then(qr => { if (live) setLinkQr(qr); }).catch(() => undefined); return () => { live = false; }; }, [linkInfo]);
  useEffect(() => {
    if (botConfig) setAllowedUserIds(botConfig.allowed_user_ids.join(", "));
  }, [botConfig?.allowed_user_ids.join(",")]);
  useEffect(() => {
    if (!awaitingLink) return;
    const refresh = () => {
      if (Date.now() >= linkExpiresAtRef.current) {
        awaitingLinkRef.current = false;
        setAwaitingLink(false);
        return;
      }
      void load();
    };
    const timer = window.setInterval(refresh, 2_500);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [awaitingLink, load]);

  function beginAction(action: string): boolean {
    if (actionLock.current) return false;
    actionLock.current = true;
    setBusyAction(action);
    return true;
  }

  function finishAction() {
    actionLock.current = false;
    setBusyAction(null);
  }

  function addAllowedUserId(userId: string) {
    setAllowedUserIds((current) => appendTelegramUserId(current, userId));
    window.requestAnimationFrame(() => {
      allowedUserIdsInput.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      allowedUserIdsInput.current?.focus();
    });
    notify(`Telegram 用户 ID ${userId} 已加入输入框，请验证并保存`);
  }

  async function pasteTelegramUserId() {
    try {
      const value = parseTelegramUserId(await navigator.clipboard.readText());
      if (!value) {
        notify("未识别到用户 ID，请从 Telegram 复制完整回复或直接输入数字");
        return;
      }
      addAllowedUserId(value);
    } catch {
      notify("浏览器未授权读取剪贴板，请长按输入框手动粘贴");
    }
  }
  async function link() {
    if (!beginAction("link")) return;
    try {
      const result = await api<{ deep_link: string; expires_at: string }>("/api/v2/telegram/link-tokens", { method: "POST" });
      linkBaselineIds.current = new Set(connections.map((item) => item.id));
      linkExpiresAtRef.current = new Date(result.expires_at).getTime();
      awaitingLinkRef.current = true;
      setAwaitingLink(true);
      setLinkInfo(result); setLinkOpen(true); notify("绑定链接已生成，请点击打开 Telegram 或扫码");
    } catch (error) { notify(errorMessage(error)); }
    finally { finishAction(); }
  }
  async function confirm(connection: TelegramConnection) {
    if (!beginAction(`confirm:${connection.id}`)) return;
    try {
      await api(`/api/v2/telegram/connections/${connection.id}:confirm`, { method: "POST", body: jsonBody({ code: mfaCodes[connection.id] ?? "" }) });
      await load(); notify("Telegram 私聊绑定已激活");
    } catch (error) { notify(errorMessage(error)); }
    finally { setMfaCodes(current => ({ ...current, [connection.id]: "" })); finishAction(); }
  }
  async function createChannel(connection: TelegramConnection) {
    if (!beginAction(`channel:${connection.id}`)) return;
    try {
      await api("/api/v2/notification-channels", { method: "POST", body: jsonBody({ alias: `私聊 @${connection.telegram_username ?? connection.telegram_user_id}`, connection_id: connection.id }) });
      await load(); notify("通知通道已建立");
    } catch (error) { notify(errorMessage(error)); }
    finally { finishAction(); }
  }
  async function test(channel: NotificationChannel) {
    if (!beginAction(`test:${channel.id}`)) return;
    setTestProgress((current) => ({ ...current, [channel.id]: "排队中…" }));
    try {
      const queued = await api<{ operation_id: string }>(`/api/v2/notification-channels/${channel.id}/test`, { method: "POST" });
      for (let attempt = 0; attempt < 24; attempt += 1) {
        if (!alive.current) return;
        const operation = await api<Operation>(`/api/v2/operations/${queued.operation_id}`);
        setTestProgress((current) => ({ ...current, [channel.id]: operation.status === "QUEUED" ? "排队中…" : operation.status === "RUNNING" ? "发送中…" : operation.status === "SUCCEEDED" ? "已送达" : "发送失败" }));
        if (operation.status === "SUCCEEDED") { notify("Telegram 测试消息已送达"); return; }
        if (operation.status === "FAILED") throw new Error(notificationFailureMessage(operation.safe_error_code));
        await delay(750);
      }
      notify("消息仍在队列中，可在消息中心查看进度");
    }
    catch (error) { const message = errorMessage(error); setTestProgress(current => ({ ...current, [channel.id]: message })); notify(message); }
    finally {
      finishAction();
    }
  }
  async function saveBotConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!beginAction("save-bot")) return;
    const form = event.currentTarget;
    const data = new FormData(form);
    const allowedIds = allowedUserIds
      .split(/[\s,]+/)
      .filter(Boolean)
      .map(Number);
    if (allowedIds.some((value) => !Number.isSafeInteger(value) || value <= 0)) {
      notify("请输入有效的 Telegram 数字用户 ID，可用逗号或空格分隔"); finishAction(); return;
    }
    try {
      const encryptedSubmission = await encryptSubmission(`tenant:${session.user.tenant_id}:telegram_bot_config`, {
        bot_token: data.get("bot_token"),
        allowed_user_ids: allowedIds,
        current_password: data.get("current_password"),
        mfa_code: data.get("mfa_code"),
      });
      const updated = await api<TelegramBotConfig>("/api/v2/telegram/bot-config", {
        method: "PUT",
        body: jsonBody(wrapEncryptedSubmission(encryptedSubmission)),
      });
      setAllowedUserIds(updated.allowed_user_ids.join(", ")); setBotConfig(updated); setEditingBot(false); await load();
      notify(updated.configured ? "Telegram Bot 与 Webhook 已配置" : "Bot Token 已加密保存；启用 HTTPS 后才能建立 Webhook");
    } catch (error) { notify(errorMessage(error)); }
    finally { clearSensitiveFields(form); finishAction(); }
  }
  return (
    <main className="settings-page">
      <SettingsTabs active="notifications" />
      <section className="page-title"><div><h1>Telegram 与通知</h1><p>{loading ? "正在加载…" : loadError ? "部分信息读取失败" : awaitingLink ? "等待 Telegram 回传…" : configured ? `${botUsername ? `@${botUsername} · ` : ""}个人绑定与路由` : "Bot 未就绪"}</p></div><div className="button-row"><button onClick={() => void load()}>刷新</button><button className="primary" aria-busy={busyAction === "link" || awaitingLink} disabled={!configured || busyAction !== null} onClick={() => awaitingLink ? setLinkOpen(true) : void link()}>{busyAction === "link" ? "正在生成…" : awaitingLink ? "查看绑定链接" : "绑定 Telegram"}</button></div></section>
      <PageState loading={loading} error={loadError} onRetry={() => void load()} />
      {linkOpen && linkInfo && <Dialog title="绑定 Telegram" onClose={() => setLinkOpen(false)}>{linkQr && <img className="link-qr" src={linkQr} alt="Telegram 绑定二维码" />}<a className="link-launch" href={linkInfo.deep_link} target="_blank" rel="noopener noreferrer">点击打开 Telegram</a><p>电脑可用手机扫码。链接有效至 {new Date(linkInfo.expires_at).toLocaleTimeString()}，完成后返回网页确认。</p><button disabled={busyAction !== null} onClick={() => void load()}>我已完成，刷新绑定状态</button></Dialog>}
      {connections.some(c=>c.status==="ACTIVE") && <section className="panel"><header><h2>TG 行情查询</h2>{botUsername && <a href={`https://t.me/${botUsername}`} target="_blank" rel="noopener noreferrer">打开 Bot ↗</a>}</header><p>发送 <code>/watchlist</code>，选择自选币种、周期和形态状态，查看带原理线的 K 线图。</p><p><code>/chart BTC 1h</code> 可直接查询指标图。支持首次识别图、最新状态图和分页查询。</p></section>}
      {session.user.role === "OWNER" && botConfig && <section className="panel provider-config-panel">
        <header><div><h2>工作区 Bot（管理员）</h2><p>{botConfig.bot_username ? `@${botConfig.bot_username}` : "未配置"} · <span className={`status ${botConfig.configured ? "active" : "paused"}`}>{telegramConfigStatus(botConfig.status)}</span></p></div><div className="button-row">{botConfig.token_fingerprint && <code title="Bot Token 指纹">{botConfig.token_fingerprint}</code>}{botConfig.configured && !editingBot && <button type="button" disabled={busyAction !== null} onClick={() => setEditingBot(true)}>更新配置</button>}</div></header>
        {!window.isSecureContext && <div className="error-banner">当前连接未验证</div>}
        {(!botConfig.configured || editingBot) && <form className="provider-config-form" aria-busy={busyAction === "save-bot"} onSubmit={saveBotConfig}>
          <label>Bot Token<input name="bot_token" type="password" required autoComplete="new-password" placeholder="123456789:••••••••••••••••" /></label>
          <div className="form-field telegram-id-field">
            <div className="field-label-row"><label htmlFor="telegram-allowed-user-ids">预授权 Telegram 用户 ID（可选）</label><a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">在 Telegram 获取 ID <span aria-hidden="true">↗</span></a></div>
            <input ref={allowedUserIdsInput} id="telegram-allowed-user-ids" name="allowed_user_ids" value={allowedUserIds} onChange={(event) => setAllowedUserIds(event.target.value)} inputMode="numeric" autoComplete="off" placeholder="留空即可让成员自助绑定" aria-describedby="telegram-id-help" />
            <div className="telegram-id-assist" id="telegram-id-help"><small>可留空；多个 ID 用逗号分隔</small><button type="button" onClick={() => void pasteTelegramUserId()}>粘贴识别</button></div>
          </div>
          <label>当前登录密码<input name="current_password" type="password" required autoComplete="current-password" placeholder="用于本次敏感操作复核" /></label>
          <label>MFA 动态码<input name="mfa_code" required inputMode="numeric" minLength={6} maxLength={6} pattern="[0-9]{6}" autoComplete="one-time-code" placeholder="000000" /></label>
          <div className="button-row">{botConfig.configured && <button type="button" disabled={busyAction !== null} onClick={() => setEditingBot(false)}>取消</button>}<button className="primary" aria-busy={busyAction === "save-bot"} disabled={!botConfig.wrapping_configured || busyAction !== null}>{busyAction === "save-bot" ? "验证并连接中…" : "保存 Bot"}</button></div>
        </form>}
        {botConfig.configured && <small className="config-ok">已加密保存 · 敏感输入不会回显</small>}
      </section>}
      {!configured && !loading && !loadError && <div className="error-banner">{session.user.role === "OWNER" ? "请先配置工作区 Bot。" : "工作区 Bot 尚未配置，请联系管理员。"}</div>}
      <div className="settings-grid">
        <section className="panel"><h2>我的绑定账号</h2>{connections.length ? <PagedList label="Telegram 绑定" items={connections} size={5} render={(connection) => <article className="connection-card" key={connection.id}><div><strong>@{connection.telegram_username ?? connection.telegram_user_id}</strong><small>用户 ID {connection.telegram_user_id}</small><span className={`status ${connection.status.toLowerCase()}`}>{connection.status}</span></div><div className="connection-actions">{connection.status === "PENDING_CONFIRMATION" ? <div className="inline-confirm"><input disabled={busyAction !== null} inputMode="numeric" maxLength={6} placeholder="新的 MFA 验证码" value={mfaCodes[connection.id] ?? ""} onChange={(event) => setMfaCodes({ ...mfaCodes, [connection.id]: event.target.value.replace(/\D/g, "") })} /><button disabled={busyAction !== null || (mfaCodes[connection.id] ?? "").length !== 6} onClick={() => confirm(connection)}>{busyAction === `confirm:${connection.id}` ? "确认中…" : "Web 确认"}</button></div> : connection.status === "ACTIVE" && !channels.some((channel) => channel.connection_id === connection.id) && <button disabled={busyAction !== null} onClick={() => createChannel(connection)}>{busyAction === `channel:${connection.id}` ? "创建中…" : "创建通道"}</button>}</div></article>} /> : <Empty label={busyAction === "link" ? "正在创建绑定…" : "尚未发起绑定"} />}</section>
        <section className="panel"><h2>通知通道</h2>{channels.length ? <PagedList label="通知通道" items={channels} size={5} render={(channel) => <article className="connection-card" key={channel.id}><div><strong>{channel.alias}</strong><span className={`status ${channel.status.toLowerCase()}`}>{channel.status}</span>{testProgress[channel.id] && <small role="status">{testProgress[channel.id]}</small>}</div><button aria-busy={busyAction === `test:${channel.id}`} disabled={busyAction !== null || channel.status !== "ACTIVE"} onClick={() => test(channel)}>{busyAction === `test:${channel.id}` ? "发送中…" : "发送测试"}</button></article>} /> : <Empty label="完成私聊绑定后创建通道" />}</section>
        <section className="panel route-panel"><h2>推送路由</h2><div className="module-actions"><button className="primary" disabled={!channels.some(c => c.status === "ACTIVE") || busyAction !== null} onClick={() => setRouteEditor("new")}>新建路由</button>{!channels.length && <small>请先完成绑定并创建通知通道。</small>}</div><div className="route-list"><PagedList label="通知路由" items={routes} size={6} render={route => <div key={route.id}><strong>{route.name} · {route.enabled ? "启用" : "暂停"}</strong><span>{route.event_types.join(" · ")} · {route.max_per_minute}/min{route.quiet_hours ? ` · 免打扰 ${route.quiet_hours.start}–${route.quiet_hours.end}` : ""}</span><div className="module-actions"><button onClick={() => setRouteEditor(route)}>编辑</button><button disabled={busyAction !== null} onClick={async () => { if (!await confirmation.ask("删除通知路由", `删除 ${route.name} 后，将不再按此路由推送；历史记录保留。`)) return; if (!beginAction("delete-route")) return; try { await api(`/api/v2/notification-routes/${route.id}`, { method: "DELETE" }); await load(); notify("路由已删除，历史记录保留"); } catch (e) { notify(errorMessage(e)); } finally { finishAction(); } }}>删除</button></div></div>} /></div></section>
      </div>
      {routeEditor && <NotificationRouteEditor route={routeEditor === "new" ? undefined : routeEditor} channels={channels} onClose={() => setRouteEditor(undefined)} onSaved={() => { setRouteEditor(undefined); void load(); notify("推送路由已保存"); }} />}{confirmation.dialog}
    </main>
  );
}


type DataSourceSummary = {
  id: string;
  name: string;
  configured: boolean;
  state: string;
  origin?: string;
  bot_username?: string | null;
  secret_location: string;
  automatic_feature_eligible?: boolean;
  key_fingerprint?: string | null;
  last_tested_at?: string | null;
};

type CoinglassConfig = {
  latest_test?: CoinglassTest | null;
  configured: boolean;
  origin: string;
  key_fingerprint: string | null;
  status: string;
  last_safe_error_code: string | null;
  capability: Record<string, unknown>;
  last_tested_at: string | null;
  secret_write_allowed: boolean;
  browser_submission_configured: boolean;
};

type CoinglassCheck = {
  ok: boolean;
  http_status: number | null;
  business_validation: string;
  elapsed_ms?: number;
  received_at?: string;
  items?: number;
  fields?: string[];
  metadata?: Record<string, unknown>;
  safe_error_code?: string;
  retry_after_seconds?: number;
};

type CoinglassTest = {
  id: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  symbol: string;
  attempt_count: number;
  result: {
    connected?: boolean;
    checks?: Record<string, CoinglassCheck>;
  };
  safe_error_code: string | null;
  retry_after_seconds: number | null;
  created_at: string;
  completed_at: string | null;
};

function DataSourcesWorkspace({ session, notify }: { session: Session; notify: (value: string) => void }) {
  const [loadError, setLoadError] = useState(""), [loading, setLoading] = useState(true);
  const busyLock = useRef(false), alive = useRef(true);
  const [sources, setSources] = useState<DataSourceSummary[]>([]);
  const [worker, setWorker] = useState<Record<string, unknown>>({});
  const [strategyWorker, setStrategyWorker] = useState<Record<string, unknown>>({});
  const [config, setConfig] = useState<CoinglassConfig>();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<CoinglassTest>();
  const load = useCallback(async () => {
    try {
      const [payload, configured] = await Promise.all([
        api<{ items: DataSourceSummary[]; market_worker: Record<string, unknown>; strategy_worker: Record<string, unknown> }>("/api/v2/data-sources"),
        api<CoinglassConfig>("/api/v2/data-sources/coinglass/config"),
      ]);
      setSources(payload.items); setWorker(payload.market_worker); setStrategyWorker(payload.strategy_worker);
      setConfig(configured); setLoadError("");
      if (!busyLock.current) setTest(configured.latest_test ?? undefined);
    } catch (error) { setLoadError(errorMessage(error)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    alive.current = true; void load();
    const timer = window.setInterval(load, 15_000);
    return () => { alive.current = false; window.clearInterval(timer); };
  }, [load]);
  async function waitForTest(initial: CoinglassTest) {
    setTest(initial);
    if (["SUCCEEDED", "FAILED"].includes(initial.status)) return initial;
    for (let attempt = 0; attempt < 75; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1_000));
      if (!alive.current) return initial;
      const payload = await api<{ test: CoinglassTest }>(`/api/v2/data-sources/coinglass/tests/${initial.id}`);
      setTest(payload.test);
      if (["SUCCEEDED", "FAILED"].includes(payload.test.status)) return payload.test;
    }
    throw new Error("检测仍在后台执行，请稍后点击刷新");
  }
  async function saveCoinglass(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    if (busyLock.current) return;
    busyLock.current = true; setSaving(true); setTest(undefined);
    try {
      const encrypted = await encryptSubmission(`tenant:${session.user.tenant_id}:coinglass_config`, {
        api_key: data.get("api_key"),
        current_password: data.get("current_password"),
        mfa_code: data.get("mfa_code"),
      });
      const response = await api<{ config: CoinglassConfig; test: CoinglassTest }>(
        "/api/v2/data-sources/coinglass/config",
        { method: "PUT", body: jsonBody(wrapEncryptedSubmission(encrypted)) },
      );
      setConfig(response.config);
      clearSensitiveFields(form);
      const completed = await waitForTest(response.test);
      notify(completed.status === "SUCCEEDED" ? "Coinglass 与 LSR 已连接" : "配置已保存，但连通性检测未通过");
      await load();
    } catch (error) { notify(errorMessage(error)); }
    finally { clearSensitiveFields(form); busyLock.current = false; setSaving(false); }
  }
  async function testCoinglass() {
    if (busyLock.current) return;
    busyLock.current = true; setTesting(true); setTest(undefined);
    try {
      const response = await api<{ test: CoinglassTest }>("/api/v2/data-sources/coinglass:test", { method: "POST" });
      const completed = await waitForTest(response.test);
      notify(completed.status === "SUCCEEDED" ? "三个接口均检测成功" : "部分接口检测失败");
      await load();
    } catch (error) { notify(errorMessage(error)); }
    finally { busyLock.current = false; setTesting(false); }
  }
  const checks = test?.result.checks ?? {};
  return (
    <main className="settings-page">
      <section className="page-title"><div><h1>数据源与运行状态</h1></div><button onClick={load}>刷新</button></section>
      <PageState loading={loading} error={loadError} onRetry={() => void load()} />
      <section className="panel provider-config-panel">
        <header><div><h2>Coinglass / LSR 中转站</h2><p>工作区共享配置，密钥只允许行情 Worker 解密。</p></div><span className={`status ${config?.status === "ACTIVE" ? "active" : "paused"}`}>{coinglassStatus(config?.status ?? "NOT_CONFIGURED")}</span></header>
        <div className="provider-origin"><span>固定服务地址</span><code>https://vip2.coinglass.site</code><small>不可修改 · API Key 仅通过 X-API-Key 请求头发送</small></div>
        <form className="provider-config-form" aria-busy={saving} onSubmit={saveCoinglass}>
          <label>API Key<input type="password" name="api_key" required minLength={35} maxLength={131} autoComplete="new-password" placeholder={config?.key_fingerprint ? `已保存 ${config.key_fingerprint} · 输入新 Key 可轮换` : "cg_…"} /></label>
          <label>当前登录密码<input type="password" name="current_password" required autoComplete="current-password" /></label>
          <label>MFA 动态码<input name="mfa_code" required inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} placeholder="新的 6 位验证码" /></label>
          <div className="provider-config-summary"><span>保存后自动检测</span><b>市场列表 · BTC LSR · 账户权限</b>{config?.last_tested_at && <small>最近检测 {new Date(config.last_tested_at).toLocaleString()}</small>}</div>
          <div className="button-row"><button type="button" disabled={!config?.configured || testing || saving} onClick={testCoinglass}>{testing ? "检测中…" : "重新检测"}</button><button className="primary" disabled={saving || testing || !config?.secret_write_allowed} aria-busy={saving}>{saving ? "加密保存并检测…" : config?.configured ? "轮换 Key 并检测" : "保存并检测"}</button></div>
        </form>
      </section>
      {!loading && config && !config.secret_write_allowed && <div className="page-error">当前没有密钥写入权限或加密配置未就绪，请联系工作区管理员。</div>}
      {test && <section className="panel provider-result">
        <header><div><h2>连通性检测</h2><small>测试币种 {test.symbol} · {coinglassTestStatus(test.status)}</small></div><span className={`status ${test.status === "SUCCEEDED" ? "active" : "paused"}`}>{coinglassTestStatus(test.status)}</span></header>
        <div className="provider-check-grid">{(["coins_markets", "lsr", "account"] as const).map((name) => <ProviderCheckCard key={name} name={name} check={checks[name]} pending={!checks[name] && !["FAILED", "SUCCEEDED"].includes(test.status)} />)}</div>
        {checks.account?.metadata && Object.keys(checks.account.metadata).length > 0 && <div className="provider-account-metadata"><h3>账户权限与额度</h3><dl>{Object.entries(checks.account.metadata).slice(0, 30).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{safeMetadataValue(value)}</dd></div>)}</dl></div>}
        {test.safe_error_code && <div className="provider-error">{coinglassFailureMessage(test.safe_error_code)}{test.retry_after_seconds ? ` · ${test.retry_after_seconds} 秒后可重试` : ""}</div>}
      </section>}
      <div className="settings-grid data-source-grid">
        {sources.map((source) => <section className="panel data-source-card" key={source.id}><header><div><h2>{source.name}</h2><small>{source.origin ?? source.bot_username ?? source.secret_location}</small></div><span className={`status ${source.configured ? "active" : "paused"}`}>{source.id === "coinglass" ? coinglassStatus(source.state) : source.state}</span></header><div className="exchange-facts"><span>服务器配置<b>{source.configured ? "已配置" : "待配置"}</b></span><span>密钥边界<b>{source.secret_location}</b></span></div>{source.id === "coinglass" && <p className="muted-copy">当前只用于研究与人工决策，不自动取得真实开单资格。</p>}</section>)}
        <section className="panel data-source-card"><header><div><h2>正式行情 Worker</h2><small>{String(worker.worker_id ?? "等待启动")}</small></div><span className={`status ${worker.healthy ? "active" : "paused"}`}>{String(worker.state ?? "UNKNOWN")}</span></header><pre className="safe-json">{JSON.stringify(worker.status ?? {}, null, 2)}</pre></section>
        <section className="panel data-source-card"><header><div><h2>策略 Worker</h2><small>{String(strategyWorker.worker_id ?? "等待启动")}</small></div><span className={`status ${strategyWorker.healthy ? "active" : "paused"}`}>{String(strategyWorker.state ?? "UNKNOWN")}</span></header><pre className="safe-json">{JSON.stringify(strategyWorker.status ?? {}, null, 2)}</pre></section>
      </div>
    </main>
  );
}

function ProviderCheckCard({ name, check, pending }: { name: "coins_markets" | "lsr" | "account"; check?: CoinglassCheck; pending: boolean }) {
  const labels = { coins_markets: "Coinglass 市场", lsr: "BTC LSR", account: "账户权限" };
  return <article className={`provider-check ${check?.ok ? "passed" : check ? "failed" : "pending"}`}><header><strong>{labels[name]}</strong><span>{pending ? "等待" : check?.ok ? "成功" : "失败"}</span></header><dl><div><dt>HTTP</dt><dd>{check?.http_status ?? "--"}</dd></div><div><dt>业务校验</dt><dd>{check?.business_validation ? ({ PASSED: "通过", SCHEMA_PASSED: "结构通过", NOT_RUN: "未执行", FAILED: "失败" } as Record<string, string>)[check.business_validation] ?? check.business_validation : "--"}</dd></div><div><dt>耗时</dt><dd>{typeof check?.elapsed_ms === "number" ? `${check.elapsed_ms} ms` : "--"}</dd></div></dl>{typeof check?.items === "number" && <small>返回 {check.items} 条市场数据</small>}{check?.safe_error_code && <small>{coinglassFailureMessage(check.safe_error_code)}</small>}</article>;
}

function safeMetadataValue(value: unknown): string {
  if (value === null || value === undefined) return "--";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try { return JSON.stringify(value); }
  catch { return "--"; }
}

function SecurityWorkspace({ session }: { session: Session }) {
  const [sessions, setSessions] = useState<Array<{ id: string; last_seen_at: string; current: boolean; auth_strength: string }>>([]);
  const [notice, setNotice] = useState(""), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const action = useAction(setError), confirmation = useConfirm();
  const load = useCallback(async () => {
    try { const result = await api<{ items: typeof sessions }>("/auth/sessions"); setSessions(result.items); setError(""); }
    catch (e) { setError(errorMessage(e)); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function logout() { await api("/auth/logout", { method: "POST" }); location.assign("/login"); }
  return <main className="settings-page"><SettingsTabs active="security" /><section className="page-title"><div><h1>用户设置</h1><small className="user-meta">{session.user.username} · {session.user.role} · {session.auth_strength}</small></div><button disabled={!!action.busy} onClick={() => void action.run("退出中…", logout)}>{action.busy || "退出登录"}</button></section><PageState loading={loading} error={error} onRetry={() => void load()} /><div className="settings-grid user-settings-grid"><MfaManager authenticated onVerified={async () => setNotice("验证器设置已更新")} /><section className="panel sessions"><h2>活动会话</h2><PagedList label="活动会话" items={sessions} render={item => <article key={item.id}><div><strong>{item.current ? "当前会话" : "其他会话"}</strong><small>{item.auth_strength} · 最后活动 {new Date(item.last_seen_at).toLocaleString()}</small></div>{!item.current && <button disabled={!!action.busy} onClick={() => void action.run("撤销中…", async () => { if (!await confirmation.ask("退出其他设备", "该设备需要重新登录，当前设备不受影响。")) return; await api(`/auth/sessions/${item.id}`, { method: "DELETE" }); await load(); setNotice("该会话已退出"); })}>退出此设备</button>}</article>} /></section></div>{notice && <div className="research-feedback" role="status">{notice}<button onClick={() => setNotice("")}>关闭</button></div>}{confirmation.dialog}</main>;
}

type NotificationEvent = {
  id: string;
  event_type: string;
  channel_alias: string;
  status: string;
  telegram_message_id: number | null;
  safe_error_code: string | null;
  created_at: string;
  updated_at: string;
};

function NotificationCenter({ notify }: { notify: (value: string) => void }) {
  const [items, setItems] = useState<NotificationEvent[]>([]);
  const [attention, setAttention] = useState(0);
  const [pageIndex, setPageIndex] = useState(0), [total, setTotal] = useState(0), [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [detail, setDetail] = useState<NotificationEvent>();
  const requests = useRef(0);
  const load = useCallback(async () => {
    const current = ++requests.current; setLoading(true);
    try {
      const page = await api<Page<NotificationEvent> & { attention_count: number; total: number }>(`/api/v2/notifications?limit=20&offset=${pageIndex * 20}&status=${filter}`);
      if (current !== requests.current) return;
      setItems(page.items); setAttention(page.attention_count); setTotal(page.total ?? page.items.length); setError("");
    } catch (error) { if (current === requests.current) setError(errorMessage(error)); }
    finally { if (current === requests.current) setLoading(false); }
  }, [pageIndex, filter]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (pageIndex === 0 && !document.hidden) void load(); }, 15_000);
    return () => { window.clearInterval(timer); requests.current += 1; };
  }, [load, pageIndex]);
  return <main className="settings-page"><section className="page-title"><div><h1>消息通知</h1></div><div className="button-row"><span className={`status ${attention ? "paused" : "active"}`}>{error ? "状态未知" : loading ? "更新中" : attention ? `${attention} 条待处理` : "无待处理消息"}</span><button disabled={loading} onClick={() => void load()}>刷新</button></div></section><PageState loading={loading} error={error} onRetry={() => void load()} /><label className="module-actions">投递状态<select value={filter} onChange={e => { setFilter(e.target.value); setPageIndex(0); }}><option value="">全部</option>{["PENDING", "SENDING", "RETRY", "SENT", "FAILED", "DEAD_LETTER"].map(s => <option key={s} value={s}>{deliveryStatus(s)}</option>)}</select></label><section className="panel notification-feed">{items.length ? items.map((item) => <article key={item.id}><span className={`delivery-dot ${item.status.toLowerCase()}`} /><div><strong>{notificationEventLabel(item.event_type)}</strong><small>{item.channel_alias} · {new Date(item.updated_at).toLocaleString()}</small>{item.safe_error_code && <code>{notificationFailureMessage(item.safe_error_code)}</code>}</div><span className={`status ${item.status.toLowerCase()}`}>{deliveryStatus(item.status)}</span><button onClick={() => setDetail(item)}>详情</button></article>) : !loading && !error && <Empty label="该条件下暂无投递记录" />}<Pagination label="消息" page={pageIndex} pages={Math.max(1, Math.ceil(total / 20))} total={total} busy={loading} onChange={setPageIndex} /></section>{detail && <Dialog title="通知详情" onClose={() => setDetail(undefined)}><dl className="detail-facts">{[["事件", notificationEventLabel(detail.event_type)], ["状态", deliveryStatus(detail.status)], ["通道", detail.channel_alias], ["创建时间", new Date(detail.created_at).toLocaleString()], ["更新时间", new Date(detail.updated_at).toLocaleString()], ["消息编号", detail.telegram_message_id ?? "尚未送达"]].map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>{detail.safe_error_code && <PageState error={notificationFailureMessage(detail.safe_error_code)} />}<a href="/settings/notifications">前往 Telegram 与通知设置</a></Dialog>}</main>;
}

function SettingsTabs({ active }: { active: "security" | "notifications" }) {
  return <nav className="settings-tabs" aria-label="用户设置"><a className={active === "security" ? "active" : ""} href={VIEW_PATHS.security}>账号与验证器</a><a className={active === "notifications" ? "active" : ""} href={VIEW_PATHS.telegram}>Telegram 与通知</a></nav>;
}

function Brand() { return <div className="brand"><span>G</span><div><strong>GateScope</strong><small>Evidence-first trading system</small></div></div>; }
function NotificationIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></svg>; }
function SearchIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></svg>; }
function PlusIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>; }
function ChevronIcon() { return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4" /></svg>; }
function CoinOrbitIcon() { return <svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="7" /><ellipse cx="24" cy="24" rx="19" ry="9" transform="rotate(-28 24 24)" /><circle cx="39" cy="18" r="2.5" /></svg>; }
function CoinMark({ symbol, large = false }: { symbol: string; large?: boolean }) {
  const base = splitMarketSymbol(symbol)[0];
  const hue = Array.from(base).reduce((value, character) => value + character.charCodeAt(0), 0) % 360;
  return <span className={`coin-mark ${large ? "large" : ""}`} style={{ "--coin-hue": `${hue}deg` } as React.CSSProperties}>{base.slice(0, 2)}</span>;
}
function NavButton({ active, children, onClick }: { active: boolean; children: React.ReactNode; onClick: () => void }) { return <button className={active ? "active" : ""} onClick={onClick}>{children}</button>; }
function NavLink({ active, children, href }: { active: boolean; children: React.ReactNode; href: string }) { return <a className={active ? "active" : ""} href={href} aria-current={active ? "page" : undefined}>{children}</a>; }
function Empty({ label }: { label: string }) { return <div className="empty">{label}</div>; }
function Splash({ label }: { label: string }) { return <main className="auth-shell"><div className="splash"><Brand /><div className="pulse" />{label}</div></main>; }
function epoch(value: string): UTCTimestamp { return Math.floor(new Date(value).getTime() / 1000) as UTCTimestamp; }
function delay(milliseconds: number): Promise<void> { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }
function errorMessage(reason: unknown): string { return reason instanceof Error ? reason.message : "请求失败，请稍后重试"; }
function percent(value: number | undefined): string { return value === undefined ? "--" : `${(value * 100).toFixed(2)}%`; }
function splitMarketSymbol(symbol: string): [string, string] {
  const [base, quote = ""] = symbol.replace("/", "_").split("_", 2);
  return [base || symbol, quote];
}
function marketSymbol(symbol: string): string {
  const [base, quote] = splitMarketSymbol(symbol);
  return quote ? `${base} / ${quote}` : base;
}
function formatMarketPrice(value: number | undefined): string {
  return priceText(value);
}
function formatIndicatorValue(value: number): string {
  if (!Number.isFinite(value)) return "--";
  const magnitude = Math.abs(value);
  return magnitude >= 100_000 ? value.toExponential(2) : priceText(value);
}
function isIndicatorNodeSpec(value: unknown): value is IndicatorNodeSpec {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<IndicatorNodeSpec>;
  return typeof candidate.instance_id === "string"
    && typeof candidate.indicator_id === "string"
    && Boolean(candidate.params) && typeof candidate.params === "object"
    && Array.isArray(candidate.depends_on)
    && INDICATOR_PRESETS.some((item) => item.instance_id === candidate.instance_id && item.indicator_id === candidate.indicator_id);
}
function indicatorOutputLabel(node: IndicatorNodeSpec, output: string): string {
  if (node.indicator_id === "boll") return `${indicatorNodeLabel(node)} ${output}`;
  if (["macd", "adx"].includes(node.indicator_id)) return output.toUpperCase();
  return indicatorNodeLabel(node);
}
function indicatorNodeLabel(node: IndicatorNodeSpec): string {
  return `${node.indicator_id.toUpperCase()}${node.params.period ? ` ${node.params.period}` : node.indicator_id === "macd" ? ` ${node.params.fast_period}/${node.params.slow_period}/${node.params.signal_period}` : ""}`;
}
function viewForPath(path: string, role: string): View | "forbidden" | "notfound" {
  if (["/settings", "/settings/security"].includes(path)) return "security";
  const entry = (Object.entries(VIEW_PATHS) as Array<[View, string]>).find(([, value]) => value === path);
  if (!entry && !["/", "/login"].includes(path)) return "notfound";
  const view = entry?.[0] ?? "chart";
  if (role !== "OWNER" && ["exchange", "mcp", "data"].includes(view)) return "forbidden";
  return view;
}
function telegramConfigStatus(status: string): string {
  return ({ ACTIVE: "已连接", NEEDS_HTTPS: "等待 HTTPS", VALIDATION_FAILED: "连接失败", DISABLED: "已停用", UNCONFIGURED: "未配置", LEGACY_ENV: "环境配置" } as Record<string, string>)[status] ?? status;
}
function coinglassStatus(status: string): string {
  return ({ ACTIVE: "已连接", PENDING_TEST: "等待检测", VALIDATION_FAILED: "检测失败", DISABLED: "已停用", NOT_CONFIGURED: "未配置", SECRET_REQUIRED: "待配置", READY_TO_TEST: "可检测" } as Record<string, string>)[status] ?? status;
}
function coinglassTestStatus(status: string): string {
  return ({ QUEUED: "排队中", RUNNING: "检测中", SUCCEEDED: "全部成功", FAILED: "检测失败" } as Record<string, string>)[status] ?? status;
}
function coinglassFailureMessage(code: string): string {
  return ({
    PROVIDER_RATE_LIMITED: "上游限流，已按 Retry-After 停止重试",
    PROVIDER_SECURITY_ERROR: "上游发生不安全跳转，已拒绝发送密钥",
    PROVIDER_SCHEMA_ERROR: "上游返回结构不符合预期",
    PROVIDER_BUSINESS_ERROR: "HTTP 成功，但业务码表示失败",
    PROVIDER_UNAVAILABLE: "上游暂时不可用",
    PROVIDER_TEST_INVALID_RESPONSE: "检测结果无效",
    PROVIDER_TEST_WORKER_TIMEOUT: "检测 Worker 超时",
    COINGLASS_CONFIG_SUPERSEDED: "检测期间配置已轮换",
  } as Record<string, string>)[code] ?? "检测未通过";
}

function clearSensitiveFields(form: HTMLFormElement): void {
  form.querySelectorAll<HTMLInputElement>('input[type="password"], input[autocomplete="one-time-code"]')
    .forEach((input) => { input.value = ""; });
}
function notificationEventLabel(eventType: string): string {
  return ({
    "notification.test.requested.v1": "测试通知",
    "notification.requested.v1": "策略与风险通知",
  } as Record<string, string>)[eventType] ?? eventType;
}
function deliveryStatus(status: string): string {
  return ({ PENDING: "排队中", SENDING: "发送中", RETRY: "重试中", SENT: "已送达", FAILED: "失败", DEAD_LETTER: "已停止" } as Record<string, string>)[status] ?? status;
}
function notificationFailureMessage(code: string | null): string {
  return ({
    TG_FORBIDDEN: "Bot 被阻止或无权发送",
    TG_AUTH: "Bot Token 已失效",
    TG_NETWORK: "Telegram 网络暂不可达",
    TG_RATE_LIMIT: "Telegram 限流，等待重试",
    TG_UPSTREAM: "Telegram 服务暂不可用",
    TG_REJECTED: "Telegram 拒绝了消息",
    TG_NOT_CONFIGURED: "工作区 Bot 未配置",
    NOTIFICATION_CHANNEL_UNAVAILABLE: "通知通道不可用",
    SIGNAL_EXPIRED: "信号已过期，未推送",
    SIGNAL_INVALID_TIME: "信号时间异常，未推送",
    SIGNAL_EXPIRY_UNKNOWN: "信号有效期不明，未推送",
  } as Record<string, string>)[code ?? ""] ?? (code || "发送失败");
}
function exchangeStatus(status: ExchangeAccount["status"]): string {
  return ({ PENDING_VALIDATION: "验证中", READ_VALIDATED: "读取验证通过", TESTNET_READY: "TESTNET 已解锁", TESTNET_BUSY: "TESTNET 执行中", EXECUTION_FROZEN: "执行冻结", VALIDATION_FAILED: "验证失败", DISABLED: "已停用" })[status];
}
function exchangeWarning(code: string): string {
  return ({
    API_PERMISSION_SCOPE_UNVERIFIED: "Gate 未通过该接口返回完整 Key 权限；下单与提现权限均保持未验证。",
    IP_WHITELIST_MISSING: "未检测到固定 IP 白名单；请在 Gate 控制台设置验证进程的出口 IP。",
  } as Record<string, string>)[code] ?? `安全提示：${code}`;
}
function exchangeError(code: string): string {
  return ({
    GATE_AUTHENTICATION_FAILED: "认证失败，请检查是否使用 TESTNET Key、Secret 是否匹配。",
    GATE_RATE_LIMITED: "Gate 限流，系统会按 Retry-After 有界重试。",
    GATE_NETWORK_UNAVAILABLE: "网络暂不可达，系统会有界重试。",
    GATE_SERVICE_UNAVAILABLE: "Gate 服务暂不可用，系统会有界重试。",
    CREDENTIAL_DECRYPT_FAILED: "密钥版本不可用或密文完整性校验失败。",
    GATE_RESPONSE_INVALID: "Gate 返回了无法验证的响应。",
  } as Record<string, string>)[code] ?? code;
}
