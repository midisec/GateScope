from contextlib import contextmanager
import json

import pytest

from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository
from gatescope.application.storage_pressure import StoragePressure


class Pool:
    def __init__(self):
        self.calls = []

    @contextmanager
    def public_transaction(self):
        yield self

    def execute(self, query, params):
        self.calls.append((query, params))


def persist(status):
    pool = Pool()
    PostgresMarketIngestRepository(pool).heartbeat("market-worker", running=True, status=status)
    params = pool.calls[-1][1]
    assert params[1] is True
    return json.loads(params[3])


@pytest.mark.parametrize("state", ["RUNNING", "PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE"])
def test_market_heartbeat_persists_validated_capacity_report_without_secrets(state):
    pressure = StoragePressure(state, 900, 1, "LOCAL_DATABASE_EXPLICIT_PATHS", 3000, 5000)
    result = persist({"history_state": "wrong-top-level", "history_storage": {
        **pressure.heartbeat(), "paths": ["private-marker"], "database_url": "private-marker",
        "error": "private-marker"}, "history_pages": 2, "inserted_bars": 1})
    assert result["history_state"] == state
    assert result["history_storage"] == pressure.heartbeat()
    assert result["history_pages"] == 2 and result["inserted_bars"] == 1
    assert "private-marker" not in repr(result) and "wrong-top-level" not in repr(result)


@pytest.mark.parametrize("storage", [None, "private-marker", {}, {"state": "private-marker"},
                                   {"state": ["RUNNING"]}, {"state": "RUNNING", "database_scope": []}])
def test_invalid_or_missing_storage_never_claims_running_or_persists_untrusted_label(storage):
    result = persist({"history_state": "RUNNING", "history_storage": storage})
    assert "history_state" not in result and "history_storage" not in result
    assert "private-marker" not in repr(result)


def test_storage_scope_and_measurements_use_shared_safe_whitelist():
    result = persist({"history_storage": {"state": "PAUSED_STORAGE_UNAVAILABLE",
        "database_scope": "private-marker", "min_free_bytes": -1, "checked_paths": True,
        "pause_below_bytes": "private-marker", "resume_above_bytes": 5000}})
    assert result["history_storage"] == {
        "state": "PAUSED_STORAGE_UNAVAILABLE", "database_scope": "UNVERIFIED", "min_free_bytes": None,
        "checked_paths": None, "pause_below_bytes": None, "resume_above_bytes": 5000}
    assert "private-marker" not in repr(result)


@pytest.mark.parametrize("pages", [None, -1, True, "private-marker", "2", 2.5, [], {}])
def test_history_page_count_rejects_non_counts_without_crashing_or_leaking(pages):
    result = persist({"history_pages": pages})
    assert result["history_pages"] == 0
    assert "private-marker" not in repr(result)
