"""Immutable-version alert rule repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool


class AlertRuleNotFound(LookupError):
    pass


class AlertRuleConflict(RuntimeError):
    pass


class AlertRulePreconditionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class AlertRule:
    id: UUID
    name: str
    status: str
    etag: int
    current_version: int
    version_id: UUID
    condition: Mapping[str, Any]
    routing: Mapping[str, Any]
    version_created_by: UUID
    version_created_at: datetime
    created_at: datetime
    updated_at: datetime
    symbol: str = ""


class PostgresAlertRuleRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def list(self, tenant_id: UUID, owner_id: UUID, *, limit: int = 100) -> list[AlertRule]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                self._select() + " WHERE r.owner_user_id=%s AND r.status<>'DELETED' ORDER BY r.updated_at DESC, r.id DESC LIMIT %s",
                (owner_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._rule(row) for row in rows]

    def get(self, tenant_id: UUID, owner_id: UUID, rule_id: UUID) -> AlertRule:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                self._select() + " WHERE r.id=%s AND r.owner_user_id=%s AND r.status<>'DELETED'",
                (rule_id, owner_id),
            ).fetchone()
        if row is None:
            raise AlertRuleNotFound("alert rule not found")
        return self._rule(row)

    def list_events(
        self, tenant_id: UUID, owner_id: UUID, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                """SELECT a.id,r.id,r.name,i.symbol,a.timeframe,a.available_at,
                          a.payload_json,a.payload_hash
                     FROM alert_event_facts a
                     JOIN alert_rules r ON r.id=a.alert_rule_id AND r.owner_user_id=%s
                     JOIN instruments i ON i.id=a.instrument_id
                    ORDER BY a.available_at DESC,a.id DESC LIMIT %s""",
                (owner_id, max(1, min(limit, 200))),
            ).fetchall()
        return [
            {
                "id": str(row[0]), "rule_id": str(row[1]), "rule_name": str(row[2]),
                "symbol": str(row[3]), "timeframe": str(row[4]),
                "available_at": row[5], "payload": dict(row[6]),
                "payload_hash": str(row[7]),
            }
            for row in rows
        ]

    def create(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        *,
        name: str,
        condition: Mapping[str, Any],
        routing: Mapping[str, Any],
    ) -> AlertRule:
        rule_id, version_id = uuid4(), uuid4()
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                self._require_instrument(connection, condition)
                connection.execute(
                    """INSERT INTO alert_rules
                         (id, tenant_id, owner_user_id, name, current_version, status, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, 1, 'ACTIVE', now(), now())""",
                    (rule_id, tenant_id, owner_id, _name(name)),
                )
                connection.execute(
                    """INSERT INTO alert_rule_versions
                         (id, tenant_id, alert_rule_id, version, condition_json, routing_json,
                          created_by, created_at)
                       VALUES (%s, %s, %s, 1, %s, %s, %s, now())""",
                    (version_id, tenant_id, rule_id, Jsonb(dict(condition)), Jsonb(dict(routing)), owner_id),
                )
        except UniqueViolation as exc:
            raise AlertRuleConflict("active alert rule name already exists") from exc
        return self.get(tenant_id, owner_id, rule_id)

    def rename(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        rule_id: UUID,
        *,
        name: str,
        expected_etag: int,
    ) -> AlertRule:
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                updated = connection.execute(
                    """UPDATE alert_rules SET name=%s, etag=etag+1, updated_at=now()
                       WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' AND etag=%s
                       RETURNING id""",
                    (_name(name), rule_id, owner_id, expected_etag),
                ).fetchone()
                if updated is None:
                    self._raise_missing_or_stale(connection, owner_id, rule_id)
        except UniqueViolation as exc:
            raise AlertRuleConflict("active alert rule name already exists") from exc
        return self.get(tenant_id, owner_id, rule_id)

    def add_version(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        rule_id: UUID,
        *,
        condition: Mapping[str, Any],
        routing: Mapping[str, Any],
        expected_etag: int,
    ) -> AlertRule:
        version_id = uuid4()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """SELECT current_version, etag FROM alert_rules
                   WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' FOR UPDATE""",
                (rule_id, owner_id),
            ).fetchone()
            if row is None:
                raise AlertRuleNotFound("alert rule not found")
            if int(row[1]) != expected_etag:
                raise AlertRulePreconditionFailed("alert rule etag changed")
            self._require_instrument(connection, condition)
            version = int(row[0]) + 1
            connection.execute(
                """INSERT INTO alert_rule_versions
                     (id, tenant_id, alert_rule_id, version, condition_json, routing_json,
                      created_by, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, now())""",
                (
                    version_id, tenant_id, rule_id, version,
                    Jsonb(dict(condition)), Jsonb(dict(routing)), owner_id,
                ),
            )
            connection.execute(
                """UPDATE alert_rules SET current_version=%s, etag=etag+1, updated_at=now()
                   WHERE id=%s""",
                (version, rule_id),
            )
        return self.get(tenant_id, owner_id, rule_id)

    def update(self, tenant_id: UUID, owner_id: UUID, rule_id: UUID, *, name: str,
               condition: Mapping[str, Any], routing: Mapping[str, Any], expected_etag: int) -> AlertRule:
        """Atomically rename and append a version; never mutate historical conditions."""
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                row = connection.execute(
                    "SELECT current_version,etag FROM alert_rules WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' FOR UPDATE",
                    (rule_id, owner_id),
                ).fetchone()
                if row is None:
                    raise AlertRuleNotFound("alert rule not found")
                if int(row[1]) != expected_etag:
                    raise AlertRulePreconditionFailed("alert rule etag changed")
                self._require_instrument(connection, condition)
                for channel_id in routing.get("channel_ids", []):
                    if connection.execute("SELECT id FROM notification_channels WHERE id=%s AND owner_user_id=%s AND status='ACTIVE'", (channel_id, owner_id)).fetchone() is None:
                        raise AlertRuleConflict("notification channel is unavailable")
                version = int(row[0]) + 1
                connection.execute("""INSERT INTO alert_rule_versions
                    (id,tenant_id,alert_rule_id,version,condition_json,routing_json,created_by,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,now())""",
                    (uuid4(), tenant_id, rule_id, version, Jsonb(dict(condition)), Jsonb(dict(routing)), owner_id))
                connection.execute("UPDATE alert_rules SET name=%s,current_version=%s,etag=etag+1,updated_at=now() WHERE id=%s",
                    (_name(name), version, rule_id))
        except UniqueViolation as exc:
            raise AlertRuleConflict("active alert rule name already exists") from exc
        return self.get(tenant_id, owner_id, rule_id)

    def delete(self, tenant_id: UUID, owner_id: UUID, rule_id: UUID, *, expected_etag: int) -> None:
        """Soft delete, retaining versions, signals and historical delivery evidence."""
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute("""UPDATE alert_rules SET status='DELETED',etag=etag+1,updated_at=now()
                WHERE id=%s AND owner_user_id=%s AND status<>'DELETED' AND etag=%s RETURNING id""",
                (rule_id, owner_id, expected_etag)).fetchone()
            if row is None:
                self._raise_missing_or_stale(connection, owner_id, rule_id)

    def pause(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        rule_id: UUID,
        *,
        expected_etag: int,
    ) -> AlertRule:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            updated = connection.execute(
                """UPDATE alert_rules SET status='PAUSED', etag=etag+1, updated_at=now()
                   WHERE id=%s AND owner_user_id=%s AND status='ACTIVE' AND etag=%s
                   RETURNING id""",
                (rule_id, owner_id, expected_etag),
            ).fetchone()
            if updated is None:
                found = connection.execute(
                    """SELECT status, etag FROM alert_rules
                       WHERE id=%s AND owner_user_id=%s AND status<>'DELETED'""",
                    (rule_id, owner_id),
                ).fetchone()
                if found is None:
                    raise AlertRuleNotFound("alert rule not found")
                if int(found[1]) != expected_etag:
                    raise AlertRulePreconditionFailed("alert rule etag changed")
                raise AlertRuleConflict("alert rule is not active")
        return self.get(tenant_id, owner_id, rule_id)

    def resume(self, tenant_id: UUID, owner_id: UUID, rule_id: UUID, *, expected_etag: int) -> AlertRule:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                self._select() + " WHERE r.id=%s AND r.owner_user_id=%s AND r.status<>'DELETED' FOR UPDATE OF r",
                (rule_id, owner_id),
            ).fetchone()
            if row is None:
                raise AlertRuleNotFound("alert rule not found")
            rule = self._rule(row)
            if rule.etag != expected_etag:
                raise AlertRulePreconditionFailed("alert rule etag changed")
            if rule.status != "PAUSED":
                raise AlertRuleConflict("alert rule is not paused")
            self._require_instrument(connection, rule.condition)
            connection.execute(
                "UPDATE alert_rules SET status='ACTIVE',etag=etag+1,updated_at=now() WHERE id=%s",
                (rule_id,),
            )
        return self.get(tenant_id, owner_id, rule_id)

    @staticmethod
    def _select() -> str:
        return """SELECT r.id, r.name, r.status, r.etag, r.current_version,
                         v.id, v.condition_json, v.routing_json, v.created_by, v.created_at,
                         r.created_at, r.updated_at, i.symbol
                  FROM alert_rules r
                  JOIN alert_rule_versions v
                    ON v.tenant_id=r.tenant_id AND v.alert_rule_id=r.id
                   AND v.version=r.current_version
                  LEFT JOIN instruments i ON i.id=(v.condition_json->>'instrument_id')::uuid"""

    @staticmethod
    def _require_instrument(connection: Any, condition: Mapping[str, Any]) -> None:
        instrument_id = condition.get("instrument_id")
        row = connection.execute(
            "SELECT id FROM instruments WHERE id=%s AND status='ACTIVE'",
            (instrument_id,),
        ).fetchone()
        if row is None:
            raise AlertRuleConflict("instrument is unavailable")

    @staticmethod
    def _raise_missing_or_stale(connection: Any, owner_id: UUID, rule_id: UUID) -> None:
        found = connection.execute(
            """SELECT etag FROM alert_rules
               WHERE id=%s AND owner_user_id=%s AND status<>'DELETED'""",
            (rule_id, owner_id),
        ).fetchone()
        if found is None:
            raise AlertRuleNotFound("alert rule not found")
        raise AlertRulePreconditionFailed("alert rule etag changed")

    @staticmethod
    def _rule(row: Any) -> AlertRule:
        return AlertRule(
            id=row[0], name=str(row[1]), status=str(row[2]), etag=int(row[3]),
            current_version=int(row[4]), version_id=row[5], condition=dict(row[6]),
            routing=dict(row[7]), version_created_by=row[8], version_created_at=row[9],
            created_at=row[10], updated_at=row[11], symbol=str(row[12] or ""),
        )


def _name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 120:
        raise ValueError("alert rule name must be between 1 and 120 characters")
    return cleaned
