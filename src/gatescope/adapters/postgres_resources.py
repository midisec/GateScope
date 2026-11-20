"""Tenant/owner-bound versioned configuration repositories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation

from gatescope.adapters.postgres import PostgresPool

ResourceKind = Literal["chart_layout", "indicator_template"]
TABLES: dict[ResourceKind, str] = {
    "chart_layout": "chart_layouts",
    "indicator_template": "indicator_templates",
}


class ResourceNotFound(LookupError):
    pass


class ResourceConflict(RuntimeError):
    pass


class ResourcePreconditionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionedResource:
    id: UUID
    kind: ResourceKind
    name: str
    spec: Mapping[str, Any]
    etag: int
    created_at: Any
    updated_at: Any


class PostgresResourceRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def list(self, kind: ResourceKind, tenant_id: UUID, owner_id: UUID, *, limit: int = 100) -> list[VersionedResource]:
        table = TABLES[kind]
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                f"""SELECT id, name, spec_json, etag, created_at, updated_at
                    FROM {table} WHERE owner_user_id=%s AND deleted_at IS NULL
                    ORDER BY updated_at DESC, id DESC LIMIT %s""",
                (owner_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._resource(kind, row) for row in rows]

    def get(self, kind: ResourceKind, tenant_id: UUID, owner_id: UUID, resource_id: UUID) -> VersionedResource:
        table = TABLES[kind]
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                f"""SELECT id, name, spec_json, etag, created_at, updated_at
                    FROM {table} WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL""",
                (resource_id, owner_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("resource not found")
        return self._resource(kind, row)

    def create(
        self,
        kind: ResourceKind,
        tenant_id: UUID,
        owner_id: UUID,
        *,
        name: str,
        spec: Mapping[str, Any],
    ) -> VersionedResource:
        table = TABLES[kind]
        resource_id = uuid4()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                f"""INSERT INTO {table}
                     (id, tenant_id, owner_user_id, name, spec_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, now(), now())
                   ON CONFLICT DO NOTHING
                   RETURNING id, name, spec_json, etag, created_at, updated_at""",
                (resource_id, tenant_id, owner_id, _name(name), Jsonb(dict(spec))),
            ).fetchone()
        if row is None:
            raise ResourceConflict("active resource name already exists")
        return self._resource(kind, row)

    def update(
        self,
        kind: ResourceKind,
        tenant_id: UUID,
        owner_id: UUID,
        resource_id: UUID,
        *,
        name: str,
        spec: Mapping[str, Any],
        expected_etag: int,
    ) -> VersionedResource:
        table = TABLES[kind]
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                row = connection.execute(
                    f"""UPDATE {table} SET name=%s, spec_json=%s, etag=etag+1, updated_at=now()
                        WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL AND etag=%s
                        RETURNING id, name, spec_json, etag, created_at, updated_at""",
                    (_name(name), Jsonb(dict(spec)), resource_id, owner_id, expected_etag),
                ).fetchone()
                if row is None:
                    current = connection.execute(
                        f"SELECT etag FROM {table} WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL",
                        (resource_id, owner_id),
                    ).fetchone()
                    if current is None:
                        raise ResourceNotFound("resource not found")
                    raise ResourcePreconditionFailed("resource etag changed")
        except UniqueViolation as exc:
            raise ResourceConflict("active resource name already exists") from exc
        return self._resource(kind, row)

    def soft_delete(self, kind: ResourceKind, tenant_id: UUID, owner_id: UUID, resource_id: UUID) -> None:
        table = TABLES[kind]
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                f"""UPDATE {table} SET deleted_at=now(), etag=etag+1, updated_at=now()
                    WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL RETURNING id""",
                (resource_id, owner_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFound("resource not found")

    @staticmethod
    def _resource(kind: ResourceKind, row: Any) -> VersionedResource:
        return VersionedResource(
            id=row[0], kind=kind, name=str(row[1]), spec=dict(row[2]), etag=int(row[3]),
            created_at=row[4], updated_at=row[5],
        )


def _name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 120:
        raise ValueError("resource name must be between 1 and 120 characters")
    return cleaned
