"""Least-privilege PostgreSQL writer for Gate public catalog and final bars."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence
from uuid import UUID

from gatescope.adapters.legacy_sqlite import legacy_instrument_uuid
from gatescope.adapters.gate_catalog_rules import catalog_revision_payload, catalog_rules_equal
from gatescope.adapters.market_history_bounds import history_bounds
from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.domain.history import eligible_gap

CACHE_TARGET_BARS = 8000


@dataclass(frozen=True)
class MarketSubscription:
    instrument_id: UUID
    venue: str
    market: str
    symbol: str
    settle_asset: str
    timeframe: str

    def gate_instrument(self) -> dict[str, Any]:
        source_market = "futures" if self.market.startswith("FUTURES_") else "spot"
        return {
            "id": str(self.instrument_id),
            "market": source_market,
            "settle": self.settle_asset.lower(),
            "symbol": self.symbol,
        }


class PostgresMarketIngestRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def refresh_catalog(self, rows: Sequence[dict[str, Any]], *, observed_at: datetime | None = None) -> int:
        observed = observed_at or datetime.now(timezone.utc)
        changed = 0
        with self.pool.public_transaction() as connection:
            for row in rows:
                identifier = legacy_instrument_uuid(str(row["id"]))
                market = _formal_market(str(row["market"]), str(row.get("settle") or ""))
                status = _formal_status(str(row.get("status") or ""))
                result = connection.execute(
                    """INSERT INTO instruments
                         (id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,
                          created_at,updated_at)
                       VALUES (%s,'GATE',%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (id) DO UPDATE SET
                         market=EXCLUDED.market,symbol=EXCLUDED.symbol,
                         base_asset=EXCLUDED.base_asset,quote_asset=EXCLUDED.quote_asset,
                         settle_asset=EXCLUDED.settle_asset,status=EXCLUDED.status,
                         updated_at=EXCLUDED.updated_at
                       WHERE (instruments.market,instruments.symbol,instruments.base_asset,
                              instruments.quote_asset,instruments.settle_asset,instruments.status)
                         IS DISTINCT FROM (EXCLUDED.market,EXCLUDED.symbol,EXCLUDED.base_asset,
                              EXCLUDED.quote_asset,EXCLUDED.settle_asset,EXCLUDED.status)""",
                    (
                        identifier, market, str(row["symbol"]), str(row.get("base") or "UNKNOWN"),
                        str(row.get("quote") or "UNKNOWN"), str(row.get("settle") or "").upper() or None,
                        status, observed, observed,
                    ),
                )
                changed += max(0, result.rowcount)
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                identity = {
                    "venue": "GATE", "market": market, "symbol": str(row["symbol"]),
                    "base_asset": str(row.get("base") or "UNKNOWN"),
                    "quote_asset": str(row.get("quote") or "UNKNOWN"),
                    "settle_asset": str(row.get("settle") or "").upper() or None,
                    "status": status,
                }
                # The conflict check above locks the instrument even if unchanged,
                # serializing its revision allocation without rewriting the row.
                # updated_at now means a real identity/status change, not an HTTP
                # freshness claim. Worker heartbeat remains the runtime liveness.
                latest = connection.execute(
                    """SELECT revision,metadata_json FROM instrument_revisions
                       WHERE instrument_id=%s ORDER BY revision DESC LIMIT 1""", (identifier,),
                ).fetchone()
                if (latest is not None and result.rowcount == 0
                        and catalog_rules_equal(latest[1], metadata, market, identity)):
                    continue
                revision = int(latest[0]) + 1 if latest is not None else 1
                encoded, payload_hash = catalog_revision_payload(metadata, market, identity, revision)
                connection.execute(
                    """INSERT INTO instrument_revisions
                         (id,instrument_id,revision,effective_at,available_at,tick_size,lot_size,
                          min_notional,contract_multiplier,metadata_json,payload_hash,created_at)
                       VALUES(gen_random_uuid(),%s,%s,%s,%s,NULL,NULL,NULL,
                              NULL,%s::jsonb,%s,%s)""",
                    (identifier, revision, observed, observed, encoded, payload_hash, observed),
                )
        return changed

    def subscriptions(self) -> list[MarketSubscription]:
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM gatescope_security.market_watchlist_subscriptions()"
            ).fetchall()
        return [
            MarketSubscription(
                instrument_id=UUID(str(row[0])), venue=str(row[1]), market=str(row[2]),
                symbol=str(row[3]), settle_asset=str(row[4] or ""), timeframe=str(row[5]),
            )
            for row in rows
        ]

    def latest_open_time(self, subscription: MarketSubscription) -> int | None:
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT extract(epoch FROM max(open_time))::bigint FROM bars
                   WHERE venue=%s AND market=%s AND instrument_id=%s
                     AND timeframe=%s AND is_final=true AND available_at<=now()""",
                (subscription.venue, subscription.market, subscription.instrument_id, subscription.timeframe),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def sync_states(self, subscriptions: Sequence[MarketSubscription]) -> dict[tuple[UUID, str], dict[str, Any]]:
        """Only the deduplicated public pairs are persisted, never user identities."""
        with self.pool.public_transaction() as c:
            c.execute("""UPDATE market_sync_state SET active=false WHERE active=true
                AND (instrument_id,timeframe) NOT IN (
                    SELECT x.instrument_id,x.timeframe FROM jsonb_to_recordset(%s::jsonb)
                      AS x(instrument_id uuid,timeframe text))""",
                (json.dumps([{"instrument_id": str(s.instrument_id), "timeframe": s.timeframe} for s in subscriptions]),))
            with c.cursor() as cursor:
                cursor.executemany("""INSERT INTO market_sync_state(instrument_id,timeframe,target_bars,missing_bars)
                    VALUES (%s,%s,%s,%s) ON CONFLICT(instrument_id,timeframe) DO UPDATE SET active=true
                    WHERE market_sync_state.active=false""",
                    [(s.instrument_id,s.timeframe,365*86400//TIMEFRAME_SECONDS[s.timeframe],365*86400//TIMEFRAME_SECONDS[s.timeframe]) for s in subscriptions])
            rows = c.execute("""SELECT instrument_id,timeframe,next_live_at,next_history_at,live_failures,live_error_code
                FROM market_sync_state WHERE active=true""").fetchall()
        return {(r[0], r[1]): dict(zip(("next_live_at", "next_history_at", "live_failures", "live_error_code"), r[2:], strict=True)) for r in rows}

    def mark_live(self, s: MarketSubscription, now: datetime, *, error: str | None = None, delay: float = 5) -> None:
        with self.pool.public_transaction() as c:
            c.execute("""UPDATE market_sync_state SET next_live_at=%s,
                last_live_at=CASE WHEN %s::text IS NULL THEN %s ELSE last_live_at END,
                live_failures=CASE WHEN %s::text IS NULL THEN 0 ELSE live_failures+1 END,
                live_error_code=%s,updated_at=%s WHERE instrument_id=%s AND timeframe=%s""",
                (now + timedelta(seconds=delay), error, now, error, error, now, s.instrument_id, s.timeframe))

    def pause_provider(self, now: datetime, delay: float) -> None:
        with self.pool.public_transaction() as c:
            c.execute("""UPDATE market_sync_state SET next_live_at=greatest(next_live_at,%s),
                next_history_at=greatest(next_history_at,%s),live_error_code='MARKET_RATE_LIMITED'
                WHERE active=true""", (now + timedelta(seconds=delay), now + timedelta(seconds=delay)))

    def provider_retry_at(self) -> datetime | None:
        # Retain provider-wide backoff even if the user changes/removes subscriptions.
        with self.pool.public_transaction() as c:
            return c.execute("""SELECT max(next_live_at) FROM market_sync_state
                WHERE live_error_code='MARKET_RATE_LIMITED' AND next_live_at>now()""").fetchone()[0]

    def history_plan(self, s: MarketSubscription, now: datetime) -> dict[str, Any]:
        """Repair the newest gap first, including internal outages, at most 900 bars."""
        step = TIMEFRAME_SECONDS[s.timeframe]
        with self.pool.public_transaction() as c:
            bounds = history_bounds(c, s.instrument_id, s.timeframe, now)
            start, end, target = bounds["start"], bounds["end"], bounds["target_bars"]
            # A database window finds holes; never materialise 525,600 timestamps in Python.
            stats = c.execute("""SELECT count(DISTINCT open_time),min(open_time),max(open_time) FROM bars
                WHERE instrument_id=%s AND timeframe=%s AND is_final=true AND available_at<=%s
                  AND venue=%s AND market=%s AND created_at<=%s AND close_time<=%s
                  AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
                  AND mod(extract(epoch FROM open_time),%s)=0""",
                (s.instrument_id,s.timeframe,now,s.venue,s.market,now,now,start,end,step)).fetchone()
            blocked = c.execute("""SELECT from_time,to_time FROM market_history_deferred
                WHERE instrument_id=%s AND timeframe=%s AND retry_at>%s""",(s.instrument_id,s.timeframe,now)).fetchall()
            with c.cursor(name="year_gaps") as gap_cursor:
                gap_cursor.execute("""WITH present AS (
                SELECT DISTINCT extract(epoch FROM open_time)::bigint AS t FROM bars
                WHERE instrument_id=%s AND timeframe=%s AND is_final=true AND available_at<=%s
                  AND venue=%s AND market=%s AND created_at<=%s AND close_time<=%s
                  AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
                  AND mod(extract(epoch FROM open_time),%s)=0
                UNION SELECT %s::bigint UNION SELECT %s::bigint), ordered AS (
                SELECT t,lag(t) OVER(ORDER BY t) AS previous FROM present)
                SELECT previous+%s,t-%s FROM ordered WHERE t-previous>%s ORDER BY t DESC""",
                    (s.instrument_id,s.timeframe,now,s.venue,s.market,now,now,start,end,step,
                     start-step,end+step,step,step,step))
                gap=eligible_gap(gap_cursor,blocked,step)
        return {"target_bars":target,"target_from":datetime.fromtimestamp(start,timezone.utc),
            "target_to":datetime.fromtimestamp(end+step,timezone.utc),
            "cached_bars":stats[0],"missing_bars":target-stats[0],"gap":gap,
            "listing_time":bounds["listing_time"],"range_policy":bounds["range_policy"],
            "requested_start":bounds["requested_start"],
            "first_open_time":stats[1],"last_close_time":stats[2]+timedelta(seconds=step) if stats[2] else None,
            "internal_gap":bool(gap and stats[1] and gap[0]>=stats[1].timestamp())}

    def defer_history(self,s,start,end,now,*,reason,delay=21600):
        with self.pool.public_transaction() as c:
            c.execute("DELETE FROM market_history_deferred WHERE instrument_id=%s AND timeframe=%s AND retry_at<=%s",
                (s.instrument_id,s.timeframe,now))
            c.execute("""INSERT INTO market_history_deferred VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(instrument_id,timeframe,from_time,to_time) DO UPDATE SET retry_at=EXCLUDED.retry_at,reason=EXCLUDED.reason""",
                (s.instrument_id,s.timeframe,start,end,now+timedelta(seconds=delay),reason))

    def defer_history_ranges(
        self, s: MarketSubscription, ranges: Sequence[tuple[int, int]], now: datetime,
        *, reason: str, delay: float = 21600,
    ) -> None:
        """Persist proven holes from one <=900-slot archive page in one transaction.

        Alternating present/missing candles produce at most 450 missing ranges.
        Keep this separate from the single absent-month defer, which can span a
        full month because a 404 establishes that the entire file is absent.
        """
        if not ranges:
            return
        if len(ranges) > 450:
            raise ValueError("HISTORY_DEFER_RANGE_BUDGET")
        step = TIMEFRAME_SECONDS[s.timeframe]
        previous_end = None
        for start, end in ranges:
            if (type(start) is not int or type(end) is not int or start > end
                    or start % step or end % step
                    or previous_end is not None and start <= previous_end + step):
                raise ValueError("HISTORY_DEFER_RANGE_INVALID")
            previous_end = end
        if (ranges[-1][1] - ranges[0][0]) // step + 1 > 900:
            raise ValueError("HISTORY_DEFER_RANGE_BUDGET")
        retry_at = now + timedelta(seconds=delay)
        with self.pool.public_transaction() as connection:
            connection.execute("""DELETE FROM market_history_deferred
                WHERE instrument_id=%s AND timeframe=%s AND retry_at<=%s""",
                (s.instrument_id, s.timeframe, now))
            with connection.cursor() as cursor:
                cursor.executemany("""INSERT INTO market_history_deferred
                    (instrument_id,timeframe,from_time,to_time,retry_at,reason)
                    VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(instrument_id,timeframe,from_time,to_time)
                    DO UPDATE SET retry_at=EXCLUDED.retry_at,reason=EXCLUDED.reason""",
                    [(s.instrument_id, s.timeframe, start, end, retry_at, reason) for start, end in ranges])

    def mark_history(self, s: MarketSubscription, plan: dict, now: datetime, *, state: str, delay: float, error: str | None = None) -> None:
        with self.pool.public_transaction() as c:
            c.execute("""UPDATE market_sync_state SET target_bars=%s,cached_bars=%s,missing_bars=%s,
                first_open_time=%s,last_close_time=%s,last_history_at=%s,next_history_at=%s,
                history_status=%s,history_error_code=%s,updated_at=%s,target_from=%s,target_to=%s
                WHERE instrument_id=%s AND timeframe=%s""",
                (plan["target_bars"], plan["cached_bars"], plan["missing_bars"], plan["first_open_time"],
                 plan["last_close_time"], now, now + timedelta(seconds=delay), state, error, now,
                 plan["target_from"],plan["target_to"],s.instrument_id, s.timeframe))

    def upsert_live_bar(self, s: MarketSubscription, rows: Sequence[tuple], *, observed_at: datetime) -> None:
        step = TIMEFRAME_SECONDS[s.timeframe]
        candidates = [r for r in rows if r[0] <= observed_at.timestamp() < r[0] + step]
        if not candidates:
            return
        timestamp, open_, high, low, close, volume = max(candidates, key=lambda r: r[0])
        opened = datetime.fromtimestamp(int(timestamp), timezone.utc)
        bar = Bar(venue=s.venue, market=s.market, instrument_id=str(s.instrument_id), timeframe=s.timeframe,
            open_time=opened, close_time=opened + timedelta(seconds=step), open=Decimal(str(open_)),
            high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)), volume=Decimal(str(volume)),
            is_final=False, revision=1, available_at=observed_at)
        with self.pool.public_transaction() as c:
            c.execute("""INSERT INTO market_live_candles
                (instrument_id,timeframe,open_time,close_time,open,high,low,close,volume,observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(instrument_id,timeframe) DO UPDATE SET
                  open_time=EXCLUDED.open_time,close_time=EXCLUDED.close_time,open=EXCLUDED.open,
                  high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,volume=EXCLUDED.volume,observed_at=EXCLUDED.observed_at
                WHERE EXCLUDED.open_time>=market_live_candles.open_time
                  AND EXCLUDED.observed_at>market_live_candles.observed_at""",
                (s.instrument_id, s.timeframe, bar.open_time, bar.close_time, bar.open, bar.high, bar.low,
                 bar.close, bar.volume, observed_at))

    def upsert_final_bars(
        self,
        subscription: MarketSubscription,
        rows: Sequence[tuple[int, float, float, float, float, float]],
        *,
        observed_at: datetime | None = None,
        archive_source: dict | None = None,
    ) -> int:
        observed = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        step = TIMEFRAME_SECONDS[subscription.timeframe]
        inserted = 0
        final_rows = {int(r[0]): r for r in rows if int(r[0]) + step <= observed.timestamp()}
        if not final_rows:
            return 0
        with self.pool.public_transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"market:{subscription.instrument_id}:{subscription.timeframe}",))
            existing = connection.execute("""SELECT DISTINCT ON (open_time) open_time,revision,
                open,high,low,close,volume,available_at FROM bars
                WHERE instrument_id=%s AND timeframe=%s AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
                ORDER BY open_time,revision DESC""",
                (subscription.instrument_id, subscription.timeframe, min(final_rows), max(final_rows))).fetchall()
            latest_by_time = {int(r[0].timestamp()): r for r in existing}
            inserts = []
            for timestamp, open_, high, low, close, volume in final_rows.values():
                opened = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                closed = opened + timedelta(seconds=step)
                latest = latest_by_time.get(int(timestamp))
                prices = tuple(Decimal(str(v)) for v in (open_, high, low, close, volume))
                # Availability belongs to the first observation of each version, not each poll.
                if latest is not None and (archive_source or tuple(latest[2:7]) == prices or latest[7] >= observed):
                    continue
                revision = int(latest[1]) + 1 if latest is not None else 1
                bar = Bar(
                    venue=subscription.venue,
                    market=subscription.market,
                    instrument_id=str(subscription.instrument_id),
                    timeframe=subscription.timeframe,
                    open_time=opened,
                    close_time=closed,
                    open=Decimal(str(open_)), high=Decimal(str(high)), low=Decimal(str(low)),
                    close=Decimal(str(close)), volume=Decimal(str(volume)), is_final=True,
                    revision=revision, available_at=observed,
                    quality_flags=frozenset(({"GATE_MONTHLY_ARCHIVE"} | ({"AGGREGATED_COMPLETE_SOURCE"} if archive_source.get("aggregated") else set()) if archive_source else {"REST_POLLING", "EXCHANGE_FINALITY_ASSUMED"})
                        | ({"HISTORICAL_BACKFILL"} if observed - closed >= timedelta(seconds=step) else set())),
                )
                inserts.append((bar.venue, bar.market, subscription.instrument_id, bar.timeframe,
                    bar.open_time, bar.close_time, bar.open, bar.high, bar.low, bar.close,
                    bar.volume, revision, bar.available_at, bar.payload_hash, sorted(bar.quality_flags), observed))
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO bars
                         (venue,market,instrument_id,timeframe,open_time,close_time,open,high,low,
                          close,volume,is_final,source_sequence,revision,available_at,payload_hash,
                          quality_flags,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,NULL,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    inserts,
                )
                inserted += max(0, cursor.rowcount)
            if inserted:
                connection.execute("""INSERT INTO market_chart_snapshots(instrument_id,timeframe,generation)
                    VALUES(%s,%s,1) ON CONFLICT(instrument_id,timeframe) DO UPDATE
                    SET generation=market_chart_snapshots.generation+1
                    WHERE market_chart_snapshots.bar_count<1500 OR market_chart_snapshots.first_open_time<=to_timestamp(%s)""",
                    (subscription.instrument_id,subscription.timeframe,max(final_rows)))
        return inserted

    def heartbeat(self, worker_id: str, *, running: bool, status: dict[str, Any]) -> None:
        from gatescope.application.storage_pressure import safe_storage_status

        now = datetime.now(timezone.utc)
        try:
            storage = safe_storage_status(status.get("history_storage"))
        except TypeError:
            # Malformed/unhashable status values are not evidence of capacity.
            storage = {}
        history_pages = status.get("history_pages")
        safe_status = {
            "catalog_count": max(0, int(status.get("catalog_count") or 0)),
            "subscription_count": max(0, int(status.get("subscription_count") or 0)),
            "updated_subscriptions": max(0, int(status.get("updated_subscriptions") or 0)),
            "inserted_bars": max(0, int(status.get("inserted_bars") or 0)),
            "provider_tests_processed": max(
                0, int(status.get("provider_tests_processed") or 0),
            ),
            "history_pages": max(0, history_pages) if type(history_pages) is int else 0,
            "rate_limited": bool(status.get("rate_limited")),
            "degraded": bool(status.get("degraded")),
            "maintenance_running": bool(status.get("maintenance_running")),
            "maintenance_error": status.get("maintenance_error"),
            "snapshot_error": status.get("snapshot_error"),
            "safe_error_code": str(status.get("safe_error_code") or "")[:80] or None,
        }
        if storage:
            # Derive the state from the validated report, never from an unrelated
            # top-level label; no paths, DSNs or probe exception text are copied.
            safe_status.update(history_state=storage["state"], history_storage=storage)
        with self.pool.public_transaction() as connection:
            connection.execute(
                """INSERT INTO worker_heartbeats
                     (workload,worker_id,running,heartbeat_at,status_json)
                   VALUES ('market-worker',%s,%s,%s,%s::jsonb)
                   ON CONFLICT (workload) DO UPDATE SET
                     worker_id=EXCLUDED.worker_id,running=EXCLUDED.running,
                     heartbeat_at=EXCLUDED.heartbeat_at,status_json=EXCLUDED.status_json""",
                (worker_id[:80], running, now, json.dumps(safe_status, separators=(",", ":"))),
            )


def _formal_market(market: str, settle: str) -> str:
    normalized = market.strip().lower()
    if normalized == "spot":
        return "SPOT"
    if normalized == "futures" and settle.strip().lower() in {"usdt", "btc"}:
        return f"FUTURES_{settle.strip().upper()}"
    raise ValueError("unsupported Gate market")


def _formal_status(status: str) -> str:
    normalized = status.strip().lower()
    return "DISABLED" if normalized in {"delisting", "disabled", "untradable"} else "ACTIVE"
