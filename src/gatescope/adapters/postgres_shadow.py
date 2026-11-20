"""Forward-only shadow ledger driven by immutable alerts and delivery facts.

No exchange adapter, proposal, account or order is reachable from this module.
Each signal and its first successful delivery are separate observation cohorts.
"""
from __future__ import annotations

import math
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import required_row
from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.quant.structure_trade import (
    advance_case,
    market_context,
    normalize_policy,
    plan_from_notice,
    start_case,
)


def ledger_state(case: dict[str, Any]) -> str:
    return {
        "WAITING_CONFIRMATION": "PENDING", "WAITING_ENTRY": "PENDING", "OPEN": "OPEN",
        "TARGET": "CLOSED", "STOP": "CLOSED", "TIMEOUT": "CLOSED",
        "MISSED": "EXPIRED", "CENSORED": "EXPIRED", "UNAVAILABLE": "SKIPPED",
    }.get(str(case.get("state")), "SKIPPED")


def instant(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def _bar(row: tuple) -> dict:
    return {
        "ts": int(row[0].timestamp()),
        **dict(zip(("open", "high", "low", "close", "volume"), map(float, row[1:6]), strict=True)),
        "revision": int(row[6]), "available_at": row[7].isoformat(),
        "payload_hash": row[8], "quality_flags": list(row[9]),
    }


class ShadowTradeRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def due_owners(self, limit: int = 20) -> list[tuple[UUID, UUID]]:
        with self.pool.public_transaction() as conn:
            return conn.execute("SELECT * FROM gatescope_security.shadow_due_owners(%s)", (limit,)).fetchall()

    def heartbeat(self, worker_id: str, *, running: bool, safe_error: str | None = None) -> None:
        error = safe_error if safe_error in ("SHADOW_PARTIAL_FAILURE", "SHADOW_CYCLE_FAILED") else None
        with self.pool.public_transaction() as conn:
            conn.execute("""INSERT INTO worker_heartbeats(workload,worker_id,running,heartbeat_at,status_json)
              VALUES('shadow-worker',%s,%s,now(),%s)
              ON CONFLICT(workload) DO UPDATE SET worker_id=EXCLUDED.worker_id,running=EXCLUDED.running,
                heartbeat_at=EXCLUDED.heartbeat_at,status_json=EXCLUDED.status_json""",
                         (worker_id[:80], running, Jsonb({"safe_error_code": error})))

    @staticmethod
    def _enabled(conn) -> datetime:
        return required_row(conn.execute("SELECT enabled_at FROM shadow_runtime WHERE singleton").fetchone())[0]

    @staticmethod
    def _event(conn, tenant, owner, identifier, sequence, event_type, observed_at, payload) -> None:
        conn.execute(
            """INSERT INTO shadow_trade_events(id,tenant_id,owner_user_id,trade_id,sequence,
                 event_type,observed_at,payload_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
            (uuid5(NAMESPACE_URL, f"shadow-event:{identifier}:{sequence}"), tenant, owner,
             identifier, sequence, event_type, observed_at, Jsonb(payload)),
        )

    @staticmethod
    def _context(conn, instrument, timeframe, cutoff) -> dict:
        rows = conn.execute(
            """SELECT DISTINCT ON(open_time) open_time,open,high,low,close,volume,
                 revision,available_at,payload_hash,quality_flags FROM bars
               WHERE instrument_id=%s AND timeframe=%s AND is_final AND close_time<=%s
                 AND available_at<=%s AND created_at<=%s
               ORDER BY open_time DESC,revision DESC LIMIT 240""",
            (instrument, timeframe, cutoff, cutoff, cutoff),
        ).fetchall()
        return market_context([{**_bar(r), "step_seconds": TIMEFRAME_SECONDS[timeframe]} for r in reversed(rows)])

    def capture(self, tenant: UUID, owner: UUID, *, now: datetime, limit: int = 20) -> int:
        with self.pool.transaction(tenant, owner) as conn:
            lock = conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))", (f"shadow-capture:{tenant}:{owner}",)).fetchone()
            if not lock or not lock[0]:
                return 0
            rows = conn.execute(
                """SELECT af.id,af.instrument_id,af.timeframe,af.available_at,af.created_at,af.payload_json
                   FROM alert_event_facts af JOIN alert_rules ar ON ar.id=af.alert_rule_id
                   WHERE ar.owner_user_id=%s AND af.created_at>=%s AND af.created_at<=%s
                     AND NOT EXISTS(SELECT 1 FROM shadow_alert_captures c WHERE c.alert_event_id=af.id)
                   ORDER BY af.created_at,af.id LIMIT %s""",
                (owner, self._enabled(conn), now, limit),
            ).fetchall()
            for alert, instrument, timeframe, available, created, payload in rows:
                signal_at = max(available, created)
                step = TIMEFRAME_SECONDS.get(timeframe, 3600)
                market = required_row(conn.execute("SELECT market FROM instruments WHERE id=%s", (instrument,)).fetchone())[0]
                policy = normalize_policy({"timeframe_seconds": step,
                                           **({"funding_bps_per_8h": 0} if market == "SPOT" else {})})
                expires_at = signal_at + timedelta(seconds=step * int(policy["max_entry_wait_bars"]))
                matches = payload.get("matched") if isinstance(payload, dict) else None
                matches = matches if isinstance(matches, list) and matches else [{}]
                context = self._context(conn, instrument, timeframe, signal_at)
                for position, item in enumerate(matches):
                    item = item if isinstance(item, dict) else {}
                    source = dict(item["source_event"]) if isinstance(item.get("source_event"), dict) else {}
                    source.setdefault("market_context", context)
                    notice = item["trade_notice"] if isinstance(item.get("trade_notice"), dict) else {}
                    plan = plan_from_notice(notice, policy=policy, event=source)
                    for cohort in ("SIGNAL", "DELIVERED"):
                        identifier = uuid5(NAMESPACE_URL, f"shadow:{alert}:{position}:{cohort}")
                        case = start_case(plan, available_at=math.ceil(signal_at.timestamp()), source="FORWARD_SHADOW")
                        state = ledger_state(case) if cohort == "SIGNAL" else "WAITING_DELIVERY"
                        conn.execute(
                            """INSERT INTO shadow_trade_facts(id,tenant_id,owner_user_id,alert_event_id,
                                 matched_index,instrument_id,timeframe,pattern_id,pattern_name,cohort,signal_at,
                                 expires_at,engine_version,source_event_json,plan_json,policy_json)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (identifier, tenant, owner, alert, position, instrument, timeframe,
                             str(item.get("pattern_id") or notice.get("pattern") or "UNKNOWN"),
                             str(notice.get("name") or source.get("name") or item.get("pattern_id") or "未标注形态"),
                             cohort, signal_at, expires_at, str(plan.get("engine_version") or plan.get("version") or "structure-trade-1"),
                             Jsonb(source), Jsonb(plan), Jsonb(policy)),
                        )
                        conn.execute(
                            """INSERT INTO shadow_trade_states(trade_id,tenant_id,owner_user_id,state,reason_code,
                                 eligible_at,state_json) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                            (identifier, tenant, owner, state, case.get("reason") if cohort == "SIGNAL" else "AWAITING_SUCCESSFUL_DELIVERY",
                             signal_at if cohort == "SIGNAL" else None, Jsonb(case)),
                        )
                        self._event(conn, tenant, owner, identifier, 1, "CAPTURED", now,
                                    {"cohort": cohort, "signal_at": signal_at.isoformat(), "state": state,
                                     "environment": "SHADOW", "forward_only": True})
                conn.execute("INSERT INTO shadow_alert_captures(alert_event_id,tenant_id,owner_user_id) VALUES(%s,%s,%s)",
                             (alert, tenant, owner))
            return len(rows)

    @staticmethod
    def _bars(conn, instrument, timeframe, *, after, end, limit=64) -> list[dict]:
        step = TIMEFRAME_SECONDS[timeframe]
        # A narrow time window avoids sorting a year of minute revisions on every poll.
        bound = min(end, after + timedelta(seconds=step * (limit + 1)))
        rows = conn.execute(
            """SELECT DISTINCT ON(open_time) open_time,open,high,low,close,volume,
                 revision,available_at,payload_hash,quality_flags FROM bars
               WHERE instrument_id=%s AND timeframe=%s AND is_final AND open_time>%s
                 AND open_time<=%s AND close_time<=%s AND available_at<=%s AND created_at<=%s
               ORDER BY open_time,revision ASC LIMIT %s""",
            (instrument, timeframe, after, bound, end, end, end, limit),
        ).fetchall()
        return [{**_bar(row), "step_seconds": step} for row in rows]

    def advance(self, tenant: UUID, owner: UUID, *, now: datetime, limit: int = 50) -> dict[str, int]:
        attempted = closed = 0
        with self.pool.transaction(tenant, owner) as conn:
            rows = conn.execute(
                """SELECT f.id,f.alert_event_id,f.instrument_id,f.timeframe,f.cohort,f.signal_at,
                     f.expires_at,f.plan_json,s.state,s.eligible_at,s.delivered_at,s.last_bar_time,
                     s.state_json,s.sequence
                   FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id
                   WHERE s.state IN ('WAITING_DELIVERY','PENDING','OPEN')
                   ORDER BY s.checked_at,f.id LIMIT %s FOR UPDATE OF s SKIP LOCKED""", (limit,),
            ).fetchall()
            for row in rows:
                (identifier, alert, instrument, timeframe, cohort, signal_at, expires_at, plan,
                 state, eligible, delivered, last_bar, case, sequence) = row
                attempted += 1
                reason = case.get("reason")
                if state == "WAITING_DELIVERY":
                    delivery_id = uuid5(NAMESPACE_URL, f"gatescope:outbox:notification:{alert}")
                    deliveries = conn.execute(
                        "SELECT status,updated_at FROM notification_deliveries WHERE event_id=%s AND updated_at<=%s ORDER BY updated_at,id",
                        (delivery_id, now),
                    ).fetchall()
                    sent = next((d for d in deliveries if d[0] == "SENT"), None)
                    if sent:
                        delivered = sent[1]
                        eligible = max(signal_at, delivered)
                        case = start_case(plan, available_at=math.ceil(eligible.timestamp()), source="FORWARD_SHADOW_DELIVERED")
                        if delivered > expires_at:
                            state, reason = "EXPIRED", "DELIVERY_AFTER_ENTRY_WINDOW"
                            case = {**case, "state": "MISSED", "reason": reason,
                                    "outcome_time": math.ceil(delivered.timestamp())}
                        else:
                            state, reason = ledger_state(case), case.get("reason")
                        sequence += 1
                        self._event(conn, tenant, owner, identifier, sequence, "DELIVERED", now,
                                    {"delivered_at": delivered.isoformat(), "eligible_at": eligible.isoformat(), "state": state})
                    elif deliveries and all(d[0] in ("FAILED", "DEAD_LETTER") for d in deliveries):
                        state, reason = "SKIPPED", "DELIVERY_FAILED"
                        case = {**case, "state": "UNAVAILABLE", "reason": reason,
                                "outcome_time": math.ceil(now.timestamp())}
                    elif now >= expires_at:
                        state, reason = "EXPIRED", "NOT_DELIVERED_BY_DEADLINE"
                        case = {**case, "state": "MISSED", "reason": reason,
                                "outcome_time": math.ceil(now.timestamp())}
                    else:
                        reason = "AWAITING_SUCCESSFUL_DELIVERY"
                if state in ("PENDING", "OPEN") and eligible is not None:
                    execution_tf = case.get("execution_timeframe")
                    if not execution_tf:
                        execution_tf = timeframe
                        # Only already-confirmed events can use finer execution bars;
                        # forming events must retain their original close-confirmation clock.
                        if case.get("state") == "WAITING_ENTRY":
                            minute_probe = self._bars(conn, instrument, "1m", after=eligible - timedelta(microseconds=1), end=now, limit=2)
                            if minute_probe:
                                execution_tf = "1m"
                    step = TIMEFRAME_SECONDS[execution_tf]
                    after = last_bar or eligible - timedelta(microseconds=1)
                    bars = self._bars(conn, instrument, execution_tf, after=after, end=now)
                    if bars:
                        case = {**case, "execution_timeframe": execution_tf,
                                "execution_basis": "NEXT_AVAILABLE_BAR_OPEN_SIMULATION"}
                    for bar in bars:
                        if last_bar is not None and int(last_bar.timestamp()) + step != bar["ts"]:
                            case = {**case, "state": "CENSORED", "reason": "EXECUTION_DATA_GAP"}
                            state, reason = "EXPIRED", "EXECUTION_DATA_GAP"
                            break
                        previous = case.get("state")
                        chain = hashlib.sha256(f"{case.get('observation_chain', '')}:{bar['payload_hash']}:{bar['revision']}".encode()).hexdigest()
                        observed_bars = int(case.get("observed_bars", 0)) + 1
                        case = {**advance_case(case, bar), "observation_chain": chain,
                                "observed_bars": observed_bars,
                                "execution_timeframe": execution_tf,
                                "execution_basis": "NEXT_AVAILABLE_BAR_OPEN_SIMULATION"}
                        last_bar = instant(bar["ts"])
                        state, reason = ledger_state(case), case.get("reason")
                        if previous != case.get("state") or observed_bars % 64 == 0:
                            sequence += 1
                            self._event(conn, tenant, owner, identifier, sequence,
                                        "STATE_CHANGED" if previous != case.get("state") else "BAR_CHECKPOINT", now,
                                        {"state": case.get("state"), "reason": reason, "bar": bar,
                                         "observation_chain": chain, "observed_bars": observed_bars,
                                         "revision_policy": "EARLIEST_FINAL_REVISION",
                                         "entry_time": case.get("entry_time"), "exit_time": case.get("exit_time")})
                        if state not in ("PENDING", "OPEN"):
                            break
                    if not bars and now > (last_bar or eligible) + timedelta(seconds=step * 3):
                        # Missing bars are not a win/loss. Preserve the case as censored.
                        case = {**case, "state": "CENSORED", "reason": "EXECUTION_DATA_UNAVAILABLE"}
                        state, reason = "EXPIRED", "EXECUTION_DATA_UNAVAILABLE"
                if state in ("EXPIRED", "SKIPPED") and row[8] != state:
                    sequence += 1
                    self._event(conn, tenant, owner, identifier, sequence, "OBSERVATION_ENDED", now,
                                {"state": state, "reason_code": reason})
                conn.execute(
                    """UPDATE shadow_trade_states SET state=%s,reason_code=%s,eligible_at=%s,delivered_at=%s,
                         last_bar_time=%s,state_json=%s,result_json=%s,sequence=%s,checked_at=%s,updated_at=%s
                       WHERE trade_id=%s""",
                    (state, reason, eligible, delivered, last_bar, Jsonb(case), Jsonb(case if state == "CLOSED" else {}),
                     sequence, now, now, identifier),
                )
                closed += int(state == "CLOSED")
        return {"attempted": attempted, "closed": closed}

    @staticmethod
    def _filters(cohort=None, state=None, instrument_id=None) -> tuple[str, list]:
        where, args = [], []
        for column, value in (("f.cohort", cohort), ("s.state", state), ("f.instrument_id", instrument_id)):
            if value:
                where.append(f"{column}=%s")
                args.append(value)
        return (" WHERE " + " AND ".join(where) if where else ""), args

    @staticmethod
    def _select() -> str:
        return """SELECT f.id,f.alert_event_id,f.instrument_id,i.symbol,f.timeframe,f.pattern_id,f.pattern_name,
          f.cohort,s.state,s.reason_code,f.signal_at,s.delivered_at,s.eligible_at,f.created_at,s.updated_at,
          f.engine_version,f.plan_json,f.policy_json,f.source_event_json,s.state_json,s.result_json
          FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id
          JOIN instruments i ON i.id=f.instrument_id"""

    @staticmethod
    def _item(row, *, detail=False) -> dict:
        keys = ("id", "alert_event_id", "instrument_id", "symbol", "timeframe", "pattern_id", "pattern_name",
                "cohort", "state", "reason_code", "signal_at", "delivered_at", "eligible_at", "created_at", "updated_at",
                "engine_version", "plan", "policy", "source_event", "case", "result")
        item = dict(zip(keys, row, strict=True))
        plan, case = item["plan"], item.pop("case")
        notice = plan.get("notice", {})
        item.update(
            environment="SHADOW", forward_only=True,
            side={1: "LONG", -1: "SHORT"}.get(plan.get("side")),
            entry_at=instant(case.get("entry_time")), exit_at=instant(case.get("exit_time")),
            entry_price=case.get("entry_price"), exit_price=case.get("exit_price"),
            stop_price=plan.get("stop_price") or (notice.get("stop") or {}).get("price"),
            target_price=plan.get("target_price") or (notice.get("target") or {}).get("price"),
            gross_return=case.get("gross_return"), raw_gross_return=case.get("raw_gross_return"),
            slippage_return=case.get("slippage_return"), net_return=case.get("net_return"),
            costs_return=case.get("costs_return"),
            execution_timeframe=case.get("execution_timeframe"),
        )
        if detail:
            item["result"] = case
        else:
            for name in ("plan", "policy", "source_event", "result"):
                item.pop(name)
        return item

    def list(self, tenant, owner, *, limit=20, offset=0, cohort=None, state=None, instrument_id=None) -> dict:
        where, args = self._filters(cohort, state, instrument_id)
        with self.pool.transaction(tenant, owner) as conn:
            rows = conn.execute(self._select() + where + " ORDER BY f.created_at DESC,f.id DESC LIMIT %s OFFSET %s",
                                (*args, limit + 1, offset)).fetchall()
            count_row = conn.execute("SELECT count(*) FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id" + where, args).fetchone()
            assert count_row is not None
            total = count_row[0]
            enabled = self._enabled(conn)
        return {"items": [self._item(r) for r in rows[:limit]], "total": total, "has_more": len(rows) > limit,
                "next_offset": offset + limit if len(rows) > limit else None, "enabled_at": enabled}

    def get(self, tenant, owner, identifier, *, event_limit=100, event_offset=0) -> dict:
        with self.pool.transaction(tenant, owner) as conn:
            row = conn.execute(self._select() + " WHERE f.id=%s", (identifier,)).fetchone()
            if row is None:
                raise LookupError("SHADOW_TRADE_NOT_FOUND")
            events = conn.execute("SELECT sequence,event_type,observed_at,payload_json FROM shadow_trade_events WHERE trade_id=%s ORDER BY sequence LIMIT %s OFFSET %s", (identifier, event_limit + 1, event_offset)).fetchall()
            count_row = conn.execute("SELECT count(*) FROM shadow_trade_events WHERE trade_id=%s", (identifier,)).fetchone()
            assert count_row is not None
            total = count_row[0]
        return {**self._item(row, detail=True), "events": [dict(zip(("sequence", "event_type", "observed_at", "payload"), e, strict=True)) for e in events[:event_limit]],
                "events_total": total, "events_has_more": len(events) > event_limit,
                "events_next_offset": event_offset + event_limit if len(events) > event_limit else None}

    def summary(self, tenant, owner, *, cohort=None, instrument_id=None) -> dict:
        where, args = self._filters(cohort=cohort, instrument_id=instrument_id)
        with self.pool.transaction(tenant, owner) as conn:
            enabled = self._enabled(conn)
            heartbeat = conn.execute("SELECT running,heartbeat_at,status_json->>'safe_error_code' FROM worker_heartbeats WHERE workload='shadow-worker'").fetchone()
            states = conn.execute("SELECT s.state,count(*) FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id" + where + " GROUP BY s.state", args).fetchall()
            reasons = conn.execute("SELECT s.reason_code,count(*) FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id" + where + (" AND " if where else " WHERE ") + "s.reason_code IS NOT NULL GROUP BY s.reason_code ORDER BY count(*) DESC,s.reason_code", args).fetchall()
            cohorts = conn.execute("""SELECT f.cohort,count(*),count(*) FILTER(WHERE s.state='CLOSED'),
                count(*) FILTER(WHERE s.state='CLOSED' AND (s.result_json->>'net_return')::double precision>0),
                avg((s.result_json->>'net_return')::double precision) FILTER(WHERE s.state='CLOSED'),
                avg((s.result_json->>'gross_return')::double precision) FILTER(WHERE s.state='CLOSED'),
                avg((s.result_json->>'costs_return')::double precision) FILTER(WHERE s.state='CLOSED')
                FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id""" + where + " GROUP BY f.cohort ORDER BY f.cohort", args).fetchall()
            contexts = conn.execute("""SELECT c.dimension,c.bucket,f.cohort,count(*),
                count(*) FILTER(WHERE s.state='CLOSED'),
                count(*) FILTER(WHERE s.state='CLOSED' AND (s.result_json->>'net_return')::double precision>0),
                avg((s.result_json->>'net_return')::double precision) FILTER(WHERE s.state='CLOSED')
                FROM shadow_trade_facts f JOIN shadow_trade_states s ON s.trade_id=f.id
                CROSS JOIN LATERAL(VALUES
                  ('trend',coalesce(nullif(f.plan_json->'context'->>'trend',''),'UNKNOWN')),
                  ('volatility',coalesce(nullif(f.plan_json->'context'->>'volatility',''),'UNKNOWN')),
                  ('volume',coalesce(nullif(f.plan_json->'context'->>'volume',''),'UNKNOWN')),
                  ('higher_alignment',coalesce(nullif(f.plan_json->'context'->>'higher_alignment',''),'UNKNOWN')),
                  ('score_bucket',coalesce(nullif(f.plan_json->'context'->>'score_bucket',''),'UNSCORED'))
                ) c(dimension,bucket)""" + where +
                " GROUP BY c.dimension,c.bucket,f.cohort ORDER BY c.dimension,c.bucket,f.cohort", args).fetchall()
        counts = {name: 0 for name in ("total", "waiting_delivery", "pending", "open", "closed", "skipped", "expired")}
        counts.update({str(s).lower(): int(n) for s, n in states})
        counts["total"] = sum(n for _, n in states)
        result = []
        for name, total, closed, wins, net, gross, costs in cohorts:
            result.append({"cohort": name, "total": total, "closed": closed, "wins": wins,
                           "win_rate": wins / closed if closed else None, "mean_net_return": net,
                           "mean_gross_return": gross, "mean_costs_return": costs})
        health = {"state": "NOT_STARTED", "last_cycle_at": None, "safe_error_code": None}
        if heartbeat is not None:
            running, last_cycle, safe_error = heartbeat
            state = "STOPPED" if not running else "STALE" if datetime.now(timezone.utc) - last_cycle > timedelta(seconds=120) else "DEGRADED" if safe_error else "HEALTHY"
            health = {"state": state, "last_cycle_at": last_cycle,
                      "safe_error_code": safe_error if safe_error in ("SHADOW_PARTIAL_FAILURE", "SHADOW_CYCLE_FAILED") else None}
        return {"enabled_at": enabled, "environment": "SHADOW", "forward_only": True,
                "sample_unit": "MATCHED_ALERT_COHORT", "statistical_independence_assumed": False,
                "cross_cohort_total_is_trade_count": False,
                "counts": counts, "cohorts": result, "worker": health,
                "by_context": [{"dimension": d, "bucket": b, "cohort": c, "total": total,
                                "closed": n, "wins": wins, "win_rate": wins / n if n else None,
                                "mean_net_return": net, "descriptive_only": True}
                               for d, b, c, total, n, wins, net in contexts],
                "reason_counts": [{"reason_code": r, "count": n} for r, n in reasons]}
