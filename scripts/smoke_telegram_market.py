"""One explicitly marked owner-only Telegram chart/menu acceptance message.

Without --send this is read-only. Never injects forged Telegram inbound updates.
An internal negative update ID distinguishes the test from genuine inbound IDs.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from prepare_service_envs import SECRET_DIR, read_env
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
from gatescope.application.telegram_market import build_market_reply
from gatescope.application.telegram_chart import render_chart


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    p.add_argument("--mode", choices=("overview", "pattern", "opportunities"), default="overview")
    p.add_argument("--image", type=Path, help="Optional PNG acceptance artifact")
    args = p.parse_args()
    env = read_env(SECRET_DIR / "formal-runtime.env")
    username = json.loads((SECRET_DIR / "owner-login.json").read_text())["username"]
    with psycopg.connect(env["GATESCOPE_MIGRATION_DATABASE_URL"]) as c:
        bindings = c.execute(
            """SELECT tc.tenant_id,tc.user_id,tc.id FROM telegram_connections tc JOIN users u ON u.id=tc.user_id
            JOIN memberships m ON m.tenant_id=tc.tenant_id AND m.user_id=u.id
            WHERE u.normalized_username=%s AND u.status='ACTIVE' AND m.status='ACTIVE' AND m.role='OWNER' AND tc.status='ACTIVE'""",
            (username.strip().lower(),),
        ).fetchall()
    if len(bindings) != 1:
        raise RuntimeError("Expected exactly one active owner binding")
    tenant, user, connection = bindings[0]
    pool = PostgresPool(env["GATESCOPE_DATABASE_URL"])
    pool.open()
    try:
        repo = TelegramMarketRepository(pool)
        items, _ = repo.watchlist(tenant, user, 0, 1)
        if not items:
            raise RuntimeError("Owner watchlist empty")
        command = {"action": "overview", "instrument_id": items[0]["id"], "timeframe": "1h"}
        if args.mode == "opportunities":
            command = {"action": "opportunities"}
        elif args.mode == "pattern":
            from gatescope.application.telegram_market import PERIODS

            choices = []
            owned, _ = repo.watchlist(tenant, user, 0, 2)
            for item in owned:
                for tf in PERIODS:
                    records = repo.tracking(item["id"], tf).get("records", {}).values()
                    choices.extend(
                        (r, item, tf) for r in records if r["phase"] in ("FORMING", "FORMED", "CONFIRMED")
                    )
            if not choices:
                raise RuntimeError("No actual tracked pattern available")
            record, item, tf = max(choices, key=lambda v: (v[0]["phase"] == "FORMED", v[0]["observed_time"]))
            command = {
                "action": "detail",
                "instrument_id": item["id"],
                "timeframe": tf,
                "candidate": record["id"],
                "initial": False,
            }
        identifier = uuid4()
        response = build_market_reply(repo, tenant, user, identifier, command)
        response["text"] = "部署验收 · 只读测试（不下单）\n" + response["text"]
        response["acceptance_test"] = True
        photo = render_chart(response["chart"]) if response.get("chart") else None
        if args.image and photo:
            args.image.write_bytes(photo)
        print(
            f"Mode: {args.mode}; chart: {len(photo) if photo else 0} bytes; menu: {len(response['actions'])} actions"
        )
        if args.mode == "opportunities":
            print("\n".join(response["text"].splitlines()[1:6]))
        elif args.mode == "pattern":
            print(
                f"Pattern-only layout: {response['chart'].get('layout') == 'PATTERN'}; indicator nodes: {len(response['chart']['indicator_versions'])}"
            )
        if not args.send:
            return
        with pool.transaction(tenant, user) as c:
            c.execute(
                """INSERT INTO telegram_market_requests(id,tenant_id,user_id,connection_id,update_id,command_json,response_json)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    identifier,
                    tenant,
                    user,
                    connection,
                    -int(time.time() * 1000),
                    Jsonb(command),
                    Jsonb(response),
                ),
            )
        for _ in range(15):
            with pool.transaction(tenant, user) as c:
                status, error, message = c.execute(
                    "SELECT status,safe_error,telegram_message_id FROM telegram_market_requests WHERE id=%s",
                    (identifier,),
                ).fetchone()
            if status in ("SENT", "FAILED"):
                print(
                    f"Telegram chart/menu: {status}; business message acknowledged={message is not None}; safe_error={error}"
                )
                if status != "SENT":
                    raise RuntimeError("Telegram chart delivery failed")
                return
            time.sleep(2)
        raise RuntimeError("Telegram query pending; no duplicate job submitted")
    finally:
        pool.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Acceptance failed: {type(exc).__name__}; private details suppressed")
        raise SystemExit(1) from None
