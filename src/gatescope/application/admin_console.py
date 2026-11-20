"""Current-workspace OWNER administration, never a cross-tenant superuser API.

Identity queries use the existing identity pool; business counts use the API
pool. Both retain RLS and additionally bind tenant IDs explicitly. Shared market
storage is separately labelled and measured from catalogs, not by counting bars.
"""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from gatescope.domain.identity import Role

BUSINESS_KEYS = ("watchlists", "watchlist_items", "chart_layouts", "indicator_templates", "alert_rules",
                 "active_alert_rules", "paused_alert_rules", "paper_accounts")
USER_RESOURCE_KEYS = ("watchlists", "watchlist_items", "alert_rules", "active_alert_rules", "paper_accounts")
SHARED_TABLES = ("instruments", "instrument_revisions", "bars", "market_sync_state", "market_live_candles",
                 "market_chart_snapshots", "market_pattern_events", "market_pattern_reports",
                 "market_pattern_tracking", "market_pattern_studies", "market_pattern_report_versions",
                 "market_structure_stages", "market_structure_returns", "research_backtests",
                 "research_backtest_results", "research_structure_checkpoints", "research_structure_inputs",
                 "research_structure_events", "research_structure_cases", "research_discovery_reports",
                 "research_indicator_chunks", "research_portfolios", "research_portfolio_chunks")
USER_STATUSES = {"ALL", "ACTIVE", "LOCKED", "DISABLED", "REVOKED"}
ROLES = {"ALL", *[role.value for role in Role]}
OUTCOMES = {"ALL", "SUCCESS", "DENIED", "FAILED"}


class AdminConsoleError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(code)


def require_owner(actor, *, recent=False, now=None):
    if actor is None:
        raise AdminConsoleError(401, "AUTH_REQUIRED", "请先登录")
    if not getattr(actor, "fully_authenticated", False):
        raise AdminConsoleError(403, "MFA_REQUIRED", "请先完成多因素验证")
    if actor.role != Role.OWNER:
        raise AdminConsoleError(403, "ADMIN_OWNER_REQUIRED", "仅当前工作区所有者可以访问管理后台")
    current = now or datetime.now(timezone.utc)
    authenticated = getattr(actor, "recent_auth_at", None)
    if recent and (not isinstance(authenticated, datetime) or authenticated.tzinfo is None
                   or not current-timedelta(minutes=10) <= authenticated <= current+timedelta(seconds=5)):
        raise AdminConsoleError(403, "RECENT_AUTH_REQUIRED", "请再次验证当前密码后继续")


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else None


def _visible_user_status(status, locked_until, now):
    # Failed password attempts temporarily lock future logins without changing
    # users.status or invalidating sessions which are already authenticated.
    return "LOCKED" if status == "ACTIVE" and locked_until is not None and locked_until > now else status


def _page(page, page_size):
    if type(page) is not int or type(page_size) is not int or not 1 <= page <= 10_000 or not 1 <= page_size <= 100:
        raise AdminConsoleError(422, "INVALID_PAGE", "分页参数无效")
    return (page-1)*page_size


def _pagination(items, total, page, page_size):
    return {"items": items, "page": page, "page_size": page_size, "total": total,
            "total_pages": (total+page_size-1)//page_size, "has_more": page*page_size < total}


def _like_literal(value):
    return "%"+value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")+"%"


class AdminConsoleService:
    def __init__(self, api_pool, identity, *, disk_path=None):
        self.api_pool, self.identity = api_pool, identity
        self.auth_pool = identity.pool
        # This measures the application's filesystem, NOT an asserted DB volume.
        self.disk_path = Path(disk_path) if disk_path is not None else Path(__file__).resolve().parents[3]

    @contextmanager
    def _read(self, pool, actor):
        with pool.read_transaction(actor.tenant_id, actor.user_id) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            yield connection

    @staticmethod
    def _scope(connection, actor):
        row = connection.execute("""SELECT t.id,t.slug,t.name FROM tenants t
          JOIN memberships m ON m.tenant_id=t.id AND m.user_id=%s
          JOIN users u ON u.id=m.user_id
          WHERE t.id=%s AND t.status='ACTIVE' AND m.status='ACTIVE' AND m.role='OWNER' AND u.status='ACTIVE'""",
          (actor.user_id, actor.tenant_id)).fetchone()
        if row is None:
            raise AdminConsoleError(403, "ADMIN_OWNER_REQUIRED", "当前工作区管理权限已失效")
        return {"type": "CURRENT_TENANT", "tenant_id": str(row[0]), "tenant_slug": row[1], "tenant_name": row[2]}

    def _business(self, actor):
        # These foundation tables have tenant RLS. Do not append owner-only
        # order/research tables and mislabel the OWNER's rows as tenant totals.
        queries: list[str] = []
        params: list = []
        for key, table, predicate in (
            ("watchlists", "watchlists", "AND deleted_at IS NULL"),
            ("chart_layouts", "chart_layouts", "AND deleted_at IS NULL"),
            ("indicator_templates", "indicator_templates", "AND deleted_at IS NULL"),
            ("alert_rules", "alert_rules", "AND status<>'DELETED'"),
            ("active_alert_rules", "alert_rules", "AND status='ACTIVE'"),
            ("paused_alert_rules", "alert_rules", "AND status='PAUSED'"),
            ("paper_accounts", "paper_accounts", ""),
        ):
            queries.append(f"SELECT '{key}' AS resource,count(*)::bigint AS count FROM {table} WHERE tenant_id=%s {predicate}")
            params.append(actor.tenant_id)
        queries.append("""SELECT 'watchlist_items',count(*)::bigint FROM watchlist_items wi
          JOIN watchlists w ON w.id=wi.watchlist_id AND w.tenant_id=wi.tenant_id
          WHERE wi.tenant_id=%s AND w.deleted_at IS NULL""")
        params.append(actor.tenant_id)
        with self._read(self.api_pool, actor) as connection:
            rows = connection.execute(" UNION ALL ".join(queries), params).fetchall()
        result = {str(key): int(count) for key, count in rows}
        if set(result) != set(BUSINESS_KEYS):
            raise AdminConsoleError(503, "ADMIN_STATS_UNAVAILABLE", "工作区资源统计暂不可用")
        return result

    def overview(self, actor):
        require_owner(actor)
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=6)
        with self._read(self.auth_pool, actor) as connection:
            scope = self._scope(connection, actor)
            member_row = connection.execute("""SELECT count(*),
              count(*) FILTER(WHERE u.status='ACTIVE' AND m.status='ACTIVE'
                AND (u.locked_until IS NULL OR u.locked_until<=%s)),
              count(*) FILTER(WHERE u.status='LOCKED' OR (u.status='ACTIVE' AND u.locked_until>%s)),
              count(*) FILTER(WHERE u.status='DISABLED'),
              count(*) FILTER(WHERE m.status='REVOKED'),count(*) FILTER(WHERE u.mfa_required),
              count(*) FILTER(WHERE NOT u.mfa_required)
              FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.tenant_id=%s""", (now, now, actor.tenant_id)).fetchone()
            session_row = connection.execute("""SELECT count(*),count(DISTINCT s.user_id),
              count(*) FILTER(WHERE s.last_seen_at>=%s)
              FROM sessions s JOIN memberships m ON m.tenant_id=s.tenant_id AND m.user_id=s.user_id
              JOIN users u ON u.id=s.user_id WHERE s.tenant_id=%s AND s.revoked_at IS NULL AND s.expires_at>%s
              AND m.status='ACTIVE' AND u.status='ACTIVE'""", (now-timedelta(minutes=15), actor.tenant_id, now)).fetchone()
            trend = connection.execute("""SELECT (occurred_at AT TIME ZONE 'UTC')::date,
              count(*) FILTER(WHERE outcome='SUCCESS'),count(*) FILTER(WHERE outcome='DENIED'),
              count(*) FILTER(WHERE outcome='FAILED') FROM identity_audit_facts
              WHERE tenant_id=%s AND action='auth.login' AND occurred_at>=%s AND occurred_at<=%s
              GROUP BY 1 ORDER BY 1""", (actor.tenant_id, start, now)).fetchall()
        by_day = {str(row[0]): row[1:] for row in trend}
        days = []
        for index in range(7):
            date = (start+timedelta(days=index)).date().isoformat()
            success, denied, failed = by_day.get(date, (0, 0, 0))
            days.append({"date": date, "success": int(success), "denied": int(denied), "failed": int(failed)})
        return {"scope": scope, "generated_at": now.isoformat(),
                "members": dict(zip(("total", "active", "locked", "disabled", "membership_revoked", "mfa_required", "mfa_optional"),
                                    map(int, member_row), strict=True)),
                "sessions": {"active": int(session_row[0]), "active_users": int(session_row[1]),
                             "seen_within_15m": int(session_row[2]), "includes_pending_mfa": True},
                "login_trend": {"timezone": "UTC", "basis": "RECORDED_PASSWORD_LOGIN_EVENTS",
                                "from_date": start.date().isoformat(), "to_date": now.date().isoformat(), "items": days},
                "business": self._business(actor)}

    def _user_resources(self, actor, users):
        result = {str(user): dict.fromkeys(USER_RESOURCE_KEYS, 0) for user in users}
        if not users:
            return result
        queries: list[str] = []
        params: list = []
        for key, table, predicate in (
            ("watchlists", "watchlists", "AND deleted_at IS NULL"),
            ("alert_rules", "alert_rules", "AND status<>'DELETED'"),
            ("active_alert_rules", "alert_rules", "AND status='ACTIVE'"),
            ("paper_accounts", "paper_accounts", ""),
        ):
            queries.append(f"SELECT owner_user_id,'{key}',count(*)::bigint FROM {table} "
                           f"WHERE tenant_id=%s AND owner_user_id=ANY(%s::uuid[]) {predicate} GROUP BY owner_user_id")
            params.extend((actor.tenant_id, users))
        queries.append("""SELECT w.owner_user_id,'watchlist_items',count(*)::bigint FROM watchlist_items wi
          JOIN watchlists w ON w.id=wi.watchlist_id AND w.tenant_id=wi.tenant_id
          WHERE wi.tenant_id=%s AND w.owner_user_id=ANY(%s::uuid[]) AND w.deleted_at IS NULL GROUP BY w.owner_user_id""")
        params.extend((actor.tenant_id, users))
        with self._read(self.api_pool, actor) as connection:
            rows = connection.execute(" UNION ALL ".join(queries), params).fetchall()
        for owner, resource, count in rows:
            if str(owner) in result and resource in USER_RESOURCE_KEYS:
                result[str(owner)][resource] = int(count)
        return result

    def users(self, actor, *, q="", status="ALL", role="ALL", page=1, page_size=20):
        require_owner(actor)
        offset = _page(page, page_size)
        if not isinstance(q, str) or len(q) > 80 or status not in USER_STATUSES or role not in ROLES:
            raise AdminConsoleError(422, "INVALID_USER_FILTER", "成员筛选参数无效")
        now = datetime.now(timezone.utc)
        clauses, params = ["m.tenant_id=%s"], [actor.tenant_id]
        if q.strip():
            clauses.append("(u.normalized_username ILIKE %s ESCAPE E'\\\\' OR u.display_username ILIKE %s ESCAPE E'\\\\')")
            params.extend((_like_literal(q.strip()), _like_literal(q.strip())))
        if status == "REVOKED":
            clauses.append("m.status='REVOKED'")
        elif status == "ACTIVE":
            clauses.append("u.status='ACTIVE' AND m.status='ACTIVE' AND (u.locked_until IS NULL OR u.locked_until<=%s)")
            params.append(now)
        elif status == "LOCKED":
            clauses.append("(u.status='LOCKED' OR (u.status='ACTIVE' AND u.locked_until>%s))")
            params.append(now)
        elif status != "ALL":
            clauses.append("u.status=%s")
            params.append(status)
        if role != "ALL":
            clauses.append("m.role=%s")
            params.append(role)
        predicate = " AND ".join(clauses)
        with self._read(self.auth_pool, actor) as connection:
            scope = self._scope(connection, actor)
            total = int(connection.execute(f"SELECT count(*) FROM memberships m JOIN users u ON u.id=m.user_id WHERE {predicate}", params).fetchone()[0])
            rows = connection.execute(f"""SELECT u.id,u.display_username,u.normalized_username,u.status,u.mfa_required,u.etag,
              u.created_at,m.id,m.role,m.status,m.etag,u.locked_until FROM memberships m JOIN users u ON u.id=m.user_id
              WHERE {predicate} ORDER BY u.normalized_username,u.id LIMIT %s OFFSET %s""", [*params, page_size, offset]).fetchall()
            identifiers = [row[0] for row in rows]
            sessions = connection.execute("""SELECT s.user_id,max(s.authenticated_at),max(s.last_seen_at),
              count(*) FILTER(WHERE s.revoked_at IS NULL AND s.expires_at>%s AND m.status='ACTIVE' AND u.status='ACTIVE')
              FROM sessions s JOIN memberships m ON m.tenant_id=s.tenant_id AND m.user_id=s.user_id
              JOIN users u ON u.id=s.user_id WHERE s.tenant_id=%s AND s.user_id=ANY(%s::uuid[]) GROUP BY s.user_id""",
              (now, actor.tenant_id, identifiers)).fetchall() if identifiers else []
        session_by_user = {str(row[0]): row[1:] for row in sessions}
        resources = self._user_resources(actor, identifiers)
        items = []
        for row in rows:
            last_login, last_seen, active = session_by_user.get(str(row[0]), (None, None, 0))
            items.append({"id": str(row[0]), "username": row[1], "normalized_username": row[2],
                          "status": _visible_user_status(row[3], row[11], now), "locked_until": _iso(row[11]),
                          "mfa_required": bool(row[4]), "etag": int(row[5]), "created_at": _iso(row[6]),
                          "last_login_at": _iso(last_login), "last_seen_at": _iso(last_seen), "active_sessions": int(active),
                          "is_self": str(row[0]) == str(actor.user_id),
                          "membership": {"id": str(row[7]), "role": row[8], "status": row[9], "etag": int(row[10])},
                          "resources": resources[str(row[0])]})
        return {"scope": scope, "generated_at": now.isoformat(), **_pagination(items, total, page, page_size)}

    def audit(self, actor, *, action="", outcome="ALL", page=1, page_size=20):
        require_owner(actor)
        offset = _page(page, page_size)
        if not isinstance(action, str) or len(action) > 80 or outcome not in OUTCOMES:
            raise AdminConsoleError(422, "INVALID_AUDIT_FILTER", "审计筛选参数无效")
        clauses, params = ["a.tenant_id=%s"], [actor.tenant_id]
        if action.strip():
            clauses.append("a.action=%s")
            params.append(action.strip())
        if outcome != "ALL":
            clauses.append("a.outcome=%s")
            params.append(outcome)
        predicate = " AND ".join(clauses)
        with self._read(self.auth_pool, actor) as connection:
            scope = self._scope(connection, actor)
            total = int(connection.execute(f"SELECT count(*) FROM identity_audit_facts a WHERE {predicate}", params).fetchone()[0])
            rows = connection.execute(f"""SELECT a.id,a.occurred_at,a.action,a.outcome,a.actor_user_id,au.display_username,
              a.subject_user_id,su.display_username,a.trace_id FROM identity_audit_facts a
              LEFT JOIN memberships am ON am.tenant_id=a.tenant_id AND am.user_id=a.actor_user_id
              LEFT JOIN users au ON au.id=am.user_id
              LEFT JOIN memberships sm ON sm.tenant_id=a.tenant_id AND sm.user_id=a.subject_user_id
              LEFT JOIN users su ON su.id=sm.user_id
              WHERE {predicate} ORDER BY a.occurred_at DESC,a.id DESC LIMIT %s OFFSET %s""", [*params, page_size, offset]).fetchall()
        # Deliberate field projection: never select detail_json, hashes, tokens,
        # keys, password history, recovery codes or session authentication data.
        items = [{"id": str(row[0]), "occurred_at": _iso(row[1]), "action": row[2], "outcome": row[3],
                  "actor_user_id": str(row[4]) if row[4] else None, "actor_username": row[5],
                  "subject_user_id": str(row[6]) if row[6] else None, "subject_username": row[7], "trace_id": row[8]} for row in rows]
        return {"scope": scope, "generated_at": datetime.now(timezone.utc).isoformat(), **_pagination(items, total, page, page_size)}

    def _physical(self, actor):
        tables: list[dict] = [{"table": name, "total_bytes": None, "estimated_rows": None, "includes_descendants": True,
                   "available": False} for name in SHARED_TABLES]
        physical: dict = {"scope": "SHARED_DATABASE", "not_per_user": True, "database_bytes": None, "tables": tables,
                    "row_count_kind": "ESTIMATE_NOT_EXACT", "warnings": ["EXTERNAL_COMPRESSION_STORAGE_NOT_ATTRIBUTED"],
                    "storage_coverage": "BASE_AND_INHERITED_RELATIONS", "collected_at": datetime.now(timezone.utc).isoformat()}
        try:
            with self._read(self.api_pool, actor) as connection:
                physical["database_bytes"] = int(connection.execute("SELECT pg_database_size(current_database())").fetchone()[0])
        except Exception:
            physical["warnings"].append("DATABASE_SIZE_UNAVAILABLE")
        try:
            with self._read(self.api_pool, actor) as connection:
                rows = connection.execute("""WITH RECURSIVE roots AS (
                  SELECT name,to_regclass('public.'||name)::oid AS oid FROM unnest(%s::text[]) AS names(name)
                ), inherited(name,oid,depth) AS (
                  SELECT name,oid,0 FROM roots WHERE oid IS NOT NULL
                  UNION ALL SELECT p.name,i.inhrelid,p.depth+1 FROM inherited p
                    JOIN pg_inherits i ON i.inhparent=p.oid WHERE p.depth<8
                ), bounded AS MATERIALIZED (SELECT DISTINCT name,oid FROM inherited LIMIT 5001)
                SELECT r.name,sum(pg_total_relation_size(b.oid))::bigint,
                  sum(s.n_live_tup)::bigint,count(b.oid),
                  (SELECT count(*) FROM bounded)>5000 AS truncated
                FROM roots r LEFT JOIN bounded b ON b.name=r.name LEFT JOIN pg_stat_user_tables s ON s.relid=b.oid
                GROUP BY r.name ORDER BY r.name""", (list(SHARED_TABLES),)).fetchall()
            indexed = {row[0]: row for row in rows}
            for table in tables:
                row = indexed.get(table["table"])
                if row is not None and not row[4] and row[3] and row[1] is not None:
                    table.update(total_bytes=int(row[1]), estimated_rows=int(row[2]) if row[2] is not None else None, available=True)
            if any(row[4] for row in rows):
                physical["warnings"].append("TABLE_CATALOG_BUDGET_EXCEEDED")
        except Exception:
            physical["warnings"].append("TABLE_STORAGE_UNAVAILABLE")
        disk: dict = {"scope": "APPLICATION_HOST_FILESYSTEM", "total_bytes": None, "used_bytes": None,
                "free_bytes": None, "used_percent": None, "available": False}
        try:
            usage = shutil.disk_usage(self.disk_path)
            disk.update(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free,
                        used_percent=round(100*usage.used/usage.total, 2) if usage.total else None, available=True)
        except OSError:
            physical["warnings"].append("APPLICATION_DISK_UNAVAILABLE")
        physical["disk"] = disk
        return physical

    def resources(self, actor):
        require_owner(actor)
        with self._read(self.auth_pool, actor) as connection:
            scope = self._scope(connection, actor)
        return {"scope": scope, "generated_at": datetime.now(timezone.utc).isoformat(),
                "business": {"scope": "CURRENT_TENANT", "counts": self._business(actor)}, "physical": self._physical(actor)}

    def revoke_sessions(self, actor, target_user_id, *, trace_id):
        now = datetime.now(timezone.utc)
        require_owner(actor, recent=True, now=now)
        try:
            target = UUID(str(target_user_id))
        except ValueError:
            raise AdminConsoleError(422, "INVALID_USER_ID", "成员标识无效") from None
        if str(target) == str(actor.user_id):
            raise AdminConsoleError(409, "SELF_SESSION_REVOKE_FORBIDDEN", "不能在此强制退出自己的会话，请使用退出登录")
        with self.auth_pool.transaction(actor.tenant_id, actor.user_id) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            # Membership mutations take this namespace before row locks, then
            # acquire the distinct audit-chain lock after session writes.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"membership:{actor.tenant_id}",))
            now = datetime.now(timezone.utc)
            current = connection.execute("""SELECT s.id FROM sessions s
              JOIN memberships m ON m.tenant_id=s.tenant_id AND m.user_id=s.user_id
              JOIN users u ON u.id=s.user_id JOIN tenants t ON t.id=s.tenant_id
              WHERE s.tenant_id=%s AND s.user_id=%s AND s.id=%s
                AND s.revoked_at IS NULL AND s.expires_at>%s
                AND s.recent_auth_at>=%s AND s.recent_auth_at<=%s
                AND m.status='ACTIVE' AND m.role='OWNER' AND u.status='ACTIVE' AND t.status='ACTIVE'
                AND (NOT u.mfa_required OR s.mfa_verified_at IS NOT NULL)""",
              (actor.tenant_id, actor.user_id, actor.session_id, now, now-timedelta(minutes=10),
               now+timedelta(seconds=5))).fetchone()
            if current is None:
                raise AdminConsoleError(403, "RECENT_AUTH_REQUIRED", "当前登录或管理权限已失效，请重新验证后继续")
            scope = self._scope(connection, actor)
            member = connection.execute("""SELECT id FROM memberships
              WHERE tenant_id=%s AND user_id=%s FOR UPDATE""", (actor.tenant_id, target)).fetchone()
            if member is None:
                raise AdminConsoleError(404, "USER_NOT_FOUND", "该成员不属于当前工作区")
            count = int(connection.execute("""WITH revoked AS (
              UPDATE sessions SET revoked_at=%s WHERE tenant_id=%s AND user_id=%s
                AND revoked_at IS NULL AND expires_at>%s RETURNING 1
              ) SELECT count(*) FROM revoked""", (now, actor.tenant_id, target, now)).fetchone()[0])
            self.identity._audit(connection, actor.tenant_id, actor.user_id, target,
                                 "admin.sessions.revoke", "SUCCESS", trace_id)
        return {"scope": scope, "generated_at": now.isoformat(), "user_id": str(target),
                "revoked_sessions": count, "audit_recorded": True}
