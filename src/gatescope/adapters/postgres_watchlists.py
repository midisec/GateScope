"""Tenant-isolated watchlist repository with optimistic concurrency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import ForeignKeyViolation, UniqueViolation

from gatescope.adapters.postgres import PostgresPool
from gatescope.domain.market import TIMEFRAME_SECONDS


class WatchlistNotFound(LookupError):
    pass


class WatchlistConflict(RuntimeError):
    pass


class WatchlistPreconditionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class WatchlistItem:
    id: UUID
    instrument_id: UUID
    venue: str
    market: str
    symbol: str
    position: int
    default_timeframe: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Watchlist:
    id: UUID
    name: str
    etag: int
    items: tuple[WatchlistItem, ...]
    created_at: datetime
    updated_at: datetime


class PostgresWatchlistRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self.pool = pool

    def list(self, tenant_id: UUID, owner_id: UUID, *, limit: int = 100) -> list[Watchlist]:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            rows = connection.execute(
                """SELECT id, name, etag, created_at, updated_at
                   FROM watchlists WHERE owner_user_id=%s AND deleted_at IS NULL
                   ORDER BY updated_at DESC, id DESC LIMIT %s""",
                (owner_id, max(1, min(limit, 500))),
            ).fetchall()
            return [self._watchlist(connection, row) for row in rows]

    def get(self, tenant_id: UUID, owner_id: UUID, watchlist_id: UUID) -> Watchlist:
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = self._row(connection, owner_id, watchlist_id)
            if row is None:
                raise WatchlistNotFound("watchlist not found")
            return self._watchlist(connection, row)

    def create(self, tenant_id: UUID, owner_id: UUID, name: str) -> Watchlist:
        watchlist_id = uuid4()
        with self.pool.transaction(tenant_id, owner_id) as connection:
            row = connection.execute(
                """INSERT INTO watchlists
                     (id, tenant_id, owner_user_id, name, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, now(), now())
                   ON CONFLICT DO NOTHING
                   RETURNING id, name, etag, created_at, updated_at""",
                (watchlist_id, tenant_id, owner_id, _name(name)),
            ).fetchone()
            if row is None:
                raise WatchlistConflict("active watchlist name already exists")
            return self._watchlist(connection, row)

    def rename(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        watchlist_id: UUID,
        *,
        name: str,
        expected_etag: int,
    ) -> Watchlist:
        if expected_etag < 1:
            raise ValueError("watchlist etag must be positive")
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                row = connection.execute(
                    """UPDATE watchlists SET name=%s, etag=etag+1, updated_at=now()
                       WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL AND etag=%s
                       RETURNING id, name, etag, created_at, updated_at""",
                    (_name(name), watchlist_id, owner_id, expected_etag),
                ).fetchone()
                if row is None:
                    self._raise_missing_or_stale(connection, owner_id, watchlist_id)
                return self._watchlist(connection, row)
        except UniqueViolation as exc:
            raise WatchlistConflict("active watchlist name already exists") from exc

    def add_item(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        watchlist_id: UUID,
        *,
        instrument_id: UUID,
        default_timeframe: str,
        expected_etag: int,
    ) -> Watchlist:
        if default_timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("unsupported default timeframe")
        if expected_etag < 1:
            raise ValueError("watchlist etag must be positive")
        try:
            with self.pool.transaction(tenant_id, owner_id) as connection:
                current = connection.execute(
                    """SELECT etag FROM watchlists
                       WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL FOR UPDATE""",
                    (watchlist_id, owner_id),
                ).fetchone()
                if current is None:
                    raise WatchlistNotFound("watchlist not found")
                if int(current[0]) != expected_etag:
                    raise WatchlistPreconditionFailed("watchlist etag changed")
                instrument = connection.execute(
                    "SELECT id FROM instruments WHERE id=%s AND status='ACTIVE'",
                    (instrument_id,),
                ).fetchone()
                if instrument is None:
                    raise WatchlistConflict("instrument is unavailable")
                position_row = connection.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM watchlist_items WHERE watchlist_id=%s",
                    (watchlist_id,),
                ).fetchone()
                position = int(position_row[0]) if position_row else 0
                connection.execute(
                    """INSERT INTO watchlist_items
                         (id, tenant_id, watchlist_id, instrument_id, position,
                          default_timeframe, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, now(), now())""",
                    (uuid4(), tenant_id, watchlist_id, instrument_id, position, default_timeframe),
                )
                row = connection.execute(
                    """UPDATE watchlists SET etag=etag+1, updated_at=now()
                       WHERE id=%s RETURNING id, name, etag, created_at, updated_at""",
                    (watchlist_id,),
                ).fetchone()
                return self._watchlist(connection, row)
        except (ForeignKeyViolation, UniqueViolation) as exc:
            raise WatchlistConflict("instrument is already present or unavailable") from exc

    def remove_item(
        self,
        tenant_id: UUID,
        owner_id: UUID,
        watchlist_id: UUID,
        *,
        instrument_id: UUID,
        expected_etag: int,
    ) -> Watchlist:
        if expected_etag < 1:
            raise ValueError("watchlist etag must be positive")
        with self.pool.transaction(tenant_id, owner_id) as connection:
            current = connection.execute(
                """SELECT etag FROM watchlists
                   WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL FOR UPDATE""",
                (watchlist_id, owner_id),
            ).fetchone()
            if current is None:
                raise WatchlistNotFound("watchlist not found")
            if int(current[0]) != expected_etag:
                raise WatchlistPreconditionFailed("watchlist etag changed")
            deleted = connection.execute(
                """DELETE FROM watchlist_items
                   WHERE watchlist_id=%s AND instrument_id=%s RETURNING id""",
                (watchlist_id, instrument_id),
            ).fetchone()
            if deleted is None:
                raise WatchlistNotFound("watchlist item not found")
            row = connection.execute(
                """UPDATE watchlists SET etag=etag+1, updated_at=now()
                   WHERE id=%s RETURNING id, name, etag, created_at, updated_at""",
                (watchlist_id,),
            ).fetchone()
            return self._watchlist(connection, row)

    @staticmethod
    def _row(connection: Any, owner_id: UUID, watchlist_id: UUID) -> Any:
        return connection.execute(
            """SELECT id, name, etag, created_at, updated_at FROM watchlists
               WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL""",
            (watchlist_id, owner_id),
        ).fetchone()

    @staticmethod
    def _raise_missing_or_stale(connection: Any, owner_id: UUID, watchlist_id: UUID) -> None:
        found = connection.execute(
            "SELECT etag FROM watchlists WHERE id=%s AND owner_user_id=%s AND deleted_at IS NULL",
            (watchlist_id, owner_id),
        ).fetchone()
        if found is None:
            raise WatchlistNotFound("watchlist not found")
        raise WatchlistPreconditionFailed("watchlist etag changed")

    @staticmethod
    def _watchlist(connection: Any, row: Any) -> Watchlist:
        item_rows = connection.execute(
            """SELECT wi.id, wi.instrument_id, i.venue, i.market, i.symbol, wi.position,
                      wi.default_timeframe, wi.created_at, wi.updated_at
               FROM watchlist_items wi JOIN instruments i ON i.id=wi.instrument_id
               WHERE wi.watchlist_id=%s ORDER BY wi.position, wi.id""",
            (row[0],),
        ).fetchall()
        return Watchlist(
            id=row[0], name=str(row[1]), etag=int(row[2]),
            created_at=row[3], updated_at=row[4],
            items=tuple(
                WatchlistItem(
                    id=item[0], instrument_id=item[1], venue=str(item[2]), market=str(item[3]),
                    symbol=str(item[4]), position=int(item[5]), default_timeframe=str(item[6]),
                    created_at=item[7], updated_at=item[8],
                )
                for item in item_rows
            ),
        )


def _name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 120:
        raise ValueError("watchlist name must be between 1 and 120 characters")
    return cleaned
