"""Small public-workload status projection; never expose paths or configuration."""
from datetime import datetime, timezone

STATES = frozenset({"RUNNING", "PAUSED_LOW_DISK", "PAUSED_DISABLED", "PAUSED_STORAGE_UNAVAILABLE"})


def replay_control(pool, *, now=None):
    unknown = {"state": "UNKNOWN", "checked_at": None}
    try:
        with pool.public_transaction() as conn:
            conn.execute("SET LOCAL statement_timeout='2s'")
            row = conn.execute("""SELECT heartbeat_at,running,status_json->>'history_state'
                FROM worker_heartbeats WHERE workload='strategy-worker'""").fetchone()
        current = now or datetime.now(timezone.utc)
        if not row or not row[1] or row[2] not in STATES or not isinstance(row[0], datetime):
            return unknown
        age = (current - row[0]).total_seconds()
        if not -5 <= age <= 90:
            return unknown
        return {"state": row[2], "checked_at": row[0].isoformat()}
    except Exception:
        # An auxiliary status read must not hide already-read history results.
        return unknown
