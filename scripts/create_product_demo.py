#!/usr/bin/env python3
"""Inspect, or explicitly provision, one isolated product-demo workspace.

No source credentials, notification bindings, tasks or trading facts are copied.
Database writes are one transaction. A private, exclusively-created login file
is fsynced before commit; an ambiguous commit keeps that file for admin recovery
instead of losing a committed account's password or silently resetting it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb

from gatescope.auth import validate_password
from gatescope.domain.market import TIMEFRAME_SECONDS

if __package__:
    from .prepare_service_envs import SECRET_DIR, read_env
else:
    from prepare_service_envs import SECRET_DIR, read_env

SOURCE_TENANT, SOURCE_USER = "default", "owner"
TARGET_TENANT, TARGET_USER, TARGET_NAME = "product-demo", "product_test", "产品体验"
OUTPUT = SECRET_DIR / "product-test-login.json"
MAX_COPY_ROWS = 5000
IDENTIFIER = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


class ProvisioningRefused(RuntimeError):
    """Only fixed, non-sensitive codes may reach CLI output."""


def inspect_source(connection, *, lock=False):
    source = connection.execute("""SELECT t.id,u.id FROM tenants t
      JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id
      WHERE t.slug=%s AND u.normalized_username=%s AND t.status='ACTIVE'
        AND u.status='ACTIVE' AND m.status='ACTIVE' AND m.role='OWNER' AND u.mfa_required=true
      """ + (" FOR SHARE OF t,u,m" if lock else ""), (SOURCE_TENANT, SOURCE_USER)).fetchone()
    if source is None:
        raise ProvisioningRefused("SOURCE_ACTIVE_MFA_OWNER_REQUIRED")
    tenant, user = source
    target = connection.execute("""SELECT
      (SELECT count(*) FROM tenants WHERE slug=%s),
      (SELECT count(*) FROM users WHERE normalized_username=%s)""", (TARGET_TENANT, TARGET_USER)).fetchone()
    counts = {}
    for label, table, extra in (
        ("watchlists", "watchlists", "AND deleted_at IS NULL"),
        ("chart_layouts", "chart_layouts", "AND deleted_at IS NULL"),
        ("indicator_templates", "indicator_templates", "AND deleted_at IS NULL"),
        ("alert_rules", "alert_rules", "AND status<>'DELETED'"),
        ("paper_accounts", "paper_accounts", ""),
    ):
        counts[label] = int(connection.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND owner_user_id=%s {extra}", (tenant, user)
        ).fetchone()[0])
    counts["watchlist_items"] = int(connection.execute("""SELECT count(*) FROM watchlist_items wi
      JOIN watchlists w ON w.id=wi.watchlist_id AND w.tenant_id=wi.tenant_id
      JOIN instruments i ON i.id=wi.instrument_id AND i.status='ACTIVE'
      WHERE w.tenant_id=%s AND w.owner_user_id=%s AND w.deleted_at IS NULL""", (tenant, user)).fetchone()[0])
    return {"tenant_id": tenant, "user_id": user, "counts": counts,
            "target_tenants": int(target[0]), "target_users": int(target[1])}


def _identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def clean_layout(spec):
    if not isinstance(spec, dict):
        raise ProvisioningRefused("INVALID_SOURCE_LAYOUT")
    clean = {"schema_version": 1}
    if "instrument_id" in spec:
        clean["instrument_id"] = str(UUID(str(spec["instrument_id"])))
    if isinstance(spec.get("symbol"), str) and len(spec["symbol"]) <= 120:
        clean["symbol"] = spec["symbol"]
    if spec.get("timeframe") in TIMEFRAME_SECONDS:
        clean["timeframe"] = spec["timeframe"]
    clean["panes"] = [value for value in spec.get("panes", []) if value in ("price", "volume", "rsi", "macd")][:8]
    clean["indicator_ids"] = [value for value in spec.get("indicator_ids", []) if _identifier(value)][:32]
    studies = spec.get("chart_studies")
    if isinstance(studies, dict) and studies.get("version") == 1:
        vpvr = studies.get("vpvr") if isinstance(studies.get("vpvr"), dict) else {}
        metrics = studies.get("coinglassMetrics") if isinstance(studies.get("coinglassMetrics"), list) else []
        def bounded(key, low, high, default):
            value = vpvr.get(key)
            return value if type(value) is int and low <= value <= high else default
        clean["chart_studies"] = {"version": 1,
            "coinglassMetrics": list(dict.fromkeys(value for value in metrics
                if value in ("oi", "funding", "lsr", "liquidation_long", "liquidation_short", "oi_change"))),
            "vpvr": {"enabled": vpvr.get("enabled") is True, "rows": vpvr.get("rows") if vpvr.get("rows") in (24, 48, 96) else 48,
                     "valueAreaPercent": bounded("valueAreaPercent", 50, 95, 70), "widthPercent": bounded("widthPercent", 10, 40, 25)}}
    return clean


def clean_template(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("nodes", []), list):
        raise ProvisioningRefused("INVALID_SOURCE_TEMPLATE")
    nodes = []
    for node in spec.get("nodes", [])[:32]:
        if not isinstance(node, dict) or not _identifier(node.get("instance_id")) or not _identifier(node.get("indicator_id")):
            raise ProvisioningRefused("INVALID_SOURCE_INDICATOR_NODE")
        params = node.get("params", {})
        if not isinstance(params, dict):
            raise ProvisioningRefused("INVALID_SOURCE_INDICATOR_PARAMS")
        clean = {"instance_id": node["instance_id"], "indicator_id": node["indicator_id"],
                 "params": {key: value for key, value in params.items() if _identifier(key)
                            and type(value) in (int, float) and math.isfinite(value)}}
        dependencies = node.get("depends_on", [])
        if isinstance(dependencies, list):
            clean["depends_on"] = [value for value in dependencies if _identifier(value)][:16]
        nodes.append(clean)
    return {"schema_version": 1, "nodes": nodes}


def clean_condition(condition):
    keys = {"schema_version", "instrument_id", "timeframe", "pattern_ids", "match_mode", "minimum_confidence",
            "confirmed_only", "coincidence_bars", "cooldown_seconds"}
    if not isinstance(condition, dict) or condition.get("timeframe") not in TIMEFRAME_SECONDS:
        raise ProvisioningRefused("INVALID_SOURCE_ALERT")
    clean = {key: value for key, value in condition.items() if key in keys}
    clean["instrument_id"] = str(UUID(str(condition.get("instrument_id"))))
    patterns = condition.get("pattern_ids")
    if not isinstance(patterns, list) or not 1 <= len(patterns) <= 28 or not all(_identifier(value) for value in patterns):
        raise ProvisioningRefused("INVALID_SOURCE_ALERT_PATTERNS")
    # Known condition fields are primitives/lists, not opaque nested credential containers.
    if any(isinstance(value, (dict, list)) for key, value in clean.items() if key != "pattern_ids"):
        raise ProvisioningRefused("INVALID_SOURCE_ALERT_FIELDS")
    clean["schema_version"] = 1
    return clean


def _source_rows(connection, source):
    args = (source["tenant_id"], source["user_id"])
    reads = {
        "watchlists": "SELECT id,name FROM watchlists WHERE tenant_id=%s AND owner_user_id=%s AND deleted_at IS NULL ORDER BY created_at,id",
        "watchlist_items": """SELECT wi.watchlist_id,wi.instrument_id,wi.position,wi.default_timeframe FROM watchlist_items wi
          JOIN watchlists w ON w.id=wi.watchlist_id AND w.tenant_id=wi.tenant_id
          JOIN instruments i ON i.id=wi.instrument_id AND i.status='ACTIVE'
          WHERE w.tenant_id=%s AND w.owner_user_id=%s AND w.deleted_at IS NULL ORDER BY wi.watchlist_id,wi.position""",
        "chart_layouts": "SELECT name,spec_json FROM chart_layouts WHERE tenant_id=%s AND owner_user_id=%s AND deleted_at IS NULL ORDER BY created_at,id",
        "indicator_templates": "SELECT name,spec_json FROM indicator_templates WHERE tenant_id=%s AND owner_user_id=%s AND deleted_at IS NULL ORDER BY created_at,id",
        "alert_rules": """SELECT r.name,v.condition_json FROM alert_rules r JOIN alert_rule_versions v
          ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id AND v.version=r.current_version
          WHERE r.tenant_id=%s AND r.owner_user_id=%s AND r.status<>'DELETED' ORDER BY r.created_at,r.id""",
        "paper_accounts": """SELECT name,base_currency,initial_equity,risk_fraction,max_notional_fraction,
          fee_bps_per_side,market_slippage_bps,max_entry_drift_bps FROM paper_accounts
          WHERE tenant_id=%s AND owner_user_id=%s ORDER BY created_at,id""",
    }
    rows = {name: connection.execute(sql+" LIMIT 5001", args).fetchall() for name, sql in reads.items()}
    if any(len(values) != source["counts"][name] for name, values in rows.items()):
        raise ProvisioningRefused("SOURCE_COUNT_OR_CURRENT_VERSION_MISMATCH")
    return rows


def _append_audit(connection, tenant, user, source, counts, now, trace):
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(tenant),))
    previous = connection.execute("""SELECT fact_hash FROM identity_audit_facts
      WHERE tenant_id=%s ORDER BY occurred_at DESC,id DESC LIMIT 1""", (tenant,)).fetchone()
    previous_hash = str(previous[0]) if previous else None
    fact_id = uuid4()
    payload = {"action": "admin.demo.create", "actor_id": None, "fact_id": str(fact_id),
               "occurred_at": now.isoformat(), "outcome": "SUCCESS", "previous_hash": previous_hash,
               "subject_id": str(user), "tenant_id": str(tenant), "trace_id": trace[:64]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    detail = {"source": "local-admin", "source_tenant_id": str(source["tenant_id"]),
              "source_user_id": str(source["user_id"]), "copied_counts": counts,
              "credentials_copied": False, "mfa_exception": "THIS_NEW_USER_ONLY"}
    connection.execute("""INSERT INTO identity_audit_facts(id,tenant_id,actor_user_id,subject_user_id,
      action,outcome,occurred_at,trace_id,detail_json,previous_hash,fact_hash)
      VALUES(%s,%s,NULL,%s,'admin.demo.create','SUCCESS',%s,%s,%s,%s,%s)""",
      (fact_id, tenant, user, now, trace[:64], Jsonb(detail), previous_hash, digest))


def write_credentials_exclusive(path: Path, document):
    if path.parent.is_symlink():
        raise ProvisioningRefused("SECRET_DIRECTORY_SYMLINK_REFUSED")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".product-test-login-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and fails if any target, including a
        # dangling symlink, already exists. Never replace an earlier login file.
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def provision(connection, *, public_origin: str, output: Path = OUTPUT):
    parsed = urlparse(public_origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password \
            or parsed.path or parsed.params or parsed.query or parsed.fragment:
        raise ProvisioningRefused("HTTPS_PUBLIC_ORIGIN_REQUIRED")
    if os.path.lexists(output):
        raise ProvisioningRefused("LOGIN_FILE_ALREADY_EXISTS_NO_OVERWRITE")
    password = secrets.token_urlsafe(32)
    validate_password(password)
    encoded = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1).hash(password)
    tenant, user, membership = uuid4(), uuid4(), uuid4()
    now, trace = datetime.now(timezone.utc), f"admin-demo-{uuid4().hex}"
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.execute("SET LOCAL statement_timeout='15s'")
        connection.execute("SET LOCAL lock_timeout='3s'")
        connection.execute("SET LOCAL row_security=off")
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("admin:product-demo:product_test",))
        source = inspect_source(connection, lock=True)
        if source["target_tenants"] or source["target_users"]:
            raise ProvisioningRefused("TARGET_ALREADY_EXISTS_NO_RESET")
        if sum(source["counts"].values()) > MAX_COPY_ROWS:
            raise ProvisioningRefused("COPY_ROW_BUDGET_EXCEEDED")
        rows = _source_rows(connection, source)
        connection.execute("""INSERT INTO tenants(id,slug,name,status,created_at,updated_at)
          VALUES(%s,%s,%s,'ACTIVE',%s,%s)""", (tenant, TARGET_TENANT, TARGET_NAME, now, now))
        connection.execute("""INSERT INTO users(id,normalized_username,display_username,password_hash,status,
          mfa_required,etag,password_changed_at,created_at,updated_at)
          VALUES(%s,%s,%s,%s,'ACTIVE',false,1,%s,%s,%s)""", (user, TARGET_USER, TARGET_USER, encoded, now, now, now))
        connection.execute("""INSERT INTO memberships(id,tenant_id,user_id,role,status,etag,created_at,updated_at)
          VALUES(%s,%s,%s,'OWNER','ACTIVE',1,%s,%s)""", (membership, tenant, user, now, now))
        connection.execute("""INSERT INTO password_history(id,tenant_id,user_id,password_hash,created_at)
          VALUES(%s,%s,%s,%s,%s)""", (uuid4(), tenant, user, encoded, now))
        watchlist_ids = {}
        for old_id, name in rows["watchlists"]:
            new_id = watchlist_ids[old_id] = uuid4()
            connection.execute("""INSERT INTO watchlists(id,tenant_id,owner_user_id,name,etag,created_at,updated_at)
              VALUES(%s,%s,%s,%s,1,%s,%s)""", (new_id, tenant, user, name, now, now))
        for old_list, instrument, position, timeframe in rows["watchlist_items"]:
            connection.execute("""INSERT INTO watchlist_items(id,tenant_id,watchlist_id,instrument_id,position,
              default_timeframe,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
              (uuid4(), tenant, watchlist_ids[old_list], instrument, position, timeframe, now, now))
        for table, cleaner in (("chart_layouts", clean_layout), ("indicator_templates", clean_template)):
            for name, spec in rows[table]:
                connection.execute(f"""INSERT INTO {table}(id,tenant_id,owner_user_id,name,spec_json,etag,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,%s,1,%s,%s)""", (uuid4(), tenant, user, name, Jsonb(cleaner(spec)), now, now))
        for name, condition in rows["alert_rules"]:
            rule_id = uuid4()
            connection.execute("""INSERT INTO alert_rules(id,tenant_id,owner_user_id,name,current_version,status,etag,created_at,updated_at)
              VALUES(%s,%s,%s,%s,1,'PAUSED',1,%s,%s)""", (rule_id, tenant, user, name, now, now))
            connection.execute("""INSERT INTO alert_rule_versions(id,tenant_id,alert_rule_id,version,condition_json,
              routing_json,created_by,created_at) VALUES(%s,%s,%s,1,%s,%s,%s,%s)""",
              (uuid4(), tenant, rule_id, Jsonb(clean_condition(condition)), Jsonb({"schema_version": 1, "channel_ids": []}), user, now))
        for parameters in rows["paper_accounts"]:
            connection.execute("""INSERT INTO paper_accounts(id,tenant_id,owner_user_id,name,base_currency,initial_equity,
              realized_pnl,risk_fraction,max_notional_fraction,fee_bps_per_side,market_slippage_bps,max_entry_drift_bps,
              status,etag,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s,'ACTIVE',1,%s,%s)""",
              (uuid4(), tenant, user, *parameters, now, now))
        _append_audit(connection, tenant, user, source, source["counts"], now, trace)
        document = {"username": TARGET_USER, "password": password, "tenant_slug": TARGET_TENANT,
                    "workspace": TARGET_NAME, "tenant_id": str(tenant), "user_id": str(user),
                    "membership_id": str(membership), "login_url": f"{public_origin}/login", "role": "OWNER",
                    "mfa_required": False, "counts": source["counts"], "created_at": now.isoformat(),
                    "warning": "私密体验凭据，权限须为0600。提交异常时先核对数据库，不要覆盖或重置。无外部凭据和原账户交易记录。"}
        write_credentials_exclusive(output, document)
    return {"tenant_slug": TARGET_TENANT, "username": TARGET_USER, "counts": source["counts"],
            "credentials_file": str(output), "mfa_required": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="原子创建隔离体验账号；默认只检查计数")
    parser.add_argument("--public-origin", help="可选HTTPS站点origin；不接受口令或密钥参数")
    args = parser.parse_args(argv)
    try:
        values = read_env(SECRET_DIR / "formal-runtime.env")
        url = values.get("GATESCOPE_MIGRATION_DATABASE_URL", "")
        if not url:
            raise ProvisioningRefused("MIGRATION_DATABASE_CONFIGURATION_REQUIRED")
        with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True, connect_timeout=5) as connection:
            if args.apply:
                origin = (args.public_origin or values.get("GATESCOPE_PUBLIC_ORIGIN", "")).rstrip("/")
                summary = provision(connection, public_origin=origin, output=OUTPUT)
            else:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    connection.execute("SET LOCAL statement_timeout='10s'")
                    connection.execute("SET LOCAL lock_timeout='2s'")
                    connection.execute("SET LOCAL row_security=off")
                    source = inspect_source(connection)
                summary = {"counts": source["counts"], "target_tenants": source["target_tenants"],
                           "target_users": source["target_users"], "credentials_file_exists": int(os.path.lexists(OUTPUT))}
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except ProvisioningRefused as exc:
        print(f"REFUSED: {exc}")
    except Exception:
        # Database/file exceptions can contain DSNs, SQL values or passwords.
        # An existing file after failure may mean an ambiguous commit: retain it.
        print("FAILED: provisioning did not confirm completion; inspect target counts and restricted login file before retrying")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
