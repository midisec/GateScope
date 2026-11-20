"""Read model used by the PostgreSQL V2 market API."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta, timezone
from uuid import UUID

from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.history import year_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS


class PostgresMarketReadDatabase:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def search_instruments(self, query: str, market: str, limit: int, offset: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            clauses.append("(symbol ILIKE %s OR base_asset ILIKE %s OR quote_asset ILIKE %s)")
            needle = f"%{query.strip()}%"
            params.extend([needle, needle, needle])
        if market == "spot":
            clauses.append("market = 'SPOT'")
        elif market.startswith("futures:"):
            settle = market.split(":", 1)[1].upper()
            clauses.append("market = %s AND settle_asset = %s")
            params.extend([f"FUTURES_{settle}", settle])
        elif market == "stock":
            return []
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 501)), max(0, offset)])
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                f"""SELECT id, market, settle_asset, symbol, base_asset, quote_asset, status
                    FROM instruments {where}
                    ORDER BY market, symbol, id LIMIT %s OFFSET %s""",
                params,
            ).fetchall()
        return [
            {
                "id": str(row[0]), "market": str(row[1]).lower(), "settle": str(row[2] or "").lower(),
                "symbol": str(row[3]), "display_name": str(row[3]), "base": str(row[4]),
                "quote": str(row[5]), "status": str(row[6]), "is_stock": False,
            }
            for row in rows
        ]

    def instrument_count(self) -> int:
        with self.pool.public_transaction() as connection:
            row = connection.execute("SELECT COUNT(*) FROM instruments").fetchone()
        return int(row[0]) if row else 0

    def latest_chart_snapshot(self,instrument_id:str,timeframe:str,limit:int):
        if limit>1500:return None
        with self.pool.public_transaction() as c:
            row=c.execute("""SELECT built_at,bar_count,
                COALESCE((SELECT jsonb_agg(value ORDER BY ord) FROM jsonb_array_elements(payload_json->'items')
                  WITH ORDINALITY AS items(value,ord) WHERE ord>bar_count-%s),'[]'::jsonb)
                FROM market_chart_snapshots WHERE instrument_id=%s AND timeframe=%s
                AND generation=built_generation AND bar_count>0""",(limit,UUID(instrument_id),timeframe)).fetchone()
        if not row:return None
        return {"items":row[2],"built_at":row[0].isoformat(),"bar_count":row[1]}

    def cache_status(self, instrument_id: str, timeframe: str) -> dict[str, Any]:
        with self.pool.public_transaction() as c:
            row = c.execute("""SELECT active,target_bars,cached_bars,missing_bars,first_open_time,
                last_close_time,last_live_at,next_live_at,live_error_code,history_status,history_error_code,
                last_history_at,next_history_at,target_from,target_to FROM market_sync_state WHERE instrument_id=%s AND timeframe=%s""",
                (UUID(instrument_id), timeframe)).fetchone()
            live = c.execute("""SELECT open_time,close_time,open,high,low,close,volume,observed_at
                FROM market_live_candles WHERE instrument_id=%s AND timeframe=%s
                AND close_time>now()""", (UUID(instrument_id), timeframe)).fetchone()
        now = datetime.now(timezone.utc)
        if row is None:
            result = {"active": False, "history_status": "NOT_SUBSCRIBED", "target_bars": year_bounds(now,timeframe)[2], "stale": True}
        else:
            result = dict(zip(("active", "target_bars", "cached_bars", "missing_bars", "first_open_time",
                "last_close_time", "last_live_at", "next_live_at", "live_error_code", "history_status",
                "history_error_code", "last_history_at", "next_history_at","target_from","target_to"), row, strict=True))
            result["stale"] = not row[0] or bool(row[8]) or not row[6] or (now - row[6]).total_seconds() > 45
        result["server_time"] = now
        result["period_days"] = 365
        result["coverage_ratio"] = result.get("cached_bars",0)/result["target_bars"]
        result["live_bar"] = ({"open_time": live[0].isoformat(), "close_time": live[1].isoformat(),
            **{name: str(value) for name, value in zip(("open", "high", "low", "close", "volume"), live[2:7], strict=True)},
            "available_at": live[7].isoformat(), "is_final": False, "revision": 0,
            "quality_flags": ["FORMING_CANDLE", "DISPLAY_ONLY"]} if live else None)
        return result

    def watchlist_cache(self, tenant: UUID, user: UUID) -> list[dict[str, Any]]:
        with self.pool.transaction(tenant, user) as c:
            rows = c.execute("""SELECT DISTINCT wi.instrument_id,wi.default_timeframe,
                lc.close,lc.observed_at,s.last_live_at,s.history_status,s.cached_bars,s.target_bars,s.active
                FROM watchlist_items wi JOIN watchlists w ON w.id=wi.watchlist_id
                LEFT JOIN market_sync_state s ON s.instrument_id=wi.instrument_id AND s.timeframe=wi.default_timeframe
                LEFT JOIN market_live_candles lc ON lc.instrument_id=wi.instrument_id AND lc.timeframe=wi.default_timeframe
                WHERE w.deleted_at IS NULL AND w.tenant_id=%s AND w.owner_user_id=%s
                LIMIT 1000""", (tenant, user)).fetchall()
        now = datetime.now(timezone.utc)
        return [{"instrument_id": str(r[0]), "timeframe": r[1], "price": str(r[2]) if r[2] is not None else None,
            "observed_at": r[3], "stale": not r[8] or not r[3] or (now - r[3]).total_seconds() > 45,
            "history_status": r[5] or "PENDING", "cached_bars": r[6] or 0, "target_bars": r[7] or year_bounds(now,r[1])[2]} for r in rows]

    def get_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        try:
            identifier = UUID(instrument_id)
        except ValueError:
            return None
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT id, market, settle_asset, symbol, base_asset, quote_asset, status
                   FROM instruments WHERE id=%s""",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]), "market": str(row[1]).lower(), "settle": str(row[2] or "").lower(),
            "symbol": str(row[3]), "display_name": str(row[3]), "base": str(row[4]),
            "quote": str(row[5]), "status": str(row[6]), "is_stock": False,
        }

    def get_canonical_candles(
        self,
        instrument_id: str,
        interval: str,
        start_ts: int | None,
        end_ts: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            identifier = UUID(instrument_id)
        except ValueError:
            return []
        latest = start_ts is None and end_ts is None
        selected_limit = max(1, min(limit, 5_001))
        if latest:
            if interval not in TIMEFRAME_SECONDS:
                return []
            maximum = self.latest_candle_time(instrument_id, interval)
            if maximum is None:
                return []
            # Bound by calendar periods, not a full-year sort looking for N
            # observations. Missing bars remain missing, never extend into older
            # history to make the indicator window appear fully populated.
            start_ts = max(0, maximum - TIMEFRAME_SECONDS[interval] * (selected_limit - 1))
            end_ts = maximum
        clauses = ["instrument_id=%s", "timeframe=%s", "is_final=true", "available_at<=now()"]
        params: list[Any] = [identifier, interval]
        if start_ts is not None:
            clauses.append("open_time >= to_timestamp(%s)")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("open_time <= to_timestamp(%s)")
            params.append(end_ts)
        if latest:
            # Exact catalog dimensions turn one instrument/timeframe into one
            # series. DISTINCT/ORDER now matches bars_current_lookup_idx without
            # putting venue/market ahead of time and sorting an entire year.
            clauses.extend([
                "venue=(SELECT venue FROM instruments WHERE id=%s)",
                "market=(SELECT market FROM instruments WHERE id=%s)",
            ])
            params.extend([identifier, identifier])
        distinct_keys = "open_time" if latest else "venue, market, instrument_id, timeframe, open_time"
        order_prefix = "" if latest else "venue, market, instrument_id, timeframe, "
        time_order = "DESC" if latest else "ASC"
        params.append(selected_limit)
        with self.pool.public_transaction() as connection:
            # Historical MCP indicator/chart reads must be bounded as well as
            # the latest-chart branch; a LIMIT alone does not bound query time.
            connection.execute("SET LOCAL statement_timeout='5s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            rows = connection.execute(
                f"""SELECT DISTINCT ON ({distinct_keys})
                           extract(epoch FROM open_time)::bigint, open, high, low, close,
                           volume, extract(epoch FROM available_at)::bigint,
                           venue, market, revision, quality_flags, is_final
                    FROM bars WHERE {' AND '.join(clauses)}
                    ORDER BY {order_prefix}open_time {time_order}, revision DESC
                    LIMIT %s""",
                params,
            ).fetchall()
        return [
            {
                "ts": int(row[0]), "open": row[1], "high": row[2], "low": row[3],
                "close": row[4], "volume": row[5], "updated_at": int(row[6]),
                "venue": str(row[7]), "market": str(row[8]), "revision": int(row[9]),
                "quality_flags": list(row[10]), "is_final": bool(row[11]),
            }
            for row in (reversed(rows) if latest else rows)
        ]

    def latest_candle_time(self, instrument_id: str, interval: str) -> int | None:
        """Prune old Timescale chunks for live charts; retain stale-cache fallback.

        MIN+MAX over years of chunks needlessly plans two scans on every poll.
        A bounded recent MAX needs only the newest chunks. If history is stale
        or the instrument is inactive, use the unrestricted MAX, not an empty
        chart or a guessed timestamp.
        """
        try:
            identifier = UUID(instrument_id)
            seconds = max(86400, TIMEFRAME_SECONDS[interval] * 3)
        except (ValueError, KeyError):
            return None
        with self.pool.public_transaction() as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            row = connection.execute(
                """SELECT extract(epoch FROM max(open_time))::bigint FROM bars
                   WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                     AND available_at<=now()
                     AND open_time >= %s""",
                (identifier, interval, datetime.now(timezone.utc) - timedelta(seconds=seconds)),
            ).fetchone()
            if row is None or row[0] is None:
                row = connection.execute(
                    """SELECT extract(epoch FROM max(open_time))::bigint FROM bars
                       WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                         AND available_at<=now()""",
                    (identifier, interval),
                ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def candle_bounds(self, instrument_id: str, interval: str) -> tuple[int | None, int | None]:
        try:
            identifier = UUID(instrument_id)
        except ValueError:
            return None, None
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT extract(epoch FROM min(open_time))::bigint,
                          extract(epoch FROM max(open_time))::bigint
                   FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                     AND available_at<=now()""",
                (identifier, interval),
            ).fetchone()
        if row is None:
            return None, None
        return (int(row[0]) if row[0] is not None else None, int(row[1]) if row[1] is not None else None)
