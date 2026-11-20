import psycopg
from psycopg.conninfo import make_conninfo

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository
from gatescope.application.storage_pressure import StoragePressure


def test_market_capacity_pause_and_resume_are_persisted_under_market_role(migrated_database):
    pool = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_market"), max_size=1)
    pool.open()
    try:
        repository = PostgresMarketIngestRepository(pool)
        for state, free, pages in [("PAUSED_LOW_DISK", 100, 0), ("RUNNING", 6000, 2)]:
            pressure = StoragePressure(state, free, 1, "LOCAL_DATABASE_EXPLICIT_PATHS", 3000, 5000)
            repository.heartbeat("market-storage-test", running=True, status={
                "history_state": "unvalidated-label", "history_pages": pages,
                "history_storage": {**pressure.heartbeat(), "paths": ["private-marker"],
                                    "database_url": "private-marker"}, "inserted_bars": 3})
            with psycopg.connect(migrated_database) as connection:
                running, payload = connection.execute("SELECT running,status_json FROM worker_heartbeats WHERE workload='market-worker'").fetchone()
            assert running is True
            assert payload["history_state"] == state
            assert payload["history_storage"] == pressure.heartbeat()
            assert payload["history_pages"] == pages and payload["inserted_bars"] == 3
            assert "private-marker" not in repr(payload) and "unvalidated-label" not in repr(payload)
        repository.heartbeat("market-storage-test", running=True,
                             status={"history_state": "RUNNING", "history_storage": {"state": "private-marker"}})
        with psycopg.connect(migrated_database) as connection:
            payload = connection.execute("SELECT status_json FROM worker_heartbeats WHERE workload='market-worker'").fetchone()[0]
        assert "history_state" not in payload and "history_storage" not in payload
        assert "private-marker" not in repr(payload)
    finally:
        pool.close()
