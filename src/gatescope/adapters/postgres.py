"""PostgreSQL pool with transaction-local tenant/subject context."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, TypeVar
from uuid import UUID

from psycopg import Connection, Error as PostgresError
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool


_Row = TypeVar("_Row")


def required_row(row: _Row | None) -> _Row:
    """Unwrap a query whose SQL promises one row, including scalar aggregates."""
    if row is None:
        raise RuntimeError("EXPECTED_DATABASE_ROW_MISSING")
    return row


def validated_uuid(value: str | UUID) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("tenant/subject context must be a UUID") from exc


def reset_connection(connection: Connection[Any]) -> None:
    if connection.info.transaction_status != TransactionStatus.IDLE:
        connection.rollback()
    connection.execute("RESET ALL")
    connection.commit()


class PostgresPool:
    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        # Workload configuration also accepts SQLAlchemy's driver-qualified URL.
        # Validate before the background pool logger can echo a malformed DSN.
        if not isinstance(database_url, str) or not database_url.strip():
            raise ValueError("POSTGRES_CONNECTION_CONFIGURATION_INVALID")
        normalized = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        try:
            conninfo_to_dict(normalized)
        except (PostgresError, ValueError, TypeError):
            raise ValueError("POSTGRES_CONNECTION_CONFIGURATION_INVALID") from None
        self.pool = ConnectionPool(
            conninfo=normalized,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"autocommit": False},
            reset=reset_connection,
        )

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self, tenant_id: str | UUID, subject_id: str | UUID) -> Iterator[Connection[Any]]:
        tenant = validated_uuid(tenant_id)
        subject = validated_uuid(subject_id)
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(subject),))
                yield connection

    @contextmanager
    def public_transaction(self) -> Iterator[Connection[Any]]:
        with self.pool.connection() as connection:
            with connection.transaction():
                yield connection

    @contextmanager
    def read_transaction(self, tenant_id: str | UUID, subject_id: str | UUID) -> Iterator[Connection[Any]]:
        """One owner-scoped immutable read snapshot, without changing write callers."""
        tenant, subject = validated_uuid(tenant_id), validated_uuid(subject_id)
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(subject),))
                yield connection

    def healthy(self) -> bool:
        try:
            with self.public_transaction() as connection:
                row = connection.execute("SELECT 1").fetchone()
                return row is not None and int(row[0]) == 1
        except Exception:
            return False
