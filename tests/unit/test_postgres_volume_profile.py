from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest

from gatescope.adapters.postgres_volume_profile import PostgresVolumeProfileRepository
from gatescope.domain.volume_profile import plan_volume_profile


@pytest.mark.parametrize("end_delta", [60, 50_000*60+30])
def test_repository_exact_read_only_indexed_canonical_query_and_no_unit_invention(end_delta):
    identifier = UUID("e3f9e4c1-f433-4581-ab69-666cbb896923")
    opening = 1_800_000_000
    asof = opening+end_delta+3600
    calls = []
    class Connection:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            if "FROM instruments" in sql:
                return Mock(fetchone=lambda: (identifier, "GATE", "FUTURES_USDT", "BTC_USDT"))
            if "FROM bars" in sql:
                row = (Decimal(opening), Decimal(opening+60), 100, 124, 100, 124, 240, True, 2,
                       Decimal(opening+60), Decimal(opening+60), "GATE", "FUTURES_USDT", identifier, "1m", ["REST_POLLING"])
                return Mock(fetchall=lambda: [row])
            return Mock()
    class Pool:
        @contextmanager
        def public_transaction(self):
            yield Connection()
    output = PostgresVolumeProfileRepository(Pool()).get(identifier,
        plan_volume_profile("1h", opening, opening+end_delta, asof=asof), asof=asof)
    assert output["used_bars"] == 1
    assert [sql for sql, _ in calls][:3] == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout='5s'", "SET LOCAL lock_timeout='1s'"]
    sql, params = next((sql, params) for sql, params in calls if "FROM bars" in sql)
    assert "DISTINCT ON (open_time)" in sql and "ORDER BY open_time DESC,revision DESC" in sql
    assert "venue=%s AND market=%s" in sql
    assert "available_at<=to_timestamp(%s) AND created_at<=to_timestamp(%s)" in sql
    assert "is_final=true" not in sql and "::bigint" not in sql
    assert params == (identifier, "1m", "GATE", "FUTURES_USDT", opening, opening+(end_delta//60)*60, asof, asof, 50_001)
    assert not any(word in sql.upper() for sql, _ in calls for word in ("INSERT ", "UPDATE ", "DELETE "))


def test_missing_instrument_does_not_query_any_bars():
    calls = []
    class Pool:
        @contextmanager
        def public_transaction(self):
            connection = Mock()
            def execute(sql, params=None):
                calls.append(sql)
                return Mock(fetchone=lambda: None)
            connection.execute = execute
            yield connection
    with pytest.raises(LookupError):
        PostgresVolumeProfileRepository(Pool()).get(UUID(int=1),
            plan_volume_profile("1h", 0, 60, asof=3600), asof=3600)
    assert not any("FROM bars" in sql for sql in calls)
