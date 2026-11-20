"""Least-privilege persistence used by the formal pattern strategy worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.application.alerts import AlertRuleSnapshot, PatternObservation
from gatescope.domain.market import Bar, TIMEFRAME_SECONDS
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.application.signal_freshness import LIVE_FRESHNESS_SECONDS, latest_closed_time, signal_lifetime
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.pattern_study import digest
from gatescope.quant.structure_screening import usable


@dataclass(frozen=True)
class ActiveStrategyRule:
    tenant_id: UUID
    snapshot: AlertRuleSnapshot


@dataclass(frozen=True)
class StructureSignalSnapshot:
    key: str
    bars: Sequence[Bar]
    events: Sequence[Mapping[str, Any]]


class PostgresStrategyRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def due_tenants(self, *, limit: int = 100) -> list[UUID]:
        with self.pool.public_transaction() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM gatescope_security.strategy_active_rule_tenants(%s)",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [row[0] for row in rows]

    def active_rules(self, tenant_id: UUID, subject_id: UUID) -> list[ActiveStrategyRule]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            rows = connection.execute(
                """SELECT r.id,v.id,v.version,v.condition_json
                     FROM alert_rules r
                     JOIN alert_rule_versions v
                       ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id
                      AND v.version=r.current_version
                    WHERE r.status='ACTIVE'
                    ORDER BY r.id"""
            ).fetchall()
        rules: list[ActiveStrategyRule] = []
        for row in rows:
            condition = dict(row[3])
            rules.append(
                ActiveStrategyRule(
                    tenant_id=tenant_id,
                    snapshot=AlertRuleSnapshot(
                        rule_id=row[0],
                        version_id=row[1],
                        version=int(row[2]),
                        instrument_id=UUID(str(condition["instrument_id"])),
                        timeframe=str(condition["timeframe"]),
                        pattern_ids=tuple(str(value) for value in condition["pattern_ids"]),
                        match_mode=str(condition.get("match_mode", "ANY")),  # type: ignore[arg-type]
                        minimum_confidence=float(condition.get("minimum_confidence", 0.65)),
                        confirmed_only=bool(condition.get("confirmed_only", True)),
                        coincidence_bars=int(condition.get("coincidence_bars", 3)),
                        cooldown_seconds=int(condition.get("cooldown_seconds", 900)),
                    ),
                )
            )
        return rules

    def canonical_bars(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        instrument_id: UUID,
        timeframe: str,
        *,
        limit: int = 600,
    ) -> list[Bar]:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            rows = connection.execute(
                """WITH latest AS (
                     SELECT DISTINCT ON(open_time)
                            venue,market,instrument_id,timeframe,open_time,close_time,
                            open,high,low,close,volume,is_final,source_sequence,revision,
                            available_at,quality_flags
                       FROM bars
                      WHERE instrument_id=%s AND timeframe=%s AND is_final=true
                        AND available_at<=now() AND close_time<=now() AND created_at<=now()
                      ORDER BY open_time DESC,revision DESC LIMIT %s
                   )
                   SELECT venue,market,instrument_id,timeframe,open_time,close_time,
                          open,high,low,close,volume,is_final,source_sequence,revision,
                          available_at,quality_flags
                     FROM latest ORDER BY open_time""",
                (instrument_id, timeframe, max(20, min(limit, 1_800))),
            ).fetchall()
        return [
            Bar(
                venue=str(row[0]),
                market=str(row[1]),
                instrument_id=str(row[2]),
                timeframe=str(row[3]),
                open_time=row[4],
                close_time=row[5],
                open=Decimal(row[6]),
                high=Decimal(row[7]),
                low=Decimal(row[8]),
                close=Decimal(row[9]),
                volume=Decimal(row[10]),
                is_final=bool(row[11]),
                source_sequence=str(row[12]) if row[12] else None,
                revision=int(row[13]),
                available_at=row[14],
                quality_flags=frozenset(row[15]),
            )
            for row in rows
        ]

    def structure_snapshot(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        instrument_id: UUID,
        timeframe: str,
        *,
        now: datetime,
    ) -> StructureSignalSnapshot | None:
        """Consume the chart's durable live decisions; never run a second detector.

        Only a small recent candle tail is read to veto post-confirmation failure.
        The original phase transition supplies the immutable signal evidence, so
        later forecast observations cannot reset its age or create another alert.
        """
        closed = latest_closed_time(timeframe, now)
        step = TIMEFRAME_SECONDS[timeframe]
        context = context_id(instrument_id, timeframe)
        with self.pool.public_transaction() as connection:
            row = connection.execute(
                """SELECT (t.checkpoint_json->>'last_ts')::bigint,
                     COALESCE((SELECT jsonb_agg(jsonb_build_object(
                       'id',r.value->'id','phase',r.value->'phase',
                       'observed_time',r.value->'observed_time','deadline_time',r.value->'deadline_time',
                       'latest',jsonb_build_object('levels',r.value->'latest'->'levels')))
                       FROM jsonb_each(COALESCE(t.checkpoint_json->'records','{}')) r
                       WHERE r.value->>'phase' IN ('FORMED','CONFIRMED')),'[]'::jsonb)
                   FROM market_pattern_tracking t
                   JOIN market_sync_state s USING(instrument_id,timeframe)
                   WHERE t.instrument_id=%s AND t.timeframe=%s AND t.version=%s
                     AND t.safe_error IS NULL AND s.active AND s.live_error_code IS NULL
                     AND s.last_live_at BETWEEN %s AND %s""",
                (instrument_id, timeframe, VERSION, now - timedelta(seconds=LIVE_FRESHNESS_SECONDS), now),
            ).fetchone()
            if not row or row[0] is None or row[0] + step != int(closed.timestamp()):
                return None
            records = {r["id"]: r for r in row[1]}
            # Preserve the original lifecycle stage, version and evidence ID.
            source = connection.execute(
                """SELECT DISTINCT ON(e.candidate_id,e.phase)
                     e.candidate_id,e.phase,gatescope_security.structure_event_record(e.event_json,e.event_blob),e.recorded_at,e.event_json->>'snapshot_at'
                   FROM jsonb_to_recordset(%s) g(id text,phase text)
                   JOIN market_pattern_events e ON e.context_id=%s
                     AND e.candidate_id=g.id AND e.phase=g.phase
                   WHERE e.version=%s AND e.source_mode='RECORDED'
                     AND e.event_json->>'reason' IN ('ADMITTED','FORMED','BREAKOUT')
                     AND e.observed_time>%s AND e.observed_time<=%s
                     AND e.recorded_at<=%s
                   ORDER BY e.candidate_id,e.phase,e.sequence LIMIT 512""",
                (
                    Jsonb([{"id": r["id"], "phase": r["phase"]} for r in records.values()]),
                    context,
                    VERSION,
                    now - signal_lifetime(timeframe),
                    closed,
                    now,
                ),
            ).fetchall()
            live = connection.execute(
                """SELECT open_time,high,low,close FROM market_live_candles
                   WHERE instrument_id=%s AND timeframe=%s AND observed_at BETWEEN %s AND %s
                     AND close_time>%s""",
                (instrument_id, timeframe, now - timedelta(seconds=LIVE_FRESHNESS_SECONDS), now, now),
            ).fetchone()
        bars = self.canonical_bars(tenant_id, subject_id, instrument_id, timeframe, limit=20)
        if not bars or bars[-1].close_time != closed:
            return None
        candles = [
            {
                "ts": int(b.open_time.timestamp()),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
            }
            for b in bars
        ]
        veto = (
            [
                {
                    "ts": int(live[0].timestamp()),
                    "high": float(live[1]),
                    "low": float(live[2]),
                    "close": float(live[3]),
                }
            ]
            if live
            else []
        )
        events = []
        for identifier, phase, event, recorded_at, snapshot_at in source:
            from gatescope.quant.structure_archive_codec import unpack_structure_record
            # Legacy test adapters may already supply a naked shape; real rows
            # always carry the complete immutable lifecycle record.
            if "latest" in event or "_archive" in event:
                event = unpack_structure_record(event)["latest"]
            if snapshot_at and datetime.fromisoformat(snapshot_at) > now:
                # Transaction now() can precede the detector's wall-clock
                # snapshot. Never consume a snapshot later than this cycle.
                continue
            record = records[identifier]
            checks = (
                [b for b in candles if b["ts"] + step > record["observed_time"]]
                if phase == "CONFIRMED"
                else [candles[-1]]
            ) + veto
            if event.get("confidence") is None or not usable(
                record, candles[-1], int(closed.timestamp()), step
            ):
                continue
            if any(not usable(record, b, int(closed.timestamp()), step) for b in checks):
                continue
            events.append(
                {
                    **event,
                    "detector_version": VERSION,
                    "context_id": context,
                    "input_basis": "IMMUTABLE_STRUCTURE_EVENT",
                    "source_mode": "RECORDED",
                    "source_recorded_at": recorded_at.isoformat(),
                    "source_snapshot_at": snapshot_at,
                }
            )
        return StructureSignalSnapshot(
            digest([VERSION, row[0], [e["evidence_id"] for e in events]]), bars, events
        )

    def persist_pattern_events(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        instrument_id: UUID,
        timeframe: str,
        events: Sequence[Mapping[str, Any]],
        bars: Sequence[Bar],
    ) -> list[PatternObservation]:
        from gatescope.application.pattern_trade_notice import build_pattern_trade_notice
        from gatescope.adapters.postgres_pattern_studies import snapshot

        if not bars:
            return []
        available_at = max(bar.available_at for bar in bars)
        quality_flags = sorted({flag for bar in bars for flag in bar.quality_flags})
        live_eligible = all(bar.live_eligible_at(available_at) for bar in bars)
        observations: list[PatternObservation] = []
        with self.pool.transaction(tenant_id, subject_id) as connection:
            for raw in events:
                payload = dict(raw)
                if payload.get("detector_version") != VERSION:
                    raise ValueError("STRATEGY_STRUCTURE_VERSION_MISMATCH")
                observed_at = datetime.fromtimestamp(int(payload["observed_time"]), tz=timezone.utc)
                source_available = max(observed_at, datetime.fromisoformat(payload["source_recorded_at"]))
                if payload.get("source_snapshot_at"):
                    source_available = max(source_available, datetime.fromisoformat(payload["source_snapshot_at"]))
                # The consumed input is the chart's immutable event, not the
                # short candle tail used only to veto a stale/failed setup.
                input_digest = digest([VERSION, payload["context_id"], payload["evidence_id"]])
                payload_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
                series_material = {
                    "instrument_id": str(instrument_id),
                    "timeframe": timeframe,
                    "pattern": str(payload["pattern"]),
                    "event_id": str(payload["id"]),
                    "detector_version": VERSION,
                }
                series_id = hashlib.sha256(
                    json.dumps(series_material, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                latest = connection.execute(
                    """SELECT fact_id,revision,payload_hash,available_at
                         FROM pattern_event_facts
                        WHERE tenant_id=%s AND series_id=%s ORDER BY revision DESC LIMIT 1""",
                    (tenant_id, series_id),
                ).fetchone()
                if latest is not None and str(latest[2]) == payload_hash:
                    fact_id, fact_available = str(latest[0]), latest[3]
                else:
                    revision = int(latest[1]) + 1 if latest is not None else 1
                    fact_id = hashlib.sha256(
                        f"{tenant_id}:{series_id}:{revision}:{payload_hash}".encode()
                    ).hexdigest()
                    event_time = observed_at
                    connection.execute(
                        """INSERT INTO pattern_event_facts
                             (id,tenant_id,fact_id,series_id,revision,supersedes_fact_id,
                              detector_version,instrument_id,timeframe,pattern_id,event_time,
                              available_at,confidence,confirmed,payload_json,payload_hash,
                              input_digest,quality_flags,live_eligible,created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,now())
                           ON CONFLICT (tenant_id,fact_id) DO NOTHING""",
                        (
                            uuid5(NAMESPACE_URL, f"gatescope:pattern-fact:{tenant_id}:{fact_id}"),
                            tenant_id,
                            fact_id,
                            series_id,
                            revision,
                            str(latest[0]) if latest is not None else None,
                            VERSION,
                            instrument_id,
                            timeframe,
                            str(payload["pattern"]),
                            event_time,
                            source_available,
                            float(payload["confidence"]),
                            bool(payload["confirmed"]),
                            Jsonb(payload),
                            payload_hash,
                            input_digest,
                            quality_flags,
                            live_eligible,
                        ),
                    )
                    fact_available = source_available
                reference = {}
                if payload.get("source_snapshot_at"):
                    # Match the original transition's point-in-time OHLC
                    # revision, not a later correction in the live candle tail.
                    source_snapshot = datetime.fromisoformat(payload["source_snapshot_at"])
                    if observed_at <= source_snapshot <= source_available:
                        original_bars, _ = snapshot(connection, instrument_id, timeframe, 1,
                                                    source_snapshot, end_time=observed_at, strict=False)
                        if original_bars and original_bars[-1]["ts"] + TIMEFRAME_SECONDS[timeframe] == int(observed_at.timestamp()):
                            reference = original_bars[-1]
                observations.append(
                    PatternObservation(
                        fact_id=fact_id,
                        series_id=series_id,
                        pattern_id=str(payload["pattern"]),
                        confidence=float(payload["confidence"]),
                        confirmed=bool(payload["confirmed"]),
                        end_time=datetime.fromtimestamp(int(payload["end_time"]), tz=timezone.utc),
                        available_at=fact_available,
                        payload_hash=payload_hash,
                        direction=str(payload.get("signal_direction", "neutral")),
                        observed_at=observed_at,
                        detector_version=VERSION,
                        evidence_id=str(payload["evidence_id"]),
                        context_id=str(payload["context_id"]),
                        candidate_id=str(payload["id"]),
                        phase=str(payload["phase"]),
                        source_event=payload,
                        trade_notice=build_pattern_trade_notice(payload, reference),
                    )
                )
        return observations

    def last_alert_at(
        self,
        tenant_id: UUID,
        subject_id: UUID,
        rule_id: UUID,
    ) -> datetime | None:
        with self.pool.transaction(tenant_id, subject_id) as connection:
            row = connection.execute(
                "SELECT max(available_at) FROM alert_event_facts WHERE alert_rule_id=%s",
                (rule_id,),
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    def heartbeat(self, worker_id: str, *, running: bool, status: Mapping[str, Any]) -> None:
        from gatescope.application.storage_pressure import safe_storage_status

        storage = safe_storage_status(status.get("storage_pressure"))
        safe_status: dict = {
            "tenant_count": max(0, int(status.get("tenant_count", 0))),
            "rule_count": max(0, int(status.get("rule_count", 0))),
            "scanned_rules": max(0, int(status.get("scanned_rules", 0))),
            "emitted_alerts": max(0, int(status.get("emitted_alerts", 0))),
            "failed_rules": max(0, int(status.get("failed_rules", 0))),
            "safe_error_code": str(status.get("safe_error_code") or "")[:80] or None,
        }
        if storage:
            safe_status.update(history_state=storage["state"], storage_pressure=storage)
        with self.pool.public_transaction() as connection:
            connection.execute(
                """INSERT INTO worker_heartbeats
                     (workload,worker_id,running,heartbeat_at,status_json)
                   VALUES ('strategy-worker',%s,%s,now(),%s::jsonb)
                   ON CONFLICT (workload) DO UPDATE SET
                     worker_id=EXCLUDED.worker_id,running=EXCLUDED.running,
                     heartbeat_at=EXCLUDED.heartbeat_at,status_json=EXCLUDED.status_json""",
                (worker_id[:80], running, json.dumps(safe_status, separators=(",", ":"))),
            )
