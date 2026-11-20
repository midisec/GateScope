"""Public aggregate business heartbeats; no account IDs, payloads or raw errors."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from psycopg.types.json import Jsonb


WORKLOADS = {"paper-order-worker", "paper-legacy-worker", "shadow-worker"}
COUNTERS = {"targets", "protected", "attempted", "fills", "closed_orders", "unavailable", "failed",
            "tenants", "owners", "captured", "closed", "budget_exhausted"}


def paper_execution_health(row, now):
    """Safe account/MCP projection: never exposes cross-user workload counters."""
    if row is None:
        return {"state": "UNAVAILABLE", "heartbeat_at": None, "last_success_at": None, "age_seconds": None}
    running, heartbeat, last_success, error = row
    try:
        success = datetime.fromisoformat(last_success) if isinstance(last_success, str) else None
        if success is not None and success.tzinfo is None:
            success = None
        age = (now-heartbeat).total_seconds()
        success_age = (now-success).total_seconds() if success is not None else None
    except (ValueError, TypeError):
        return {"state": "UNAVAILABLE", "heartbeat_at": None, "last_success_at": None, "age_seconds": None}
    safe_error = error if error in {"PAPER_ORDER_PARTIAL_FAILURE", "PAPER_ORDER_MARKET_UNAVAILABLE", "PAPER_ORDER_CYCLE_FAILED"} else None
    state = "STOPPED" if not running else "STALE" if not -2 <= age <= 45 else "DEGRADED" if safe_error else (
        "HEALTHY" if success_age is not None and -2 <= success_age <= 45 else "NO_RECENT_SUCCESS")
    return {"state": state, "heartbeat_at": heartbeat.astimezone(timezone.utc).isoformat(),
            "last_success_at": success.astimezone(timezone.utc).isoformat() if success else None,
            "age_seconds": round(max(0., age), 1), "safe_error_code": safe_error,
            "freshness_limit_seconds": 45, "not_a_hard_realtime_guarantee": True}


class PostgresWorkerHealth:
    def __init__(self, pool):
        self.pool = pool

    def record(self, workload, worker_id, *, running, healthy=False, safe_error=None, counters=None, elapsed=0.0):
        if workload not in WORKLOADS or not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 80:
            raise ValueError("INVALID_WORKER_HEARTBEAT")
        if not isinstance(elapsed, (float, int)) or not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("INVALID_WORKER_DURATION")
        if safe_error is not None and safe_error not in {
            "PAPER_ORDER_PARTIAL_FAILURE", "PAPER_ORDER_MARKET_UNAVAILABLE", "PAPER_ORDER_CYCLE_FAILED",
            "PAPER_LEGACY_PARTIAL_FAILURE", "PAPER_LEGACY_CYCLE_FAILED", "SHADOW_PARTIAL_FAILURE", "SHADOW_CYCLE_FAILED",
        }:
            raise ValueError("INVALID_WORKER_ERROR_CODE")
        counts = {key: value for key, value in (counters or {}).items()
                  if key in COUNTERS and isinstance(value, (int, bool)) and 0 <= value <= 10**9}
        payload = {"safe_error_code": safe_error, "cycle_elapsed_seconds": round(elapsed, 3),
                   "cycle_status": "STOPPED" if not running else "HEALTHY" if healthy else "DEGRADED",
                   "counters": counts}
        with self.pool.public_transaction() as connection:
            connection.execute("SET LOCAL statement_timeout='2s'")
            connection.execute("SET LOCAL lock_timeout='1s'")
            connection.execute("""INSERT INTO worker_heartbeats(workload,worker_id,running,heartbeat_at,status_json)
              VALUES(%s,%s,%s,now(),%s::jsonb || jsonb_build_object('last_success_at',
                CASE WHEN %s THEN to_jsonb(now()) ELSE 'null'::jsonb END))
              ON CONFLICT(workload) DO UPDATE SET worker_id=EXCLUDED.worker_id,running=EXCLUDED.running,
                heartbeat_at=EXCLUDED.heartbeat_at,status_json=EXCLUDED.status_json || jsonb_build_object(
                  'last_success_at',CASE WHEN %s THEN EXCLUDED.status_json->'last_success_at'
                    ELSE worker_heartbeats.status_json->'last_success_at' END)""",
                (workload, worker_id, running, Jsonb(payload), bool(healthy and running), bool(healthy and running)))
