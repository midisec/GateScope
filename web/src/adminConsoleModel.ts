export const ADMIN_ROLES = ["VIEWER", "RESEARCHER", "OPERATOR", "OWNER"] as const;
export type AdminRole = typeof ADMIN_ROLES[number];
export type AdminView = "overview" | "users" | "resources" | "audit";
export const ADMIN_PATHS: Record<AdminView, string> = { overview: "/admin/overview", users: "/admin/users", resources: "/admin/resources", audit: "/admin/audit" };
export const ADMIN_LABELS: Record<AdminView, string> = { overview: "管理概览", users: "用户管理", resources: "资源统计", audit: "操作审计" };
export const ROLE_LABELS: Record<AdminRole, string> = { VIEWER: "观察者", RESEARCHER: "研究员", OPERATOR: "操作员", OWNER: "所有者" };
export const ADMIN_PAGE_SIZE = 20;
export type AdminScope = { type: "CURRENT_TENANT"; tenant_id: string; tenant_slug: string; tenant_name: string };
export type AdminEnvelope = { scope: AdminScope; generated_at: string; trace_id: string };
export type AdminPage<T> = AdminEnvelope & { items: T[]; page: number; page_size: number; total: number; total_pages: number; has_more: boolean };
export type AdminCounts = Record<string, number | null>;
export type AdminMember = { id: string; username: string; normalized_username: string; status: "ACTIVE" | "LOCKED" | "DISABLED";
  mfa_required: boolean; etag: number; created_at: string; last_login_at: string | null; last_seen_at: string | null; active_sessions: number; is_self: boolean;
  membership: { id: string; role: AdminRole; status: "ACTIVE" | "REVOKED"; etag: number }; resources: AdminCounts };
export type AdminOverview = AdminEnvelope & { members: { total: number; active: number; locked: number; disabled: number; membership_revoked: number; mfa_required: number; mfa_optional: number };
  sessions: { active: number; active_users: number; includes_pending_mfa?: true }; business: AdminCounts;
  login_trend: { timezone: "UTC"; basis?: "RECORDED_PASSWORD_LOGIN_EVENTS"; from_date: string; to_date: string; items: { date: string; success: number; denied: number; failed: number }[] } };
export type AdminAudit = { id: string; occurred_at: string; action: string; outcome: string; actor_user_id: string | null; actor_username: string | null; subject_user_id: string | null; subject_username: string | null; trace_id: string | null };
export type AdminResources = AdminEnvelope & { business: { scope: "CURRENT_TENANT"; counts: AdminCounts };
  physical: { scope: "SHARED_DATABASE"; not_per_user: true; database_bytes: number | null; row_count_kind: "ESTIMATE_NOT_EXACT";
    tables: { table: string; total_bytes: number | null; estimated_rows: number | null; includes_descendants: true; available: boolean }[];
    disk: { scope: "APPLICATION_HOST_FILESYSTEM"; total_bytes: number | null; used_bytes: number | null; free_bytes: number | null; used_percent: number | null; available: boolean }; warnings: string[] } };
export const COUNT_LABELS: Record<string, string> = { watchlists: "自选列表", watchlist_items: "自选标的", chart_layouts: "图表布局", indicator_templates: "指标模板", alert_rules: "告警策略", active_alert_rules: "运行中告警", paused_alert_rules: "暂停告警", paper_accounts: "模拟账户", paper_orders: "模拟委托", paper_positions: "模拟仓位", paper_fills: "模拟成交", research_backtests: "历史回测", backtests: "历史回测", mcp_clients: "MCP 客户端", notification_channels: "通知通道", telegram_connections: "Telegram 绑定" };
export function adminActionLabel(action: string) { return ({ "auth.login": "账号登录", "auth.reauthenticate": "密码再次认证", "password.reauthenticate": "密码再次认证", "password.change": "修改密码", "user.create": "创建用户", "membership.update": "修改成员权限", "session.revoke": "注销会话", "admin.sessions.revoke": "管理员注销用户会话", "admin.user.sessions.revoke": "管理员注销用户会话", "mfa.confirm": "确认绑定验证器", "mfa.verify": "验证动态码", "mfa.remove": "移除验证器", "recovery.regenerate": "生成恢复码", "recovery.consume": "使用恢复码" } as Record<string, string>)[action] ?? "其他操作"; }
export function adminStorageWarning(value: string) { return ({ EXTERNAL_COMPRESSION_STORAGE_NOT_ATTRIBUTED: "外置或压缩存储未完整归入各表大小；表大小之和可能与数据库总大小不同。", DATABASE_SIZE_UNAVAILABLE: "数据库总大小暂不可用。", TABLE_CATALOG_BUDGET_EXCEEDED: "数据表目录超过本次统计预算，仅展示已完成的部分。", TABLE_STORAGE_UNAVAILABLE: "部分数据表大小暂不可用。", APPLICATION_DISK_UNAVAILABLE: "应用主机文件系统统计暂不可用。" } as Record<string, string>)[value] ?? (/^[A-Z0-9_]+$/.test(value) ? "存在未识别的统计限制，请核对状态码。" : value); }
export function adminView(path: string): AdminView | undefined {
  if (path === "/admin" || path === "/admin/") return "overview";
  return (Object.keys(ADMIN_PATHS) as AdminView[]).find(key => ADMIN_PATHS[key] === path.replace(/\/$/, ""));
}
export function adminListPath(kind: "users" | "audit", filters: { page: number; q?: string; role?: string; status?: string; action?: string; outcome?: string }) {
  const query = new URLSearchParams({ page: String(Math.max(1, Math.floor(filters.page) || 1)), page_size: String(ADMIN_PAGE_SIZE) });
  for (const key of kind === "users" ? ["q", "role", "status"] as const : ["action", "outcome"] as const) {
    const value = filters[key]?.trim(); if (value && value !== "ALL") query.set(key, value.slice(0, 80));
  }
  return `/api/v2/admin/${kind}?${query}`;
}
export function adminNumber(value: number | null | undefined) { return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value.toLocaleString("zh-CN") : "—"; }
export function adminBytes(value: number | string | null | undefined) {
  if (value == null || value === "") return "—";
  const bytes = Number(value); if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** unit).toFixed(1)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}
export function adminTime(value: string | null | undefined) {
  if (!value) return "—"; const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "—";
}
export function adminCreatePayload(values: Pick<FormData, "get">) {
  const username = String(values.get("username") ?? "").trim(), initial_password = String(values.get("initial_password") ?? ""), role = String(values.get("role") ?? "RESEARCHER");
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$/.test(username.normalize("NFKC").trim())) throw new Error("用户名需以字母或数字开头，共 3–64 位，可含点、横线和下划线");
  const bytes = new TextEncoder().encode(initial_password).length;
  if (initial_password.length < 16 || bytes > 256) throw new Error("初始密码至少 16 个字符，UTF-8 编码不超过 256 字节");
  if (!ADMIN_ROLES.includes(role as AdminRole)) throw new Error("请选择有效角色");
  return { username, initial_password, role: role as AdminRole };
}
