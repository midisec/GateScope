import { useRef, useState, type FormEvent } from "react";
import { api, ApiError, jsonBody } from "./api";
import { encryptSubmission, wrapEncryptedSubmission } from "./submissionCrypto";
import type { Session } from "./types";
import { Dialog, PageState, Pagination, useConfirm } from "./ui";
import { useResearchResource } from "./useResearchResource";
import { ADMIN_LABELS, ADMIN_PATHS, ADMIN_ROLES, COUNT_LABELS, ROLE_LABELS,
  adminActionLabel, adminBytes, adminCreatePayload, adminListPath, adminNumber, adminStorageWarning, adminTime, adminView,
  type AdminAudit, type AdminCounts, type AdminMember, type AdminOverview, type AdminPage, type AdminResources, type AdminRole } from "./adminConsoleModel";
import "./adminConsole.css";

const message = (error: unknown) => error instanceof Error ? error.message : "操作失败，请稍后重试";
const stateName = (value: string) => ({ ACTIVE: "正常", LOCKED: "锁定", DISABLED: "停用", REVOKED: "已停用", SUCCESS: "成功", DENIED: "拒绝", FAILED: "失败" }[value] ?? value);
const roleName = (role: AdminRole) => `${ROLE_LABELS[role]} · ${role}`;

export function AdminConsole({ session }: { session: Session }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const lock = useRef(false), view = adminView(typeof location === "undefined" ? "/admin/overview" : location.pathname);
  async function logout() {
    if (lock.current) return; lock.current = true; setBusy(true); setError("");
    try { await api("/auth/logout", { method: "POST" }); location.assign("/login"); }
    catch (cause) { setError(message(cause)); } finally { lock.current = false; setBusy(false); }
  }
  if (session.user.role !== "OWNER" || session.mfa_required) return <main className="admin-denied"><h1>无法进入管理后台</h1><p>{session.mfa_required ? "请先完成登录安全验证。" : "此模块仅向工作区所有者开放。"}</p><a href="/markets/chart">返回交易台</a></main>;
  return <div className="admin-console"><header className="admin-topbar"><a className="admin-brand" href="/admin/overview">GateScope <span>管理后台</span></a><div><span className="admin-identity">{session.user.username}</span><a href="/markets/chart">返回交易台 ↗</a><button disabled={busy} onClick={() => void logout()}>{busy ? "退出中…" : "退出"}</button></div></header>
    <div className="admin-shell"><aside className="admin-sidebar"><nav aria-label="后台导航">{(Object.keys(ADMIN_PATHS) as Array<keyof typeof ADMIN_PATHS>).map(key => <a key={key} href={ADMIN_PATHS[key]} aria-current={key === view ? "page" : undefined}><span aria-hidden="true">{({ overview: "▦", users: "♙", resources: "▤", audit: "≡" })[key]}</span>{ADMIN_LABELS[key]}</a>)}</nav><small>当前工作区 · OWNER</small></aside>
      <main className="admin-main"><PageState error={error} />{view === "overview" && <AdminOverviewPage />}{view === "users" && <AdminUsersPage session={session} />}{view === "resources" && <AdminResourcesPage />}{view === "audit" && <AdminAuditPage />}{!view && <section className="admin-card"><h1>页面不存在</h1><a href="/admin/overview">返回管理概览</a></section>}</main>
    </div></div>;
}

function PageTitle({ title, children }: { title: string; children?: React.ReactNode }) { return <header className="admin-page-title"><h1>{title}</h1><div>{children}</div></header>; }
function ScopeNote({ data }: { data?: { scope: { tenant_name: string; tenant_slug: string }; generated_at: string } }) { return data ? <p className="admin-scope">{data.scope.tenant_name} · {data.scope.tenant_slug}<span>更新于 {adminTime(data.generated_at)}</span></p> : null; }
export function AdminCountGrid({ counts }: { counts: AdminCounts }) { return <div className="admin-count-grid">{Object.entries(counts).map(([key, value]) => <div key={key}><span>{COUNT_LABELS[key] ?? key}</span><strong>{adminNumber(value)}</strong></div>)}</div>; }
export function AdminLoginTrend({ trend }: { trend: AdminOverview["login_trend"] }) {
  const maximum = Math.max(1, ...trend.items.map(item => item.success + item.denied + item.failed));
  return <section className="admin-card"><div className="admin-section-title"><h2>近 7 天密码登录</h2><span title="仅密码步骤结果，不代表后续 MFA 已完成">按 UTC 日期 · 不含 MFA 结果</span></div><div className="admin-trend" role="img" aria-label="最近七天密码登录结果，按 UTC 日期统计，不代表 MFA 已完成">{trend.items.map(item => <div className="admin-trend-day" key={item.date}><strong>{adminNumber(item.success + item.denied + item.failed)}</strong><div className="admin-trend-track" title={`${item.date}：成功 ${item.success}，拒绝 ${item.denied}，失败 ${item.failed}`}>
      <i className="admin-trend-success" style={{ height: `${item.success / maximum * 100}%` }} /><i className="admin-trend-denied" style={{ height: `${item.denied / maximum * 100}%` }} /><i className="admin-trend-failed" style={{ height: `${item.failed / maximum * 100}%` }} /></div><time dateTime={item.date}>{item.date.slice(5)}</time></div>)}</div><div className="admin-trend-legend"><span>● 成功</span><span>● 拒绝</span><span>● 失败</span></div><details className="admin-details"><summary>查看每日数据</summary><div className="admin-table-wrap"><table><thead><tr><th>日期（UTC）</th><th>成功</th><th>拒绝</th><th>失败</th></tr></thead><tbody>{trend.items.map(item => <tr key={item.date}><td>{item.date}</td><td>{adminNumber(item.success)}</td><td>{adminNumber(item.denied)}</td><td>{adminNumber(item.failed)}</td></tr>)}</tbody></table></div></details></section>;
}
export function AdminOverviewPage() {
  const result = useResearchResource<AdminOverview>("/api/v2/admin/overview"), data = result.data;
  return <><PageTitle title="管理概览"><button disabled={result.loading} onClick={result.retry}>刷新</button></PageTitle><ScopeNote data={data} /><PageState loading={result.loading} error={result.error} onRetry={result.retry} />{data && <>
    <div className="admin-kpis"><article className="admin-card"><span>工作区成员</span><strong>{adminNumber(data.members.total)}</strong><small>正常 {adminNumber(data.members.active)} · 停用成员 {adminNumber(data.members.membership_revoked)}</small></article><article className="admin-card"><span>有效会话</span><strong>{adminNumber(data.sessions.active)}</strong><small>{adminNumber(data.sessions.active_users)} 位成员 · 含待验证会话，不代表在线</small></article><article className="admin-card"><span>运行中告警</span><strong>{adminNumber(data.business.active_alert_rules)}</strong><small>共 {adminNumber(data.business.alert_rules)} 条</small></article><article className="admin-card"><span>登录安全</span><strong>{adminNumber(data.members.mfa_required)}</strong><small>要求验证器 · 免强制 {adminNumber(data.members.mfa_optional)}</small></article></div>
    <AdminLoginTrend trend={data.login_trend} /><section className="admin-card"><h2>工作区业务资源</h2><AdminCountGrid counts={data.business} /></section></>}</>;
}

export function useAdminMutation(session: Session, onChanged: () => void) {
  const [busy, setBusy] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [reauthenticate, setReauthenticate] = useState(false), [passwordError, setPasswordError] = useState("");
  const lock = useRef(false);
  async function run(label: string, work: () => Promise<void>) {
    if (lock.current) return false; lock.current = true; setBusy(label); setError(""); setNotice("");
    try { await work(); onChanged(); return true; }
    catch (cause) { setError(cause instanceof ApiError && cause.status === 412 ? `${message(cause)}。请关闭弹窗、刷新用户列表后重新编辑；未自动覆盖现有权限。` : message(cause)); if (cause instanceof ApiError && ["RECENT_AUTH_REQUIRED", "RECENT_OWNER_AUTH_REQUIRED"].includes(cause.code)) { setReauthenticate(true); setPasswordError(""); } return false; }
    finally { lock.current = false; setBusy(""); }
  }
  async function confirmIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (lock.current) return;
    const form = event.currentTarget, password = String(new FormData(form).get("password") ?? ""); if (!password) return;
    form.reset(); lock.current = true; setBusy("验证中…"); setPasswordError("");
    try { const envelope = await encryptSubmission(`user:${session.user.id}:reauthenticate`, { password });
      await api("/auth/reauthenticate", { method: "POST", body: jsonBody(wrapEncryptedSubmission(envelope)) });
      setReauthenticate(false); setError(""); setNotice("身份已确认，请重新提交操作；未自动执行任何变更。");
    } catch (cause) { setPasswordError(message(cause)); } finally { lock.current = false; setBusy(""); }
  }
  return { busy, error, notice, run, setError, setNotice,
    dialog: reauthenticate ? <Dialog title="确认管理身份" busy={!!busy} onClose={() => setReauthenticate(false)}><form className="stack-form" onSubmit={confirmIdentity}><label>当前密码<input name="password" type="password" required autoComplete="current-password" autoFocus /></label>{passwordError && <p role="alert" className="error-banner">{passwordError}</p>}<button className="primary" disabled={!!busy}>{busy || "验证身份"}</button></form></Dialog> : null };
}

type UserModal = { kind: "create" } | { kind: "detail" | "edit"; member: AdminMember };
export function AdminUsersPage({ session }: { session: Session }) {
  const [filters, setFilters] = useState({ q: "", role: "ALL", status: "ALL", page: 1 }), [refresh, setRefresh] = useState(0);
  const [modal, setModal] = useState<UserModal>(), [formError, setFormError] = useState("");
  const result = useResearchResource<AdminPage<AdminMember>>(adminListPath("users", filters), refresh), confirm = useConfirm();
  const action = useAdminMutation(session, () => setRefresh(value => value + 1));
  function open(value: UserModal) { setFormError(""); action.setError(""); setModal(value); }
  function search(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const form = new FormData(event.currentTarget); setFilters({ q: String(form.get("q") ?? "").trim(), role: String(form.get("role") ?? "ALL"), status: String(form.get("status") ?? "ALL"), page: 1 }); }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (action.busy) return; setFormError("");
    const form = event.currentTarget;
    try { const payload = adminCreatePayload(new FormData(form));
      const passwordField = form.elements.namedItem("initial_password") as HTMLInputElement | null; if (passwordField) passwordField.value = "";
      if (payload.role === "OWNER" && !await confirm.ask("授予所有者权限", "新用户将能管理工作区成员与敏感接入。确认授予 OWNER？")) return;
      await action.run("创建中…", async () => {
        const envelope = await encryptSubmission(`tenant:${session.user.tenant_id}:user_create`, payload);
        await api("/api/v2/users", { method: "POST", body: jsonBody(wrapEncryptedSubmission(envelope)) });
        setModal(undefined); action.setNotice(`已创建 ${payload.username}，首次登录需要绑定验证器。`);
      });
    } catch (cause) { setFormError(message(cause)); }
  }
  async function edit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (action.busy || modal?.kind !== "edit") return;
    const member = modal.member, form = new FormData(event.currentTarget), role = String(form.get("role")), status = String(form.get("status"));
    if (!ADMIN_ROLES.includes(role as AdminRole) || !["ACTIVE", "REVOKED"].includes(status)) { setFormError("请选择有效角色与状态"); return; }
    if (role === member.membership.role && status === member.membership.status) { setModal(undefined); return; }
    if (member.is_self && status !== "ACTIVE") { setFormError("不能在此停用当前管理账号"); return; }
    if (!await confirm.ask("确认成员权限变更", `${member.username}：${roleName(role as AdminRole)}，${status === "ACTIVE" ? "启用" : "停用"}工作区成员资格。${role === "OWNER" ? " OWNER 可以管理敏感接入。" : ""}`)) return;
    await action.run("保存中…", async () => { await api(`/api/v2/memberships/${encodeURIComponent(member.membership.id)}`, { method: "PATCH", headers: { "If-Match": `"${member.membership.etag}"` }, body: jsonBody({ role, status }) }); setModal(undefined); action.setNotice("成员权限已更新"); if (member.is_self && role !== "OWNER") location.assign("/markets/chart"); });
  }
  async function revoke(member: AdminMember) {
    if (action.busy || member.is_self) return;
    if (!await confirm.ask("注销该用户的会话", `确定注销 ${member.username} 的所有有效会话？对方需要重新登录，不会删除账户或业务记录。`)) return;
    await action.run("注销中…", async () => { const response = await api<{ revoked_sessions: number }>(`/api/v2/admin/users/${encodeURIComponent(member.id)}/sessions:revoke`, { method: "POST" }); setModal(undefined); action.setNotice(`已注销 ${adminNumber(response.revoked_sessions)} 个会话`); });
  }
  return <><PageTitle title="用户管理"><button disabled={result.loading || !!action.busy} onClick={result.retry}>刷新</button><button className="primary" disabled={!!action.busy} onClick={() => open({ kind: "create" })}>创建用户</button></PageTitle><ScopeNote data={result.data} />
    <form className="admin-filter" onSubmit={search}><label>搜索用户<input name="q" type="search" maxLength={80} placeholder="用户名" defaultValue={filters.q} /></label><label>角色<select name="role" defaultValue={filters.role}><option value="ALL">全部角色</option>{ADMIN_ROLES.map(role => <option key={role} value={role}>{roleName(role)}</option>)}</select></label><label>用户状态<select name="status" defaultValue={filters.status}><option value="ALL">全部状态</option>{["ACTIVE", "LOCKED", "DISABLED", "REVOKED"].map(state => <option key={state} value={state}>{state === "REVOKED" ? "工作区已停用" : stateName(state)}</option>)}</select></label><button type="submit" disabled={result.loading}>查询</button></form>
    <PageState loading={result.loading} error={result.error} onRetry={result.retry} />{action.error && <p className="error-banner" role="alert">{action.error}</p>}{action.notice && <p className="admin-notice" role="status">{action.notice}</p>}
    {result.data && <section className="admin-card"><div className="admin-table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>账号 / 成员状态</th><th>登录验证</th><th>最后密码登录</th><th title="含待验证会话，不代表在线">有效会话</th><th>操作</th></tr></thead><tbody>{result.data.items.map(member => <tr key={member.id}><td><strong>{member.username}</strong>{member.is_self && <small>当前账号</small>}</td><td>{ROLE_LABELS[member.membership.role]}</td><td><span className={`admin-status ${member.status.toLowerCase()}`}>{stateName(member.status)}</span><small>成员：{member.membership.status === "ACTIVE" ? "已启用" : "已停用"}</small></td><td>{member.mfa_required ? "需要验证器" : "非强制验证器"}</td><td>{adminTime(member.last_login_at)}</td><td>{adminNumber(member.active_sessions)}</td><td><div className="admin-row-actions"><button disabled={!!action.busy} onClick={() => open({ kind: "detail", member })}>详情</button><button disabled={!!action.busy} onClick={() => open({ kind: "edit", member })}>编辑</button><button disabled={!!action.busy || member.is_self || member.active_sessions === 0} onClick={() => void revoke(member)}>注销会话</button></div></td></tr>)}</tbody></table></div>{!result.data.items.length && <p className="admin-empty">没有符合条件的用户</p>}<Pagination label="用户" page={filters.page - 1} pages={Math.max(1, result.data.total_pages)} total={result.data.total} busy={result.loading || !!action.busy} onChange={page => setFilters(value => ({ ...value, page: page + 1 }))} /></section>}
    {modal && <Dialog key={modal.kind === "create" ? "create" : `${modal.kind}:${modal.member.id}`} title={modal.kind === "create" ? "创建用户" : modal.kind === "edit" ? `编辑成员 · ${modal.member.username}` : `用户详情 · ${modal.member.username}`} busy={!!action.busy} onClose={() => setModal(undefined)}>
      {modal.kind === "detail" ? <AdminMemberDetail member={modal.member} generatedAt={result.data?.generated_at} /> : <form className="stack-form admin-form" onSubmit={modal.kind === "create" ? create : edit}>
        {modal.kind === "create" && <><label>用户名<input name="username" required minLength={3} maxLength={64} autoComplete="off" autoFocus /></label><label>初始密码<input name="initial_password" type="password" required minLength={16} maxLength={256} autoComplete="new-password" /></label><small>至少 16 个字符，UTF-8 编码不超过 256 字节；首次登录必须绑定验证器。</small></>}
        <label>角色<select name="role" defaultValue={modal.kind === "edit" ? modal.member.membership.role : "RESEARCHER"}>{ADMIN_ROLES.map(role => <option key={role} value={role}>{roleName(role)}</option>)}</select></label>
        {modal.kind === "edit" && <><label>工作区成员资格<select name="status" defaultValue={modal.member.membership.status}><option value="ACTIVE">启用</option><option value="REVOKED" disabled={modal.member.is_self}>停用</option></select></label><small>修改当前工作区权限；不删除账户、历史数据或修改登录验证策略。</small></>}
        {formError && <p className="error-banner" role="alert">{formError}</p>}{action.error && <p className="error-banner" role="alert">{action.error}</p>}<div className="dialog-actions"><button type="button" disabled={!!action.busy} onClick={() => setModal(undefined)}>取消</button><button className="primary" disabled={!!action.busy}>{action.busy || (modal.kind === "create" ? "创建用户" : "保存变更")}</button></div>
      </form>}
    </Dialog>}{confirm.dialog}{action.dialog}</>;
}
export function AdminMemberDetail({ member, generatedAt }: { member: AdminMember; generatedAt?: string }) { return <div className="admin-member-detail"><dl><div><dt>用户名</dt><dd>{member.username}</dd></div><div><dt>角色</dt><dd>{roleName(member.membership.role)}</dd></div><div><dt>账号状态</dt><dd>{stateName(member.status)}</dd></div><div><dt>工作区成员</dt><dd>{member.membership.status === "ACTIVE" ? "已启用" : "已停用"}</dd></div><div><dt>登录验证器策略</dt><dd>{member.mfa_required ? "强制" : "非强制（不代表已完成 MFA）"}</dd></div><div><dt>创建时间</dt><dd>{adminTime(member.created_at)}</dd></div><div><dt>最后密码登录</dt><dd>{adminTime(member.last_login_at)}</dd></div><div><dt>最后活动</dt><dd>{adminTime(member.last_seen_at)}</dd></div><div><dt title="含待验证会话，不代表在线">有效会话</dt><dd>{adminNumber(member.active_sessions)}</dd></div></dl><h3>用户业务资源</h3><AdminCountGrid counts={member.resources} /><small>统计快照：{adminTime(generatedAt)}</small></div>; }

export function AdminPhysicalResources({ physical }: { physical: AdminResources["physical"] }) { const disk = physical.disk; return <>
  <section className="admin-card"><div className="admin-section-title"><h2>共享数据库物理资源</h2><span className="admin-scope-badge">全库共享 · 非用户占用</span></div><p className="admin-resource-total">数据库总大小 <strong>{physical.database_bytes == null ? "暂不可用" : adminBytes(physical.database_bytes)}</strong></p><div className="admin-table-wrap"><table><thead><tr><th>数据表</th><th>总大小（含索引）</th><th>估算行数</th><th>范围</th></tr></thead><tbody>{physical.tables.map(table => <tr key={table.table}><td><code>{table.table}</code></td><td>{table.available && table.total_bytes != null ? adminBytes(table.total_bytes) : "暂不可用"}</td><td>{table.available && table.estimated_rows != null ? adminNumber(table.estimated_rows) : "暂不可用"}</td><td>含后代分片</td></tr>)}</tbody></table></div><small>行数来自数据库统计估算，不是精确计数，也不能归因于当前用户。</small>{physical.warnings.length > 0 && <details className="admin-details"><summary>统计口径与限制（{physical.warnings.length}）</summary><ul>{physical.warnings.map((warning, index) => <li key={index}>{adminStorageWarning(warning)}{adminStorageWarning(warning) !== warning && <small>（{warning}）</small>}</li>)}</ul></details>}</section>
  <section className="admin-card"><div className="admin-section-title"><h2 title="应用主机的文件系统不一定与数据库磁盘相同">应用主机文件系统</h2><span className="admin-scope-badge">主机共享 · 非用户占用</span></div>{disk.available ? <><div className="admin-count-grid"><div><span>容量</span><strong>{adminBytes(disk.total_bytes)}</strong></div><div><span>已用</span><strong>{adminBytes(disk.used_bytes)}</strong></div><div><span>可用</span><strong>{adminBytes(disk.free_bytes)}</strong></div></div>{disk.used_percent != null && Number.isFinite(disk.used_percent) && <label className="admin-disk-meter">已用 {disk.used_percent.toFixed(1)}%<progress value={disk.used_percent} max={100} /></label>}</> : <p className="admin-empty">文件系统统计暂不可用</p>}</section></>; }
export function AdminResourcesPage() { const result = useResearchResource<AdminResources>("/api/v2/admin/resources"); return <><PageTitle title="资源统计"><button disabled={result.loading} onClick={result.retry}>刷新</button></PageTitle><ScopeNote data={result.data} /><PageState loading={result.loading} error={result.error} onRetry={result.retry} />{result.data && <><section className="admin-card"><div className="admin-section-title"><h2>当前工作区业务计数</h2><span>不等同于磁盘大小</span></div><AdminCountGrid counts={result.data.business.counts} /></section><AdminPhysicalResources physical={result.data.physical} /></>}</>; }

export function AdminAuditPage() {
  const [filters, setFilters] = useState({ action: "", outcome: "ALL", page: 1 });
  const result = useResearchResource<AdminPage<AdminAudit>>(adminListPath("audit", filters));
  function search(event: FormEvent<HTMLFormElement>) { event.preventDefault(); const data = new FormData(event.currentTarget); setFilters({ action: String(data.get("action") ?? "").trim(), outcome: String(data.get("outcome") ?? "ALL"), page: 1 }); }
  return <><PageTitle title="操作审计"><button disabled={result.loading} onClick={result.retry}>刷新</button></PageTitle><ScopeNote data={result.data} /><form className="admin-filter" onSubmit={search}><label>操作名称<input name="action" type="search" maxLength={80} placeholder="例如 user.create" /></label><label>结果<select name="outcome"><option value="ALL">全部结果</option>{["SUCCESS", "DENIED", "FAILED"].map(value => <option value={value} key={value}>{stateName(value)}</option>)}</select></label><button disabled={result.loading}>查询</button></form><PageState loading={result.loading} error={result.error} onRetry={result.retry} />{result.data && <section className="admin-card"><div className="admin-table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>操作者</th><th>关联用户</th><th>结果</th><th>追踪编号</th></tr></thead><tbody>{result.data.items.map(event => <tr key={event.id}><td>{adminTime(event.occurred_at)}</td><td>{adminActionLabel(event.action)}<small><code>{event.action}</code></small></td><td>{event.actor_username ?? "系统 / 未识别"}</td><td>{event.subject_username ?? "—"}</td><td><span className={`admin-status ${event.outcome.toLowerCase()}`}>{stateName(event.outcome)}</span></td><td><code className="admin-trace">{event.trace_id || "—"}</code></td></tr>)}</tbody></table></div>{!result.data.items.length && <p className="admin-empty">没有符合条件的审计记录</p>}<Pagination label="审计" page={filters.page - 1} pages={Math.max(1, result.data.total_pages)} total={result.data.total} busy={result.loading} onChange={page => setFilters(value => ({ ...value, page: page + 1 }))} /></section>}</>;
}
