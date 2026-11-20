"""Versioned, causal alert evaluation with idempotent fact/outbox persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS, utc
from gatescope.application.signal_freshness import POLICY, latest_closed_time, signal_lifetime


@dataclass(frozen=True)
class AlertRuleSnapshot:
    rule_id: UUID
    version_id: UUID
    version: int
    instrument_id: UUID
    timeframe: str
    pattern_ids: tuple[str, ...]
    match_mode: Literal["ANY", "ALL"]
    minimum_confidence: float
    confirmed_only: bool
    coincidence_bars: int
    cooldown_seconds: int

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("unsupported alert timeframe")
        if not self.pattern_ids or len(set(self.pattern_ids)) != len(self.pattern_ids):
            raise ValueError("alert patterns must be non-empty and unique")
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum confidence must be between 0 and 1")
        if self.coincidence_bars < 1 or self.cooldown_seconds < 0 or self.version < 1:
            raise ValueError("alert rule bounds are invalid")


@dataclass(frozen=True)
class PatternObservation:
    fact_id: str
    series_id: str
    pattern_id: str
    confidence: float
    confirmed: bool
    end_time: datetime
    available_at: datetime
    payload_hash: str
    direction: str = "neutral"
    observed_at: datetime | None = None
    detector_version: str | None = None
    evidence_id: str | None = None
    context_id: str | None = None
    candidate_id: str | None = None
    phase: str | None = None
    source_event: Mapping[str, Any] | None = None
    trade_notice: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AlertDecision:
    event_key: str
    rule_id: UUID
    rule_version_id: UUID
    instrument_id: UUID
    timeframe: str
    available_at: datetime
    matched_fact_ids: tuple[str, ...]
    payload_hash: str
    payload_json: str


def evaluate_alert(
    rule: AlertRuleSnapshot,
    observations: Iterable[PatternObservation],
    *,
    bar_open_time: datetime,
    watermark: datetime,
    last_emitted_at: datetime | None = None,
    now: datetime | None = None,
) -> AlertDecision | None:
    bar_time = utc(bar_open_time)
    as_of = utc(watermark)
    current = utc(now or datetime.now(timezone.utc))
    step = timedelta(seconds=TIMEFRAME_SECONDS[rule.timeframe])
    closed = bar_time + step
    if closed != latest_closed_time(rule.timeframe, current) or as_of > current:
        return None
    if last_emitted_at is not None and as_of < utc(last_emitted_at) + timedelta(
        seconds=rule.cooldown_seconds
    ):
        return None
    cutoff = closed - step * rule.coincidence_bars

    def observed_time(observation: PatternObservation) -> datetime:
        return utc(observation.observed_at) if observation.observed_at else utc(observation.end_time) + step

    eligible = [
        observation
        for observation in observations
        if observation.pattern_id in rule.pattern_ids
        and observation.confidence >= rule.minimum_confidence
        and (observation.confirmed or not rule.confirmed_only)
        and utc(observation.available_at) <= as_of
        and cutoff <= observed_time(observation) <= closed
        and current < observed_time(observation) + signal_lifetime(rule.timeframe)
    ]
    if not eligible:
        return None
    if rule.match_mode == "ALL":
        latest: dict[str, PatternObservation] = {}
        for observation in eligible:
            previous = latest.get(observation.pattern_id)
            if previous is None or _observation_order(observation) > _observation_order(previous):
                latest[observation.pattern_id] = observation
        if set(latest) != set(rule.pattern_ids):
            return None
        matched = [latest[pattern_id] for pattern_id in sorted(rule.pattern_ids)]
        directions = {observation.direction for observation in matched}
        if len(directions) != 1 or not directions.issubset({"bullish", "bearish"}):
            return None
    else:
        matched = [max(eligible, key=_observation_order)]
    matched_ids = tuple(sorted(observation.fact_id for observation in matched))
    key_material = {
        "matched_fact_ids": matched_ids,
        "rule_version_id": str(rule.version_id),
    }
    event_key = hashlib.sha256(_canonical_json(key_material).encode()).hexdigest()
    payload = {
        "alert_rule_id": str(rule.rule_id),
        "alert_rule_version": rule.version,
        "alert_rule_version_id": str(rule.version_id),
        "event_key": event_key,
        "instrument_id": str(rule.instrument_id),
        "signal_policy": POLICY,
        "observed_at": min(observed_time(item) for item in matched).isoformat(),
        "expires_at": min(
            observed_time(item) + signal_lifetime(rule.timeframe) for item in matched
        ).isoformat(),
        "direction": matched[0].direction,
        "matched": [
            {
                "confidence": observation.confidence,
                "confirmed": observation.confirmed,
                "fact_id": observation.fact_id,
                "pattern_id": observation.pattern_id,
                "payload_hash": observation.payload_hash,
                "series_id": observation.series_id,
                "direction": observation.direction,
                "phase": observation.phase,
                "observed_at": observed_time(observation).isoformat(),
                "detector_version": observation.detector_version,
                "evidence_id": observation.evidence_id,
                "context_id": observation.context_id,
                "candidate_id": observation.candidate_id,
                **({"source_event": dict(observation.source_event)} if observation.source_event else {}),
                **({"trade_notice": dict(observation.trade_notice)} if observation.trade_notice else {}),
            }
            for observation in matched
        ],
        "timeframe": rule.timeframe,
    }
    payload_json = _canonical_json(payload)
    return AlertDecision(
        event_key=event_key,
        rule_id=rule.rule_id,
        rule_version_id=rule.version_id,
        instrument_id=rule.instrument_id,
        timeframe=rule.timeframe,
        available_at=as_of,
        matched_fact_ids=matched_ids,
        payload_hash=hashlib.sha256(payload_json.encode()).hexdigest(),
        payload_json=payload_json,
    )


class PostgresAlertWriter:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def record(self, tenant_id: UUID, subject_id: UUID, decision: AlertDecision) -> bool:
        alert_id = uuid5(NAMESPACE_URL, f"gatescope:alert:{tenant_id}:{decision.event_key}")
        outbox_id = uuid5(NAMESPACE_URL, f"gatescope:outbox:notification:{alert_id}")
        payload = json.loads(decision.payload_json)
        source_keys = [
            {"context_id": item["context_id"], "evidence_id": item["evidence_id"]}
            for item in payload["matched"] if item.get("context_id") and item.get("evidence_id")
        ]
        with self.pool.transaction(tenant_id, subject_id) as connection:
            if source_keys and len(source_keys) == len(payload["matched"]):
                # Presentation metadata can create a new fact revision without
                # creating a new lifecycle event. Keep pre-upgrade event keys
                # intact and dedupe by original evidence within this version.
                connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                   (f"alert-source:{tenant_id}:{decision.rule_version_id}",))
                duplicate = connection.execute(
                    """SELECT 1 FROM alert_event_facts
                       WHERE tenant_id=%s AND alert_rule_id=%s AND alert_rule_version_id=%s
                         AND available_at>=%s
                         AND payload_json->'matched' @> %s
                         AND jsonb_array_length(payload_json->'matched')=%s LIMIT 1""",
                    (tenant_id, decision.rule_id, decision.rule_version_id,
                     datetime.fromisoformat(payload["observed_at"]), Jsonb(source_keys), len(source_keys)),
                ).fetchone()
                if duplicate:
                    return False
            inserted = connection.execute(
                """INSERT INTO alert_event_facts
                     (id, tenant_id, alert_rule_id, alert_rule_version_id, event_key,
                      instrument_id, timeframe, available_at, matched_fact_ids,
                      payload_json, payload_hash, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (tenant_id, event_key) DO NOTHING
                   RETURNING id""",
                (
                    alert_id,
                    tenant_id,
                    decision.rule_id,
                    decision.rule_version_id,
                    decision.event_key,
                    decision.instrument_id,
                    decision.timeframe,
                    decision.available_at,
                    list(decision.matched_fact_ids),
                    Jsonb(json.loads(decision.payload_json)),
                    decision.payload_hash,
                ),
            ).fetchone()
            if inserted is None:
                return False
            connection.execute(
                """INSERT INTO outbox_events
                     (event_id, tenant_id, aggregate_type, aggregate_id, event_type,
                      schema_version, payload_json, occurred_at, created_at, next_attempt_at)
                   VALUES (%s, %s, 'ALERT', %s, 'notification.requested.v1', 1,
                           %s, %s, now(), now())
                   ON CONFLICT (event_id) DO NOTHING""",
                (
                    outbox_id,
                    tenant_id,
                    alert_id,
                    Jsonb(json.loads(decision.payload_json)),
                    decision.available_at,
                ),
            )
        return True


def _observation_order(observation: PatternObservation) -> tuple[datetime, float, str]:
    return utc(observation.observed_at or observation.end_time), observation.confidence, observation.fact_id


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
