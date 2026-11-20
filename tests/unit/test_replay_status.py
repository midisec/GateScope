from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from gatescope.application.replay_status import replay_control

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class Pool:
    def __init__(self, row):
        self.row = row
        self.queries = []

    @contextmanager
    def public_transaction(self):
        yield self

    def execute(self, sql):
        self.queries.append(sql)
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize("state", ["PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE", "RUNNING"])
def test_replay_state_is_whitelisted_fresh_and_read_only(state):
    pool = Pool((NOW, True, state))
    assert replay_control(pool, now=NOW) == {"state": state, "checked_at": NOW.isoformat()}
    assert all(sql.startswith(("SET LOCAL", "SELECT")) for sql in pool.queries)
    assert "status_json->>'history_state'" in pool.queries[-1]


@pytest.mark.parametrize("row", [None, (NOW, False, "PAUSED_LOW_DISK"),
    (NOW - timedelta(seconds=91), True, "PAUSED_LOW_DISK"),
    (NOW + timedelta(seconds=6), True, "PAUSED_LOW_DISK"),
    (NOW, True, "private-marker"), ("invalid-time", True, "RUNNING")])
def test_old_missing_or_private_worker_status_is_not_reported_as_current(row):
    assert replay_control(Pool(row), now=NOW) == {"state": "UNKNOWN", "checked_at": None}


def test_auxiliary_status_failure_does_not_expose_exception_or_break_progress():
    assert replay_control(None, now=NOW) == {"state": "UNKNOWN", "checked_at": None}
