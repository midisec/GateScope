"""Causal pattern-rule evaluation for the formal strategy workload."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from gatescope.adapters.postgres_strategy import PostgresStrategyRepository
from gatescope.application.alerts import PostgresAlertWriter, evaluate_alert


@dataclass(frozen=True)
class StrategyCycleResult:
    tenants: int
    rules: int
    scanned: int
    emitted: int
    failed: int


class FormalStrategyRunner:
    """Scans each active immutable rule version once per new data watermark."""

    def __init__(
        self,
        repository: PostgresStrategyRepository,
        writer: PostgresAlertWriter,
        *,
        subject_id: UUID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.writer = writer
        self.subject_id = subject_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._watermarks: dict[UUID, str] = {}

    def run_once(self) -> StrategyCycleResult:
        tenant_ids = self.repository.due_tenants()
        rule_count = scanned = emitted = failed = 0
        now = self._clock()
        snapshots = {}
        active_versions = set()
        for tenant_id in tenant_ids:
            for active in self.repository.active_rules(tenant_id, self.subject_id):
                rule_count += 1
                rule = active.snapshot
                active_versions.add(rule.version_id)
                try:
                    pair = (rule.instrument_id, rule.timeframe)
                    if pair not in snapshots:
                        snapshots[pair] = self.repository.structure_snapshot(
                            tenant_id,
                            self.subject_id,
                            *pair,
                            now=now,
                        )
                    snapshot = snapshots[pair]
                    if snapshot is None:
                        continue
                    if self._watermarks.get(rule.version_id) == snapshot.key:
                        continue
                    observations = self.repository.persist_pattern_events(
                        tenant_id,
                        self.subject_id,
                        rule.instrument_id,
                        rule.timeframe,
                        [event for event in snapshot.events if event["pattern"] in rule.pattern_ids],
                        snapshot.bars,
                    )
                    last_emitted = self.repository.last_alert_at(tenant_id, self.subject_id, rule.rule_id)
                    # ANY means each independent matching event, not only the
                    # highest-scoring event forever hidden behind one watermark.
                    # ALL remains one same-direction coincidence decision.
                    groups = [[item] for item in observations] if rule.match_mode == "ANY" else [observations]
                    for group in groups:
                        decision = evaluate_alert(
                            rule, group, bar_open_time=snapshot.bars[-1].open_time,
                            watermark=now, now=now, last_emitted_at=last_emitted,
                        )
                        if decision is not None and self.writer.record(tenant_id, self.subject_id, decision):
                            emitted += 1
                    self._watermarks[rule.version_id] = snapshot.key
                    scanned += 1
                except (KeyError, TypeError, ValueError):
                    failed += 1
        self._watermarks = {key: value for key, value in self._watermarks.items() if key in active_versions}
        return StrategyCycleResult(len(tenant_ids), rule_count, scanned, emitted, failed)
