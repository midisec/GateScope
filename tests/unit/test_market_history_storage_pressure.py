from threading import Event
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from apps.market_worker.main import _catalog_provider_ready, _run_history_maintenance
from gatescope.application.storage_pressure import StoragePressure


@pytest.mark.parametrize("state", ["PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE"])
def test_market_backfill_pauses_without_calling_bulk_history(state):
    pressure = StoragePressure(state, 900, 1, "LOCAL_DATABASE_EXPLICIT_PATHS", 3000, 5000)
    sync, guard = Mock(), Mock()
    guard.check.return_value = pressure
    result = _run_history_maintenance(sync, guard, Event())
    sync.run_cycle.assert_not_called()
    assert result["history_pages"] == 0
    assert result["history_state"] == state
    assert result["history_storage"] == pressure.heartbeat()
    assert "path" not in result["history_storage"]


def test_market_backfill_runs_once_with_live_false_and_preserves_provider_status():
    pressure = StoragePressure("RUNNING", 6000, 1, "LOCAL_DATABASE_EXPLICIT_PATHS", 3000, 5000)
    sync, guard, stopped = Mock(), Mock(), Event()
    guard.check.return_value = pressure
    sync.run_cycle.return_value = {"history_pages": 2, "rate_limited": True}
    result = _run_history_maintenance(sync, guard, stopped)
    sync.run_cycle.assert_called_once_with(live=False, stopped=stopped.is_set)
    assert result["history_pages"] == 2 and result["rate_limited"] is True
    assert result["history_state"] == "RUNNING"


@pytest.mark.parametrize("delay,ready", [(None, True), (-60, True), (60, False)])
def test_catalog_respects_shared_provider_backoff_even_when_history_is_disabled(delay, ready):
    repository = Mock()
    repository.provider_retry_at.return_value = (
        None if delay is None else datetime.now(timezone.utc) + timedelta(seconds=delay)
    )
    assert _catalog_provider_ready(repository) is ready
    repository.provider_retry_at.assert_called_once_with()


def test_catalog_does_not_fetch_when_provider_backoff_cannot_be_verified():
    repository = Mock()
    repository.provider_retry_at.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError):
        _catalog_provider_ready(repository)
