from contextlib import contextmanager
from uuid import uuid4

from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository


class _Connection:
    def __init__(self):
        self.active = False
        self.local_settings = {}
        self.statements = []

    def execute(self, statement, parameters=()):
        assert self.active
        self.statements.append(statement)
        if statement.startswith("SET LOCAL "):
            self.local_settings[statement.split("=", 1)[0]] = statement.split("=", 1)[1]
        else:
            assert self.local_settings == {
                "SET LOCAL statement_timeout": "'2s'",
                "SET LOCAL lock_timeout": "'1s'",
            }
        return self

    def fetchone(self):
        return None


class _Pool:
    def __init__(self):
        self.connection = _Connection()

    @contextmanager
    def transaction(self, tenant, user):
        assert not self.connection.active and not self.connection.local_settings
        self.connection.active = True
        try:
            yield self.connection
        finally:
            self.connection.local_settings.clear()
            self.connection.active = False


def test_cache_get_installs_only_transaction_local_statement_and_lock_timeouts():
    pool = _Pool()
    repository = PostgresCoinglassContextRepository(pool)
    for _ in range(2):
        assert repository.get(uuid4(), uuid4(), uuid4(), "1h", request=True)["status"] == "NO_CONFIG"
        assert not pool.connection.active and not pool.connection.local_settings
    assert pool.connection.statements[:2] == [
        "SET LOCAL statement_timeout='2s'",
        "SET LOCAL lock_timeout='1s'",
    ]
    assert pool.connection.statements[3:5] == pool.connection.statements[:2]
