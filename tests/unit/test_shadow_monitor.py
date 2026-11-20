from datetime import datetime, timezone
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_shadow import instant, ledger_state
from gatescope.application.shadow_monitor import ShadowTradeMonitor


def test_engine_states_preserve_unavailable_and_censored_denominators():
    assert ledger_state({"state": "TARGET"}) == "CLOSED"
    assert ledger_state({"state": "STOP"}) == "CLOSED"
    assert ledger_state({"state": "TIMEOUT"}) == "CLOSED"
    assert ledger_state({"state": "CENSORED"}) == "EXPIRED"
    assert ledger_state({"state": "UNAVAILABLE"}) == "SKIPPED"
    assert ledger_state({"state": "WAITING_CONFIRMATION"}) == "PENDING"
    assert ledger_state({"state": "WAITING_ENTRY"}) == "PENDING"
    assert instant(None) is None and instant(float("nan")) is None


def test_monitor_failure_isolated_and_time_and_work_are_bounded():
    first, second = (uuid4(), uuid4()), (uuid4(), uuid4())
    now = datetime.now(timezone.utc)

    class Store:
        def due_owners(self, limit):
            assert limit == 100
            return [first, second]

        def capture(self, tenant, owner, *, now):
            if (tenant, owner) == first:
                raise RuntimeError("private details must never reach the result")
            return 2

        def advance(self, tenant, owner, *, now):
            assert (tenant, owner) == second
            return {"attempted": 4, "closed": 1}

    result = ShadowTradeMonitor(Store()).run_once(now=now, owner_limit=10000)
    assert (result.owners, result.captured, result.attempted, result.closed, result.failed) == (2, 2, 4, 1, 1)
    with pytest.raises(ValueError, match="timezone"):
        ShadowTradeMonitor(Store()).run_once(now=datetime(2026, 1, 1))
