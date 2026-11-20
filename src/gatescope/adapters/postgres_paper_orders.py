"""Atomic owner-scoped PAPER orders backed by the existing paper_accounts cash ledger.

Quotes are fetched before transactions; no network request occurs while holding locks.
All entries are 1x collateralised lots, never exchange orders. Acknowledgement is not
a fill, expired market data is not executable, and a protective trigger is durable.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID, uuid4

from psycopg import Error as PostgresError
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_paper import PaperConflict, PaperNotFound, PaperPreconditionFailed
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.domain.paper_decision import normalized_decision_context

ZERO = Decimal(0)
ACTIVE = ("NEW", "OPEN", "PARTIALLY_FILLED")
LIMITATIONS = [
    "PAPER_ONLY_NO_EXCHANGE_ORDER",
    "ONE_X_FULL_COLLATERAL_USDT",
    "TEN_PERCENT_VISIBLE_DEPTH_PARTICIPATION",
    "NO_QUEUE_PRIORITY_OR_OFFLINE_FILL_GUARANTEE",
    "FUNDING_NOT_SETTLED",
    "ACCEPTED_IS_NOT_FILLED",
]


def _json(value: Any) -> Any:
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("UTC_TIMESTAMP_REQUIRED")
    return result.astimezone(timezone.utc)


def _decimal(value: Any, name: str, *, positive: bool = True) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception:
        raise ValueError(f"INVALID_{name.upper()}") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"INVALID_{name.upper()}")
    return number


def _integer(value: Any, name: str, *, low: int = 1, high: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"INVALID_{name.upper()}")
    return value


def normalized_request(
    request: Mapping[str, Any], *, effect: str = "OPEN", position_id: UUID | None = None
) -> dict[str, Any]:
    """Hash the complete effective order, not just its client request ID."""
    allowed = {
        "account_id",
        "instrument_id",
        "symbol",
        "timeframe",
        "side",
        "contracts",
        "order_type",
        "time_in_force",
        "limit_price",
        "stop_price",
        "take_profit_price",
        "max_slippage_bps",
        "ttl_seconds",
        "source",
        "idempotency_key",
        "decision_context",
    }
    if set(request) - allowed:
        raise ValueError("UNRECOGNIZED_ORDER_FIELDS")
    key = request.get("idempotency_key")
    if not isinstance(key, str) or not 8 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("INVALID_IDEMPOTENCY_KEY")
    order_type = str(request.get("order_type", "MARKET")).upper()
    tif = str(request.get("time_in_force", "GTC" if order_type == "LIMIT" else "IOC")).upper()
    if (
        order_type not in ("MARKET", "LIMIT")
        or tif not in ("GTC", "IOC", "FOK")
        or (order_type == "MARKET" and tif != "IOC")
    ):
        raise ValueError("INVALID_ORDER_TYPE_OR_TIME_IN_FORCE")
    source = str(request.get("source", "MCP")).upper()
    if source not in ("WEB", "MCP", "SYSTEM"):
        raise ValueError("INVALID_ORDER_SOURCE")
    contracts = (
        None
        if effect == "CLOSE" and request.get("contracts") is None
        else _integer(request.get("contracts"), "contracts")
    )
    payload: dict[str, Any] = {
        "effect": effect,
        "idempotency_key": key,
        "contracts": contracts,
        "order_type": order_type,
        "time_in_force": tif,
        "source": source,
        "max_slippage_bps": str(
            _decimal(request.get("max_slippage_bps", 50), "max_slippage_bps", positive=False)
        ),
        "ttl_seconds": _integer(request.get("ttl_seconds", 3600), "ttl_seconds", high=86400),
    }
    if Decimal(payload["max_slippage_bps"]) > 100:
        raise ValueError("MAX_SLIPPAGE_EXCEEDS_100_BPS")
    if order_type == "LIMIT":
        payload["limit_price"] = str(_decimal(request.get("limit_price"), "limit_price"))
    elif request.get("limit_price") is not None:
        raise ValueError("MARKET_ORDER_HAS_LIMIT_PRICE")
    if effect == "OPEN":
        payload.update(
            {
                "account_id": str(UUID(str(request["account_id"]))),
                "instrument_id": str(UUID(str(request["instrument_id"]))),
                "symbol": str(request["symbol"]),
                "timeframe": str(request.get("timeframe", "15m")),
                "side": str(request.get("side", "")).upper(),
                "stop_price": str(_decimal(request.get("stop_price"), "stop_price")),
                "take_profit_price": str(_decimal(request.get("take_profit_price"), "take_profit_price")),
            }
        )
        if payload["timeframe"] not in TIMEFRAME_SECONDS or payload["side"] not in ("LONG", "SHORT"):
            raise ValueError("INVALID_SIDE_OR_TIMEFRAME")
        # Absent/null context deliberately leaves the pre-0048 canonical hash
        # unchanged, so retries of existing requests remain safe after upgrades.
        if request.get("decision_context") is not None:
            payload["decision_context"] = normalized_decision_context(request["decision_context"])
    elif effect == "CLOSE" and position_id is not None:
        payload["position_id"] = str(UUID(str(position_id)))
        # Reject irrelevant opening fields: a close can only reduce the named lot.
        if any(
            request.get(key) is not None
            for key in ("side", "stop_price", "take_profit_price", "decision_context")
        ):
            raise ValueError("CLOSE_CANNOT_CHANGE_POSITION")
        for key in ("account_id", "instrument_id", "symbol", "timeframe"):
            if request.get(key) is not None:
                payload[key] = str(request[key])
    else:
        raise ValueError("INVALID_ORDER_EFFECT")
    return payload


def request_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_json(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor = connection.execute(sql, params)
    row = cursor.fetchone()
    return (
        dict(zip((column.name for column in cursor.description), row, strict=True))
        if row is not None
        else None
    )


def _many(connection: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, params)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


class PostgresPaperOrderRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    @staticmethod
    def _actor_can_trade(connection: Any) -> bool:
        return bool(connection.execute("SELECT gatescope_security.paper_actor_can_trade()").fetchone()[0])

    @staticmethod
    def _account(
        connection: Any, tenant_id: UUID, owner_id: UUID, account_id: UUID, *, lock: bool = False
    ) -> dict[str, Any]:
        account = _one(
            connection,
            "SELECT * FROM paper_accounts WHERE tenant_id=%s AND owner_user_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (tenant_id, owner_id, account_id),
        )
        if account is None:
            raise PaperNotFound("PAPER_ACCOUNT_NOT_FOUND")
        return account

    @staticmethod
    def budgets(
        connection: Any, tenant_id: UUID, owner_id: UUID, account: Mapping[str, Any]
    ) -> dict[str, Decimal]:
        """Call under the paper_accounts row lock before either old or new entries."""
        args = (tenant_id, owner_id, account["id"])
        old = connection.execute(
            """SELECT coalesce(sum(s.entry_fill_price*s.quantity),0),
            coalesce(sum((t.exact_terms_json->>'worst_case_loss')::numeric),0),coalesce(sum(s.fees),0)
            FROM trades t JOIN trade_state_current s ON s.trade_id=t.id
            WHERE t.tenant_id=%s AND t.owner_user_id=%s AND t.paper_account_id=%s AND s.state='OPEN'""",
            args,
        ).fetchone()
        positions = connection.execute(
            """SELECT coalesce(sum(average_entry_price*remaining_contracts*multiplier),0),
            coalesce(sum((abs(average_entry_price-stop_price)+stop_price*fee_rate)*remaining_contracts*multiplier),0)
            FROM paper_positions WHERE tenant_id=%s AND owner_user_id=%s AND account_id=%s AND status='OPEN'""",
            args,
        ).fetchone()
        orders = connection.execute(
            """SELECT coalesce(sum(reserved_notional),0),coalesce(sum(reserved_risk),0),coalesce(sum(reserved_fees),0)
            FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND account_id=%s AND status=ANY(%s)""",
            (*args, list(ACTIVE)),
        ).fetchone()
        cash = Decimal(account["initial_equity"]) + Decimal(account["realized_pnl"])
        margin, reserved, fees = (
            Decimal(old[0]) + Decimal(positions[0]),
            Decimal(orders[0]),
            Decimal(orders[2]),
        )
        return {
            "initial_equity": Decimal(account["initial_equity"]),
            "realized_pnl": Decimal(account["realized_pnl"]),
            "cash_balance": cash,
            "legacy_open_fees": Decimal(old[2]),
            "legacy_position_margin": Decimal(old[0]),
            "position_margin": margin,
            "order_reserved_margin": reserved,
            "order_reserved_fees": fees,
            "available_balance": cash - margin - reserved - fees - Decimal(old[2]),
            "risk_used": Decimal(old[1]) + Decimal(positions[1]) + Decimal(orders[1]),
            "risk_limit": max(ZERO, (cash - Decimal(old[2])) * Decimal(account["risk_fraction"])),
            "notional_used": margin + reserved,
            "notional_limit": max(ZERO, (cash - Decimal(old[2])) * Decimal(account["max_notional_fraction"])),
        }

    def account_snapshot(
        self, tenant_id: UUID, owner_id: UUID, account_id: UUID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        from gatescope.adapters.postgres_worker_health import paper_execution_health

        at = _now(now)
        with self.pool.read_transaction(tenant_id, owner_id) as connection:
            account = self._account(connection, tenant_id, owner_id, account_id)
            balance: dict[str, Any] = self.budgets(connection, tenant_id, owner_id, account)
            positions = _many(
                connection,
                "SELECT * FROM paper_positions WHERE tenant_id=%s AND owner_user_id=%s AND account_id=%s AND status='OPEN'",
                (tenant_id, owner_id, account_id),
            )
            valid = all(
                p["mark_price"] is not None
                and p["mark_as_of"] is not None
                and timedelta(0) <= at - p["mark_as_of"] <= timedelta(seconds=15)
                for p in positions
            )
            valid = valid and balance["legacy_position_margin"] == 0
            unrealized = (
                sum(
                    (
                        (Decimal(p["mark_price"]) - Decimal(p["average_entry_price"]))
                        * p["remaining_contracts"]
                        * Decimal(p["multiplier"])
                        * (1 if p["side"] == "LONG" else -1)
                        for p in positions
                    ),
                    ZERO,
                )
                if valid
                else None
            )
            balance.update(
                {
                    "unrealized_pnl": unrealized,
                    "equity": balance["cash_balance"] + unrealized - balance["legacy_open_fees"]
                    if unrealized is not None
                    else None,
                    "mark_status": "FRESH" if valid else "UNAVAILABLE_OR_STALE",
                    "as_of": at,
                }
            )
            heartbeat = connection.execute("""SELECT running,heartbeat_at,status_json->>'last_success_at',status_json->>'safe_error_code'
                FROM worker_heartbeats WHERE workload='paper-order-worker'""").fetchone()
            execution_health = paper_execution_health(heartbeat, at)
        return _json(
            {
                "environment": "PAPER",
                "account": {
                    key: account[key]
                    for key in (
                        "id",
                        "name",
                        "base_currency",
                        "status",
                        "risk_fraction",
                        "max_notional_fraction",
                    )
                },
                "balance": balance,
                "limitations": LIMITATIONS,
                "execution_health": execution_health,
            }
        )

    def _list(
        self,
        table: str,
        tenant_id: UUID,
        owner_id: UUID,
        *,
        account_id: UUID | None = None,
        instrument_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        cursor: str | None = None,
        pagination: str = "OFFSET",
    ) -> dict[str, Any]:
        from gatescope.domain.paper_pagination import decode_cursor, encode_cursor, scope_digest

        _integer(limit, "limit", high=100)
        _integer(offset, "offset", low=0, high=10000)
        if pagination not in {"OFFSET", "CURSOR"} or (cursor is not None and offset):
            raise ValueError("PAPER_PAGINATION_INVALID")
        keyset = pagination == "CURSOR" or cursor is not None
        if keyset and offset:
            raise ValueError("PAPER_CURSOR_OFFSET_CONFLICT")
        clauses = ["tenant_id=%s", "owner_user_id=%s"]
        args: list[Any] = [tenant_id, owner_id]
        for name, value in (("account_id", account_id), ("instrument_id", instrument_id)):
            if value is not None:
                clauses.append(name + "=%s")
                args.append(value)
        if status is not None:
            if table == "paper_order_fills":
                raise ValueError("FILLS_HAVE_NO_STATUS")
            clauses.append("status=ANY(%s)" if status == "ACTIVE" else "status=%s")
            args.append(
                list(ACTIVE)
                if status == "ACTIVE" and table == "paper_orders"
                else (["OPEN"] if status == "ACTIVE" else status)
            )
        time_col = {
            "paper_orders": "created_at",
            "paper_positions": "opened_at",
            "paper_order_fills": "filled_at",
        }[table]
        ordering = (
            "last_checked_at ASC NULLS FIRST,id"
            if status == "ACTIVE" and not keyset
            else f"{time_col} DESC,id DESC"
        )
        scope = scope_digest(
            table=table,
            tenant=tenant_id,
            owner=owner_id,
            account=account_id,
            instrument=instrument_id,
            status=status,
        )
        snapshot = _now(None)
        if keyset:
            if cursor is not None:
                moment, identity, snapshot = decode_cursor(cursor, scope)
                clauses.append(f"({time_col},id)<(%s,%s)")
                args.extend((moment, identity))
            clauses.append(f"{time_col}<=%s")
            args.append(snapshot)
        projection = (
            "*"
            if table != "paper_order_fills"
            else "id,account_id,order_id,position_id,instrument_id,symbol,timeframe,environment,effect,side,execution_side,contracts,price,base_quantity,notional,fee,realized_pnl,quote_id,reason,filled_at"
        )
        with self.pool.read_transaction(tenant_id, owner_id) as connection:
            if account_id is not None:
                self._account(connection, tenant_id, owner_id, account_id)
            rows = _many(
                connection,
                f"SELECT {projection} FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT %s OFFSET %s",
                (*args, limit + 1, offset),
            )
        more = len(rows) > limit
        next_cursor = (
            encode_cursor(scope, rows[limit - 1][time_col], rows[limit - 1]["id"], snapshot)
            if keyset and more
            else None
        )
        return _json(
            {
                "environment": "PAPER",
                "items": [self._public(row) for row in rows[:limit]],
                "limit": limit,
                "offset": offset,
                "has_more": more,
                "next_offset": offset + limit if more and not keyset else None,
                "pagination": "CURSOR" if keyset else "OFFSET",
                "next_cursor": next_cursor,
                "snapshot_at": snapshot if keyset else None,
                "snapshot_policy": "INSERTION_WINDOW_NOT_FROZEN_MUTABLE_STATUS" if keyset else None,
            }
        )

    def list_orders(self, tenant_id: UUID, owner_id: UUID, **kwargs: Any) -> dict[str, Any]:
        return self._list("paper_orders", tenant_id, owner_id, **kwargs)

    def list_positions(self, tenant_id: UUID, owner_id: UUID, **kwargs: Any) -> dict[str, Any]:
        return self._list("paper_positions", tenant_id, owner_id, **kwargs)

    def list_fills(self, tenant_id: UUID, owner_id: UUID, **kwargs: Any) -> dict[str, Any]:
        return self._list("paper_order_fills", tenant_id, owner_id, **kwargs)

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: value
            for key, value in row.items()
            if key not in ("tenant_id", "owner_user_id", "request_hash", "request_json")
        }
        if "decision_snapshot_id" in row:
            result["decision_status"] = "LINKED" if row["decision_snapshot_id"] else "UNASSOCIATED"
        if "opened_contracts" in row:
            result["gross_realized_pnl"] = row["realized_pnl"]
            result["net_realized_pnl"] = Decimal(row["realized_pnl"]) - Decimal(row["fees"])
            result["pnl_semantics"] = "REALIZED_PNL_IS_GROSS; NET_REALIZED_PNL_DEDUCTS_ALL_CHARGED_LOT_FEES"
        return result

    def _get(self, table: str, tenant_id: UUID, owner_id: UUID, identity: UUID) -> dict[str, Any]:
        with self.pool.read_transaction(tenant_id, owner_id) as connection:
            row = _one(
                connection,
                f"SELECT * FROM {table} WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                (tenant_id, owner_id, identity),
            )
        if row is None:
            raise PaperNotFound("PAPER_RECORD_NOT_FOUND")
        return _json(self._public(row))

    def get_order(
        self, tenant_id: UUID, owner_id: UUID, order_id: UUID, *, now: datetime | None = None
    ) -> dict[str, Any]:
        from gatescope.domain.paper_pagination import encode_cursor, scope_digest

        snapshot = _now(now)
        with self.pool.read_transaction(tenant_id, owner_id) as connection:
            row = _one(
                connection,
                "SELECT * FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                (tenant_id, owner_id, order_id),
            )
            if row is None:
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            fills = _many(
                connection,
                "SELECT * FROM paper_order_fills WHERE tenant_id=%s AND owner_user_id=%s AND order_id=%s AND filled_at<=%s ORDER BY filled_at,id LIMIT 101",
                (tenant_id, owner_id, order_id, snapshot),
            )
            events = _many(
                connection,
                "SELECT id,event_type,payload_json-'quote_snapshot' AS payload_json,created_at FROM paper_order_events WHERE tenant_id=%s AND owner_user_id=%s AND order_id=%s AND created_at<=%s ORDER BY created_at,id LIMIT 101",
                (tenant_id, owner_id, order_id, snapshot),
            )
            decision = None
            if row.get("decision_snapshot_id"):
                decision = _one(
                    connection,
                    """SELECT id,evidence_digest,evidence_json FROM paper_decision_snapshots
                    WHERE tenant_id=%s AND owner_user_id=%s AND id=%s""",
                    (tenant_id, owner_id, row["decision_snapshot_id"]),
                )
        result = self._public(row)
        result.update(
            {
                "fills": [self._public(fill) for fill in fills[:100]],
                "events": events[:100],
                "fills_has_more": len(fills) > 100,
                "events_has_more": len(events) > 100,
                "history_limit": 100,
                "decision_snapshot": decision,
            }
        )
        for kind, time_col, records in (("fills", "filled_at", fills), ("events", "created_at", events)):
            scope = scope_digest(tenant=tenant_id, owner=owner_id, order=order_id, kind=kind)
            result[kind + "_next_cursor"] = (
                encode_cursor(scope, records[99][time_col], records[99]["id"], snapshot)
                if len(records) > 100
                else None
            )
        return _json(result)

    def order_history(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        order_id: UUID,
        *,
        kind: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from gatescope.domain.paper_pagination import decode_cursor, encode_cursor, scope_digest

        _integer(limit, "limit", high=100)
        if kind not in {"fills", "events"}:
            raise ValueError("PAPER_HISTORY_KIND_INVALID")
        table, time_col = (
            ("paper_order_fills", "filled_at") if kind == "fills" else ("paper_order_events", "created_at")
        )
        projection = (
            "*"
            if kind == "fills"
            else "id,event_type,payload_json-'quote_snapshot' AS payload_json,created_at"
        )
        scope = scope_digest(tenant=tenant_id, owner=owner_id, order=order_id, kind=kind)
        clauses = ["tenant_id=%s", "owner_user_id=%s", "order_id=%s"]
        args: list[Any] = [tenant_id, owner_id, order_id]
        snapshot = _now(None)
        if cursor is not None:
            moment, identity, snapshot = decode_cursor(cursor, scope)
            clauses.append(f"({time_col},id)>(%s,%s)")
            args.extend((moment, identity))
        clauses.append(f"{time_col}<=%s")
        args.append(snapshot)
        with self.pool.read_transaction(tenant_id, owner_id) as conn:
            if (
                _one(
                    conn,
                    "SELECT id FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                    (tenant_id, owner_id, order_id),
                )
                is None
            ):
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            rows = _many(
                conn,
                f"SELECT {projection} FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {time_col},id LIMIT %s",
                (*args, limit + 1),
            )
        more = len(rows) > limit
        return _json(
            {
                "environment": "PAPER",
                "items": [self._public(r) for r in rows[:limit]],
                "limit": limit,
                "has_more": more,
                "snapshot_at": snapshot,
                "pagination": "CURSOR",
                "next_cursor": encode_cursor(
                    scope, rows[limit - 1][time_col], rows[limit - 1]["id"], snapshot
                )
                if more
                else None,
            }
        )

    def get_position(self, tenant_id: UUID, owner_id: UUID, position_id: UUID) -> dict[str, Any]:
        return self._get("paper_positions", tenant_id, owner_id, position_id)

    def replay_request(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        request: Mapping[str, Any],
        *,
        effect: str = "OPEN",
        position_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        payload = normalized_request(request, effect=effect, position_id=position_id)
        with self.pool.read_transaction(tenant_id, owner_id) as connection:
            return self._replay(connection, tenant_id, owner_id, payload)

    def _replay(
        self, connection: Any, tenant_id: UUID, owner_id: UUID, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        row = _one(
            connection,
            "SELECT * FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND idempotency_key=%s",
            (tenant_id, owner_id, payload["idempotency_key"]),
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash(payload):
            raise PaperConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
        return self._result(connection, row, replayed=True)

    @staticmethod
    def _quote(quote: Any, symbol: str, at: datetime) -> None:
        if quote is None:
            raise PaperPreconditionFailed("MARKET_UNAVAILABLE")
        book = quote.book
        if quote.spec.symbol != symbol or book.symbol != symbol:
            raise PaperPreconditionFailed("QUOTE_DIMENSION_MISMATCH")
        if book.source_at.tzinfo is None or not timedelta(seconds=-2) <= at - book.source_at <= timedelta(
            seconds=5
        ):
            raise PaperPreconditionFailed("QUOTE_STALE")
        if quote.metadata_as_of.tzinfo is None or not timedelta(
            seconds=-2
        ) <= at - quote.metadata_as_of <= timedelta(seconds=60):
            raise PaperPreconditionFailed("CONTRACT_METADATA_STALE")

    def submit(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        request: Mapping[str, Any],
        quote: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._submit(tenant_id, owner_id, request, quote, now=now)

    def close(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        position_id: UUID,
        request: Mapping[str, Any],
        quote: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._submit(tenant_id, owner_id, request, quote, position_id=position_id, now=now)

    def _submit(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        request: Mapping[str, Any],
        quote: Any,
        *,
        position_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        payload = normalized_request(
            request, effect="CLOSE" if position_id else "OPEN", position_id=position_id
        )
        at = _now(now)
        started = time.monotonic()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            connection.execute("SET LOCAL lock_timeout='3s'")
            if not self._actor_can_trade(connection):
                raise PermissionError("PAPER_TRADE_ROLE_REQUIRED")
            # Serialize a request key even if reused maliciously across accounts.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"paper-request:{tenant_id}:{owner_id}:{payload['idempotency_key']}",),
            )
            replay = self._replay(connection, tenant_id, owner_id, payload)
            if replay is not None:
                return replay
            position = None
            if position_id:
                position = _one(
                    connection,
                    "SELECT * FROM paper_positions WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                    (tenant_id, owner_id, position_id),
                )
                if position is None:
                    raise PaperNotFound("PAPER_POSITION_NOT_FOUND")
                account_id = position["account_id"]
            else:
                account_id = UUID(payload["account_id"])
            account = self._account(connection, tenant_id, owner_id, account_id, lock=True)
            if position_id:
                position = _one(
                    connection,
                    "SELECT * FROM paper_positions WHERE tenant_id=%s AND owner_user_id=%s AND id=%s FOR UPDATE",
                    (tenant_id, owner_id, position_id),
                )
            if not self._actor_can_trade(connection):
                raise PermissionError("PAPER_TRADE_ROLE_REQUIRED")
            initial_at = at
            at = initial_at + timedelta(seconds=time.monotonic() - started)
            order = self._create(connection, tenant_id, owner_id, account, payload, position, quote, at)
            if order["status"] == "REJECTED":
                return self._result(connection, order, fills=[])
            return self._match(
                connection, account, order, quote, initial_at + timedelta(seconds=time.monotonic() - started)
            )

    def _create(
        self,
        connection: Any,
        tenant_id: UUID,
        owner_id: UUID,
        account: Mapping[str, Any],
        payload: Mapping[str, Any],
        position: Mapping[str, Any] | None,
        quote: Any,
        at: datetime,
        *,
        protective: bool = False,
    ) -> dict[str, Any]:
        from gatescope.domain.paper_matching import validate_protection

        if account["base_currency"] != "USDT" or (account["status"] != "ACTIVE" and position is None):
            raise PaperPreconditionFailed("PAPER_ACCOUNT_UNAVAILABLE")
        symbol = position["symbol"] if position else payload["symbol"]
        self._quote(quote, symbol, at)
        side = position["side"] if position else payload["side"]
        effect = "CLOSE" if position else "OPEN"
        execution_side = "BUY" if (side == "LONG") == (effect == "OPEN") else "SELL"
        spec, book = quote.spec, quote.book
        if position is None and spec.min_contracts > 1:
            raise PaperPreconditionFailed("UNSUPPORTED_MINIMUM_CONTRACTS_FOR_SAFE_PARTIAL_EXITS")
        levels = book.asks if execution_side == "BUY" else book.bids
        if not levels:
            raise PaperPreconditionFailed("NO_EXECUTABLE_BOOK")
        reference = (
            Decimal(payload["limit_price"]) if payload["order_type"] == "LIMIT" else Decimal(levels[0].price)
        )
        if payload["contracts"] is not None:
            contracts = int(payload["contracts"])
        elif position is not None:
            contracts = int(position["remaining_contracts"] - position["reserved_close_contracts"])
        else:
            raise ValueError("CONTRACTS_REQUIRED")
        if (
            contracts < spec.min_contracts
            or contracts > spec.max_contracts
            or (
                payload["order_type"] == "MARKET"
                and spec.market_max_contracts is not None
                and contracts > spec.market_max_contracts
            )
        ):
            raise ValueError("CONTRACT_SIZE_LIMIT")
        if payload["order_type"] == "LIMIT" and reference % spec.price_tick:
            raise ValueError("LIMIT_PRICE_TICK_MISMATCH")
        multiplier = Decimal(spec.multiplier)
        fee_rate = max(Decimal(spec.taker_fee_rate), Decimal(account["fee_bps_per_side"]) / 10000)
        if fee_rate > Decimal(".01"):
            raise PaperPreconditionFailed("UNSUPPORTED_FEE_RATE")
        reserved_notional = reserved_risk = reserved_fees = ZERO
        rejection = None
        if position:
            if (
                position["status"] != "OPEN"
                or contracts > position["remaining_contracts"] - position["reserved_close_contracts"]
            ):
                raise PaperConflict("REDUCE_ONLY_QUANTITY_UNAVAILABLE")
            if multiplier != Decimal(position["multiplier"]):
                raise PaperPreconditionFailed("CONTRACT_MULTIPLIER_CHANGED")
            if position["trigger_reason"] and not protective:
                raise PaperConflict("PROTECTIVE_EXIT_ALREADY_TRIGGERED")
            for key in ("account_id", "instrument_id", "symbol", "timeframe"):
                if payload.get(key) is not None and str(payload[key]) != str(position[key]):
                    raise ValueError("CLOSE_POSITION_DIMENSION_MISMATCH")
            opening = _one(
                connection,
                "SELECT * FROM paper_orders WHERE id=%s AND status=ANY(%s) FOR UPDATE",
                (position["opening_order_id"], list(ACTIVE)),
            )
            if opening is not None:
                self._finish(connection, opening, "CANCELED", "OPENING_LOT_EXIT_STARTED", at)
            connection.execute(
                "UPDATE paper_positions SET reserved_close_contracts=reserved_close_contracts+%s WHERE id=%s",
                (contracts, position["id"]),
            )
            stop, target = Decimal(position["stop_price"]), Decimal(position["take_profit_price"])
        else:
            identity = connection.execute(
                "SELECT symbol,venue,market,settle_asset,quote_asset,status FROM instruments WHERE id=%s",
                (UUID(payload["instrument_id"]),),
            ).fetchone()
            if (
                identity is None
                or identity[0] != symbol
                or str(identity[1]).lower() not in ("gate", "gateio", "gate.io")
                or str(identity[2]).upper() not in ("FUTURES_USDT", "PERPETUAL", "PERP", "FUTURES")
                or identity[3] != "USDT"
                or identity[4] != "USDT"
                or identity[5] != "ACTIVE"
            ):
                raise PaperPreconditionFailed("UNSUPPORTED_PAPER_INSTRUMENT")
            stop, target = Decimal(payload["stop_price"]), Decimal(payload["take_profit_price"])
            validate_protection(side, reference, stop, target, spec.price_tick)
            # Freeze conservative cost/risk bounds, independent of whether the limit crosses now.
            bound = (
                reference * (1 + Decimal(payload["max_slippage_bps"]) / 10000)
                if payload["order_type"] == "MARKET"
                else reference
            )
            reserved_notional = bound * contracts * multiplier
            reserved_fees = reserved_notional * fee_rate
            reserved_risk = (
                max(
                    abs(bound - stop),
                    abs(reference * (1 - Decimal(payload["max_slippage_bps"]) / 10000) - stop),
                )
                * contracts
                * multiplier
                + reserved_fees
                + stop * contracts * multiplier * fee_rate
            )
            budget = self.budgets(connection, tenant_id, owner_id, account)
            if reserved_notional + reserved_fees > budget["available_balance"]:
                rejection = "INSUFFICIENT_PAPER_BALANCE"
            elif budget["notional_used"] + reserved_notional > budget["notional_limit"]:
                rejection = "ACCOUNT_NOTIONAL_LIMIT"
            elif budget["risk_used"] + reserved_risk > budget["risk_limit"]:
                rejection = "ACCOUNT_RISK_LIMIT"
            if rejection:
                reserved_notional = reserved_risk = reserved_fees = ZERO
        decision_id = None
        if position:
            original = connection.execute(
                "SELECT decision_snapshot_id FROM paper_orders WHERE id=%s", (position["opening_order_id"],)
            ).fetchone()
            decision_id = original[0] if original else None
        elif payload.get("decision_context") is not None:
            from gatescope.adapters.postgres_paper_decisions import decision_snapshot

            decision_id = decision_snapshot(
                connection,
                tenant_id,
                owner_id,
                UUID(payload["instrument_id"]),
                symbol,
                payload["timeframe"],
                payload["decision_context"],
                now=at,
            )
        row = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "owner_user_id": owner_id,
            "account_id": account["id"],
            "decision_snapshot_id": decision_id,
            "instrument_id": position["instrument_id"] if position else UUID(payload["instrument_id"]),
            "symbol": symbol,
            "timeframe": position["timeframe"] if position else payload["timeframe"],
            "position_id": position["id"] if position else None,
            "environment": "PAPER",
            "effect": effect,
            "side": side,
            "execution_side": execution_side,
            "order_type": payload["order_type"],
            "time_in_force": payload["time_in_force"],
            "status": "REJECTED" if rejection else "NEW",
            "contracts": contracts,
            "filled_contracts": 0,
            "remaining_contracts": contracts,
            "average_fill_price": None,
            "multiplier": multiplier,
            "limit_price": Decimal(payload["limit_price"]) if payload.get("limit_price") else None,
            "reference_price": reference,
            "stop_price": stop,
            "take_profit_price": target,
            "max_slippage_bps": Decimal(payload["max_slippage_bps"]),
            "fee_rate": fee_rate,
            "reserved_notional": reserved_notional,
            "reserved_risk": reserved_risk,
            "reserved_fees": reserved_fees,
            "idempotency_key": payload["idempotency_key"],
            "request_hash": request_hash(payload),
            "request_json": dict(payload),
            "source": payload["source"],
            "reason": position["trigger_reason"] if protective and position else rejection,
            "expires_at": at + timedelta(seconds=payload["ttl_seconds"]),
            "created_at": at,
            "updated_at": at,
            "version": 1,
        }
        columns = list(row)
        connection.execute(
            f"INSERT INTO paper_orders ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})",
            tuple(Jsonb(_json(row[name])) if name == "request_json" else row[name] for name in columns),
        )
        self._event(
            connection,
            row,
            "REJECTED" if rejection else "ACCEPTED",
            {"request": payload, "metadata": spec.to_dict(), "environment": "PAPER", "reason": rejection},
            at,
        )
        return row

    def _match(
        self, connection: Any, account: Mapping[str, Any], order: dict[str, Any], quote: Any, at: datetime
    ) -> dict[str, Any]:
        from gatescope.domain.paper_matching import match_order, validate_protection

        started = time.monotonic()
        self._quote(quote, order["symbol"], at)
        before_status = order["status"]
        if Decimal(quote.spec.multiplier) != Decimal(order["multiplier"]):
            return self._finish(connection, order, "CANCELED", "CONTRACT_MULTIPLIER_CHANGED", at)
        if order["effect"] == "OPEN" and Decimal(quote.spec.taker_fee_rate) > Decimal(order["fee_rate"]):
            return self._finish(connection, order, "CANCELED", "FEE_SCHEDULE_CHANGED", at)
        # Immutable quote identity and global per-price consumption prevent concurrent
        # accounts, retries or repeated polling from reusing the same book volume.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"paper-book:{quote.book.quote_id}:{order['execution_side']}",),
        )
        at += timedelta(seconds=time.monotonic() - started)
        self._quote(quote, order["symbol"], at)
        used = connection.execute(
            "SELECT price,contracts FROM paper_quote_consumption WHERE quote_id=%s AND execution_side=%s",
            (quote.book.quote_id, order["execution_side"]),
        ).fetchall()
        result = match_order(
            quote.spec,
            quote.book,
            side=order["execution_side"],
            contracts=int(order["remaining_contracts"]),
            order_type=order["order_type"],
            time_in_force=order["time_in_force"],
            limit_price=order["limit_price"],
            max_slippage_bps=order["max_slippage_bps"],
            now=at,
            consumed={Decimal(row[0]): int(row[1]) for row in used},
            remaining_order=bool(order["filled_contracts"]),
        )
        order["fee_rate"] = max(Decimal(order["fee_rate"]), Decimal(quote.spec.taker_fee_rate))
        # Pending entries cannot acquire a fill outside the originally protected plan.
        if order["effect"] == "OPEN":
            try:
                for fill in result.fills:
                    validate_protection(
                        order["side"],
                        fill.price,
                        order["stop_price"],
                        order["take_profit_price"],
                        quote.spec.price_tick,
                    )
            except ValueError:
                return self._finish(connection, order, "CANCELED", "PROTECTION_INVALID_AT_EXECUTION", at)
            if result.fills:
                budget = self.budgets(connection, order["tenant_id"], order["owner_user_id"], account)
                fraction = (
                    Decimal(result.remaining_contracts) / Decimal(order["remaining_contracts"])
                    if order["time_in_force"] == "GTC"
                    else ZERO
                )
                fee = sum((fill.notional * order["fee_rate"] for fill in result.fills), ZERO)
                notional = sum((fill.notional for fill in result.fills), ZERO)
                risk = sum(
                    (
                        (abs(fill.price - order["stop_price"]) + order["stop_price"] * order["fee_rate"])
                        * fill.base_quantity
                        for fill in result.fills
                    ),
                    ZERO,
                )
                cash = budget["cash_balance"] - fee
                after_notional = (
                    budget["notional_used"] - order["reserved_notional"] * (1 - fraction) + notional
                )
                after_risk = budget["risk_used"] - order["reserved_risk"] * (1 - fraction) + risk
                after_reserved_fees = budget["order_reserved_fees"] - order["reserved_fees"] * (1 - fraction)
                if (
                    cash - after_notional - after_reserved_fees - budget["legacy_open_fees"] < 0
                    or after_notional
                    > (cash - budget["legacy_open_fees"]) * Decimal(account["max_notional_fraction"])
                    or after_risk > (cash - budget["legacy_open_fees"]) * Decimal(account["risk_fraction"])
                ):
                    return self._finish(connection, order, "CANCELED", "BUDGET_CHANGED_BEFORE_FILL", at)
        filled: list[dict[str, Any]] = []
        for fill in result.fills:
            frozen = {
                "quote_id": quote.book.quote_id,
                "source_at": quote.book.source_at,
                "captured_at": quote.book.captured_at,
                "updated_at": quote.book.updated_at,
                "metadata_as_of": quote.metadata_as_of,
                "spec": quote.spec.to_dict(),
                "execution_side": order["execution_side"],
                "price": fill.price,
                "observed_level_contracts": next(
                    level.contracts
                    for level in (quote.book.asks if order["execution_side"] == "BUY" else quote.book.bids)
                    if level.price == fill.price
                ),
                "participation_rate": result.participation_rate,
                "model_version": result.model_version,
            }
            fee = Decimal(fill.notional) * Decimal(order["fee_rate"])
            position = self._apply_position(connection, order, fill, fee, at)
            fact = {
                "id": uuid4(),
                "tenant_id": order["tenant_id"],
                "owner_user_id": order["owner_user_id"],
                "account_id": order["account_id"],
                "order_id": order["id"],
                "position_id": position["id"],
                "instrument_id": order["instrument_id"],
                "symbol": order["symbol"],
                "timeframe": order["timeframe"],
                "environment": "PAPER",
                "effect": order["effect"],
                "side": order["side"],
                "execution_side": order["execution_side"],
                "contracts": fill.contracts,
                "price": fill.price,
                "base_quantity": fill.base_quantity,
                "notional": fill.notional,
                "fee": fee,
                "realized_pnl": position["fill_realized_pnl"],
                "quote_id": quote.book.quote_id,
                "quote_snapshot": frozen,
                "reason": order["reason"]
                if order["reason"] in ("STOP_LOSS", "TAKE_PROFIT")
                else "USER_ORDER",
                "filled_at": at,
            }
            columns = list(fact)
            connection.execute(
                f"INSERT INTO paper_order_fills({','.join(columns)}) VALUES({','.join(['%s'] * len(columns))})",
                tuple(Jsonb(_json(fact[key])) if key == "quote_snapshot" else fact[key] for key in columns),
            )
            connection.execute(
                """INSERT INTO paper_quote_consumption(quote_id,execution_side,price,contracts,updated_at) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(quote_id,execution_side,price) DO UPDATE SET contracts=paper_quote_consumption.contracts+excluded.contracts,updated_at=excluded.updated_at""",
                (quote.book.quote_id, order["execution_side"], fill.price, fill.contracts, at),
            )
            connection.execute(
                "UPDATE paper_accounts SET realized_pnl=realized_pnl+%s,etag=etag+1,updated_at=%s WHERE id=%s AND tenant_id=%s AND owner_user_id=%s",
                (
                    position["fill_realized_pnl"] - fee,
                    at,
                    order["account_id"],
                    order["tenant_id"],
                    order["owner_user_id"],
                ),
            )
            previous_qty = int(order["filled_contracts"])
            order["average_fill_price"] = (
                (Decimal(order["average_fill_price"] or 0) * previous_qty)
                + Decimal(fill.price) * fill.contracts
            ) / (previous_qty + fill.contracts)
            order["filled_contracts"] += fill.contracts
            order["remaining_contracts"] -= fill.contracts
            order["position_id"] = position["id"]
            filled.append(fact)
        remaining = order["remaining_contracts"]
        if remaining == 0:
            status = "FILLED"
        elif order["time_in_force"] == "GTC":
            status = "PARTIALLY_FILLED" if order["filled_contracts"] else "OPEN"
        else:
            status = "CANCELED"
        if status in ACTIVE:
            fraction = Decimal(remaining) / Decimal(
                order["contracts"] - order["filled_contracts"] + sum(f["contracts"] for f in filled)
            )
            for field in ("reserved_notional", "reserved_risk", "reserved_fees"):
                order[field] *= fraction
        else:
            if order["effect"] == "CLOSE" and remaining:
                connection.execute(
                    "UPDATE paper_positions SET reserved_close_contracts=reserved_close_contracts-%s WHERE id=%s",
                    (remaining, order["position_id"]),
                )
            order["reserved_notional"] = order["reserved_risk"] = order["reserved_fees"] = ZERO
        order.update(
            {"status": status, "last_quote_id": quote.book.quote_id, "last_checked_at": at, "updated_at": at}
        )
        if order["reason"] not in ("STOP_LOSS", "TAKE_PROFIT"):
            order["reason"] = (
                str(result.reason)
                if result.reason
                else ("FILLED" if status == "FILLED" else "WAITING_LIQUIDITY")
            )
        self._save(connection, order)
        if filled or before_status != status:
            self._event(
                connection,
                order,
                "MATCHED",
                {
                    "status": status,
                    "quote_id": quote.book.quote_id,
                    "filled_contracts": sum(f["contracts"] for f in filled),
                    "remaining_contracts": remaining,
                    "reason": order["reason"],
                    "quote_snapshot": quote.to_dict() if filled else None,
                },
                at,
            )
        if filled:
            self._notify(connection, order, filled, at)
        return self._result(connection, order, fills=filled)

    def _apply_position(
        self, connection: Any, order: dict[str, Any], fill: Any, fee: Decimal, at: datetime
    ) -> dict[str, Any]:
        if order["effect"] == "OPEN":
            position = _one(
                connection,
                "SELECT * FROM paper_positions WHERE opening_order_id=%s FOR UPDATE",
                (order["id"],),
            )
            if position is None:
                identity = uuid4()
                connection.execute(
                    """INSERT INTO paper_positions(id,tenant_id,owner_user_id,account_id,opening_order_id,instrument_id,symbol,timeframe,side,
                    opened_contracts,remaining_contracts,multiplier,average_entry_price,stop_price,take_profit_price,fee_rate,fees,status,opened_at,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s)""",
                    (
                        identity,
                        order["tenant_id"],
                        order["owner_user_id"],
                        order["account_id"],
                        order["id"],
                        order["instrument_id"],
                        order["symbol"],
                        order["timeframe"],
                        order["side"],
                        fill.contracts,
                        fill.contracts,
                        order["multiplier"],
                        fill.price,
                        order["stop_price"],
                        order["take_profit_price"],
                        order["fee_rate"],
                        fee,
                        at,
                        at,
                    ),
                )
                return {"id": identity, "fill_realized_pnl": ZERO}
            if position["status"] != "OPEN" or position["trigger_reason"]:
                raise PaperConflict("OPENING_LOT_NO_LONGER_ACCEPTS_FILLS")
            average = (
                Decimal(position["average_entry_price"]) * position["opened_contracts"]
                + fill.price * fill.contracts
            ) / (position["opened_contracts"] + fill.contracts)
            connection.execute(
                "UPDATE paper_positions SET opened_contracts=opened_contracts+%s,remaining_contracts=remaining_contracts+%s,average_entry_price=%s,fees=fees+%s,updated_at=%s WHERE id=%s",
                (fill.contracts, fill.contracts, average, fee, at, position["id"]),
            )
            return {"id": position["id"], "fill_realized_pnl": ZERO}
        position = _one(
            connection, "SELECT * FROM paper_positions WHERE id=%s FOR UPDATE", (order["position_id"],)
        )
        if (
            position is None
            or fill.contracts > position["remaining_contracts"]
            or fill.contracts > position["reserved_close_contracts"]
        ):
            raise PaperConflict("REDUCE_ONLY_POSITION_CONFLICT")
        pnl = (
            (fill.price - Decimal(position["average_entry_price"]))
            * fill.base_quantity
            * (1 if position["side"] == "LONG" else -1)
        )
        remaining = position["remaining_contracts"] - fill.contracts
        average = (
            Decimal(position["average_exit_price"] or 0) * position["closed_contracts"]
            + fill.price * fill.contracts
        ) / (position["closed_contracts"] + fill.contracts)
        connection.execute(
            """UPDATE paper_positions SET closed_contracts=closed_contracts+%s,remaining_contracts=%s,reserved_close_contracts=reserved_close_contracts-%s,
            average_exit_price=%s,realized_pnl=realized_pnl+%s,fees=fees+%s,status=%s,protection_status=%s,closed_at=%s,updated_at=%s WHERE id=%s""",
            (
                fill.contracts,
                remaining,
                fill.contracts,
                average,
                pnl,
                fee,
                "OPEN" if remaining else "CLOSED",
                position["protection_status"] if remaining else "COMPLETED",
                None if remaining else at,
                at,
                position["id"],
            ),
        )
        return {"id": position["id"], "fill_realized_pnl": pnl}

    @staticmethod
    def _save(connection: Any, order: Mapping[str, Any]) -> None:
        names = (
            "position_id",
            "status",
            "filled_contracts",
            "remaining_contracts",
            "average_fill_price",
            "reserved_notional",
            "reserved_risk",
            "reserved_fees",
            "fee_rate",
            "reason",
            "last_quote_id",
            "last_checked_at",
            "updated_at",
        )
        connection.execute(
            "UPDATE paper_orders SET "
            + ",".join(name + "=%s" for name in names)
            + ",version=version+1 WHERE id=%s AND tenant_id=%s AND owner_user_id=%s",
            tuple(order.get(name) for name in names)
            + (order["id"], order["tenant_id"], order["owner_user_id"]),
        )

    @staticmethod
    def _event(
        connection: Any, order: Mapping[str, Any], event: str, payload: Mapping[str, Any], at: datetime
    ) -> None:
        connection.execute(
            "INSERT INTO paper_order_events(id,tenant_id,owner_user_id,order_id,event_type,payload_json,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                order["tenant_id"],
                order["owner_user_id"],
                order["id"],
                event,
                Jsonb(_json(payload)),
                at,
            ),
        )

    @staticmethod
    def _notify(connection: Any, order: Mapping[str, Any], fills: list[dict[str, Any]], at: datetime) -> None:
        quantity = sum(fill["contracts"] for fill in fills)
        payload = {
            "owner_user_id": order["owner_user_id"],
            "account_id": order["account_id"],
            "order_id": order["id"],
            "position_id": order["position_id"],
            "instrument_id": order["instrument_id"],
            "side": order["side"],
            "effect": order["effect"],
            "contracts": quantity,
            "fill_price": sum(fill["price"] * fill["contracts"] for fill in fills) / quantity,
            "fee": sum((fill["fee"] for fill in fills), ZERO),
            "realized_pnl": sum((fill["realized_pnl"] for fill in fills), ZERO),
            "state": order["status"],
            "reason": fills[0]["reason"],
            "symbol": order["symbol"],
            "timeframe": order["timeframe"],
            "environment": "PAPER",
            "filled_contracts": order["filled_contracts"],
            "remaining_contracts": order["remaining_contracts"],
            "fill_ids": [fill["id"] for fill in fills],
            "filled_at": at,
        }
        connection.execute(
            """INSERT INTO outbox_events(event_id,tenant_id,aggregate_type,aggregate_id,event_type,schema_version,payload_json,occurred_at,created_at,next_attempt_at)
            VALUES(%s,%s,'paper_order',%s,'paper.order.fill.v1',1,%s,%s,%s,%s)""",
            (uuid4(), order["tenant_id"], order["id"], Jsonb(_json(payload)), at, at, at),
        )

    def _result(
        self,
        connection: Any,
        order: Mapping[str, Any],
        *,
        replayed: bool = False,
        fills: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if fills is None:
            fills = _many(
                connection,
                "SELECT * FROM paper_order_fills WHERE tenant_id=%s AND owner_user_id=%s AND order_id=%s ORDER BY filled_at,id LIMIT 101",
                (order["tenant_id"], order["owner_user_id"], order["id"]),
            )
        return _json(
            {
                "environment": "PAPER",
                "accepted": order["status"] != "REJECTED",
                "replayed": replayed,
                "order": self._public(order),
                "fills": [
                    {key: value for key, value in self._public(fill).items() if key != "quote_snapshot"}
                    for fill in fills[:100]
                ],
                "fills_has_more": len(fills) > 100,
                "limitations": LIMITATIONS,
            }
        )

    def _finish(
        self, connection: Any, order: dict[str, Any], status: str, reason: str, at: datetime
    ) -> dict[str, Any]:
        if order["status"] in ACTIVE and order["effect"] == "CLOSE" and order["remaining_contracts"]:
            connection.execute(
                "UPDATE paper_positions SET reserved_close_contracts=reserved_close_contracts-%s WHERE id=%s",
                (order["remaining_contracts"], order["position_id"]),
            )
        order.update(
            {
                "status": status,
                "reason": reason,
                "updated_at": at,
                "last_checked_at": at,
                "reserved_notional": ZERO,
                "reserved_risk": ZERO,
                "reserved_fees": ZERO,
            }
        )
        self._save(connection, order)
        self._event(
            connection,
            order,
            status,
            {"reason": reason, "remaining_contracts": order["remaining_contracts"]},
            at,
        )
        return self._result(connection, order, fills=[])

    def cancel(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        order_id: UUID,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        at = _now(now)
        with self.pool.transaction(tenant_id, owner_id) as connection:
            if not self._actor_can_trade(connection):
                raise PermissionError("PAPER_TRADE_ROLE_REQUIRED")
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"paper-cancel:{tenant_id}:{owner_id}:{idempotency_key}",),
            )
            digest = request_hash(
                {"effect": "CANCEL", "order_id": str(order_id), "idempotency_key": idempotency_key}
            )
            command = _one(
                connection,
                "SELECT * FROM paper_order_commands WHERE tenant_id=%s AND owner_user_id=%s AND idempotency_key=%s",
                (tenant_id, owner_id, idempotency_key),
            )
            if command is not None and command["request_hash"] != digest:
                raise PaperConflict("IDEMPOTENCY_PAYLOAD_CONFLICT")
            order = _one(
                connection,
                "SELECT * FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                (tenant_id, owner_id, order_id),
            )
            if order is None:
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            self._account(connection, tenant_id, owner_id, order["account_id"], lock=True)
            order = _one(connection, "SELECT * FROM paper_orders WHERE id=%s FOR UPDATE", (order_id,))
            if order is None:
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            if not self._actor_can_trade(connection):
                raise PermissionError("PAPER_TRADE_ROLE_REQUIRED")
            if command is None:
                connection.execute(
                    "INSERT INTO paper_order_commands(id,tenant_id,owner_user_id,order_id,idempotency_key,request_hash,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (uuid4(), tenant_id, owner_id, order_id, idempotency_key, digest, at),
                )
            if order["status"] not in ACTIVE:
                return self._result(connection, order, replayed=True)
            return self._finish(connection, order, "CANCELED", "USER_CANCELED", at)

    def pending_targets(self, limit: int = 100) -> list[dict[str, Any]]:
        _integer(limit, "limit", high=100)
        with self.pool.public_transaction() as connection:
            return _many(connection, "SELECT * FROM gatescope_security.paper_order_due_targets(%s)", (limit,))

    def process_pending(
        self, tenant_id: UUID, owner_id: UUID, order_id: UUID, quote: Any, *, now: datetime | None = None
    ) -> dict[str, Any]:
        at = _now(now)
        started = time.monotonic()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            connection.execute("SET LOCAL lock_timeout='3s'")
            order = _one(
                connection,
                "SELECT * FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND id=%s",
                (tenant_id, owner_id, order_id),
            )
            if order is None:
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            account = self._account(connection, tenant_id, owner_id, order["account_id"], lock=True)
            order = _one(connection, "SELECT * FROM paper_orders WHERE id=%s FOR UPDATE", (order_id,))
            if order is None:
                raise PaperNotFound("PAPER_ORDER_NOT_FOUND")
            at += timedelta(seconds=time.monotonic() - started)
            if order["status"] not in ACTIVE:
                return self._result(connection, order, replayed=True)
            if at >= order["expires_at"]:
                return self._finish(connection, order, "EXPIRED", "TIME_IN_FORCE_EXPIRED", at)
            if order["effect"] == "OPEN" and (
                account["status"] != "ACTIVE" or not self._actor_can_trade(connection)
            ):
                return self._finish(connection, order, "CANCELED", "ACCOUNT_OR_OWNER_INACTIVE", at)
            if order["effect"] == "OPEN" and order["position_id"]:
                position = _one(
                    connection, "SELECT * FROM paper_positions WHERE id=%s", (order["position_id"],)
                )
                if (
                    position is None
                    or position["status"] != "OPEN"
                    or position["trigger_reason"]
                    or position["closed_contracts"]
                ):
                    return self._finish(connection, order, "CANCELED", "OPENING_LOT_EXIT_STARTED", at)
            try:
                self._quote(quote, order["symbol"], at)
            except PaperPreconditionFailed as exc:
                order.update({"reason": str(exc), "last_checked_at": at, "updated_at": at})
                self._save(connection, order)
                return self._result(connection, order, fills=[])
            return self._match(connection, account, order, quote, at)

    def _latch_protection(
        self,
        connection: Any,
        position: dict[str, Any],
        reason: str,
        quote: Any,
        at: datetime,
        *,
        observed_at: datetime,
    ) -> None:
        if position["trigger_reason"]:
            return
        connection.execute(
            "UPDATE paper_positions SET trigger_reason=%s,triggered_at=%s WHERE id=%s",
            (reason, observed_at, position["id"]),
        )
        position["trigger_reason"], position["triggered_at"] = reason, observed_at
        self._event(
            connection,
            {
                "id": position["opening_order_id"],
                "tenant_id": position["tenant_id"],
                "owner_user_id": position["owner_user_id"],
            },
            "PROTECTION_TRIGGERED",
            {
                "position_id": position["id"],
                "reason": reason,
                "stop_price": position["stop_price"],
                "take_profit_price": position["take_profit_price"],
                "observed_at": observed_at,
                "quote_id": quote.book.quote_id,
                "source_at": quote.book.source_at,
                "captured_at": quote.book.captured_at,
                "executable_side_price": quote.book.bids[0].price
                if position["side"] == "LONG"
                else quote.book.asks[0].price,
                "fill_guaranteed": False,
            },
            at,
        )

    def protect_positions(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        instrument_id: UUID,
        quote: Any,
        *,
        account_id: UUID | None = None,
        limit: int = 10,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from gatescope.domain.paper_matching import exit_trigger

        base_at, started = _now(now), time.monotonic()
        rows = self.list_positions(
            tenant_id,
            owner_id,
            account_id=account_id,
            instrument_id=instrument_id,
            status="ACTIVE",
            limit=limit,
        )["items"]
        results = []
        for item in rows:
            observed_at = base_at + timedelta(seconds=time.monotonic() - started)
            observed_trigger = None
            try:
                self._quote(quote, item["symbol"], observed_at)
                observed_trigger = exit_trigger(
                    item["side"],
                    quote.book,
                    stop_loss=Decimal(item["stop_price"]),
                    take_profit=Decimal(item["take_profit_price"]),
                    latched_reason=item["trigger_reason"],
                    now=observed_at,
                )
            except (PaperPreconditionFailed, ValueError):
                pass
            with self.pool.transaction(tenant_id, owner_id) as connection:
                connection.execute("SET LOCAL lock_timeout='3s'")
                account = self._account(connection, tenant_id, owner_id, UUID(item["account_id"]), lock=True)
                position = _one(
                    connection,
                    "SELECT * FROM paper_positions WHERE id=%s AND tenant_id=%s AND owner_user_id=%s FOR UPDATE",
                    (UUID(item["id"]), tenant_id, owner_id),
                )
                at = base_at + timedelta(seconds=time.monotonic() - started)
                if position is None or position["status"] != "OPEN":
                    continue
                try:
                    self._quote(quote, position["symbol"], at)
                except PaperPreconditionFailed:
                    # A fresh observation made before waiting for a lock remains
                    # valid trigger evidence, even though it is too old to FILL.
                    if observed_trigger:
                        self._latch_protection(
                            connection, position, observed_trigger, quote, at, observed_at=observed_at
                        )
                    connection.execute(
                        "UPDATE paper_positions SET protection_status='WAITING_MARKET',last_checked_at=%s,updated_at=%s WHERE id=%s",
                        (at, at, position["id"]),
                    )
                    results.append(
                        {
                            "position_id": str(position["id"]),
                            "status": "WAITING_MARKET",
                            "trigger_reason": position["trigger_reason"],
                        }
                    )
                    continue
                book = quote.book
                price = book.bids[0].price if position["side"] == "LONG" else book.asks[0].price
                reason = exit_trigger(
                    position["side"],
                    book,
                    stop_loss=position["stop_price"],
                    take_profit=position["take_profit_price"],
                    latched_reason=position["trigger_reason"],
                    now=at,
                )
                connection.execute(
                    "UPDATE paper_positions SET mark_price=%s,mark_as_of=%s,last_quote_id=%s,last_checked_at=%s,updated_at=%s,protection_status=%s WHERE id=%s",
                    (
                        price,
                        book.source_at,
                        book.quote_id,
                        at,
                        at,
                        "TRIGGERED" if reason else "ACTIVE",
                        position["id"],
                    ),
                )
                if not reason:
                    continue
                self._latch_protection(connection, position, reason, quote, at, observed_at=observed_at)
                # Protection preempts any user close/remaining entry; cancellation releases
                # all quantities before making an IOC exit, and the trigger survives retries.
                pending = _many(
                    connection,
                    "SELECT * FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND position_id=%s AND status=ANY(%s) FOR UPDATE",
                    (tenant_id, owner_id, position["id"], list(ACTIVE)),
                )
                for order in pending:
                    self._finish(connection, order, "CANCELED", "PROTECTIVE_EXIT_PREEMPTED", at)
                position["reserved_close_contracts"] = 0
                # One exit attempt per unchanged quote avoids creating endless empty orders.
                key = f"protect:{position['id']}:{book.quote_id}"[:128]
                if _one(
                    connection,
                    "SELECT id FROM paper_orders WHERE tenant_id=%s AND owner_user_id=%s AND idempotency_key=%s",
                    (tenant_id, owner_id, key),
                ):
                    continue
                max_contracts = int(
                    min(quote.spec.max_contracts, quote.spec.market_max_contracts or quote.spec.max_contracts)
                )
                payload = normalized_request(
                    {
                        "contracts": min(position["remaining_contracts"], max_contracts),
                        "order_type": "MARKET",
                        "time_in_force": "IOC",
                        "max_slippage_bps": 100,
                        "ttl_seconds": 60,
                        "source": "SYSTEM",
                        "idempotency_key": key,
                    },
                    effect="CLOSE",
                    position_id=position["id"],
                )
                try:
                    # Only the attempt rolls back on failure; the trigger above is
                    # already durable in the outer transaction and cannot disappear.
                    with connection.transaction():
                        order = self._create(
                            connection,
                            tenant_id,
                            owner_id,
                            account,
                            payload,
                            position,
                            quote,
                            at,
                            protective=True,
                        )
                        results.append(self._match(connection, account, order, quote, at))
                except (PaperPreconditionFailed, PaperConflict, ValueError, PostgresError) as exc:
                    failure_status = (
                        "WAITING_MARKET" if isinstance(exc, PaperPreconditionFailed) else "EXECUTION_RETRY"
                    )
                    connection.execute(
                        "UPDATE paper_positions SET protection_status=%s WHERE id=%s",
                        (failure_status, position["id"]),
                    )
                    results.append(
                        {
                            "position_id": str(position["id"]),
                            "status": failure_status,
                            "trigger_reason": reason,
                        }
                    )
        return {"environment": "PAPER", "items": results, "checked": len(rows)}
