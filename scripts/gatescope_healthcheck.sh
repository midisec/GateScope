#!/bin/sh
set -eu

readonly STATE_DIR=${GATESCOPE_HEALTH_STATE_DIR:-/run/gatescope-healthcheck}
readonly LOCAL_READY_URL=${GATESCOPE_HEALTH_LOCAL_URL:-http://127.0.0.1:17758/health/ready}
readonly PUBLIC_READY_URL=${GATESCOPE_HEALTH_PUBLIC_URL:-https://43.108.44.74/health/ready}
readonly FAILURE_THRESHOLD=3
readonly DATABASE_UNIT=postgresql@14-main.service
readonly DISK_PATH=${GATESCOPE_HEALTH_DISK_PATH:-/home/ubuntu/workspace/crypto_gate_trade}

if [ -e /run/gatescope-maintenance ] || ! systemctl is-enabled --quiet gatescope-api.service; then
    echo "gatescope-healthcheck: maintenance or disabled deployment; skipping"
    exit 0
fi

mkdir -p "$STATE_DIR"

ready() {
    curl --fail --silent --show-error \
        --connect-timeout 3 \
        --max-time 8 \
        --retry 1 \
        --retry-delay 1 \
        --retry-connrefused \
        "$1" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"'
}

clear_failures() {
    rm -f "$STATE_DIR/$1.failures"
}

record_failure() {
    component=$1
    failure_file="$STATE_DIR/$component.failures"
    failures=0
    if [ -r "$failure_file" ]; then
        read -r failures < "$failure_file" || failures=0
    fi
    case "$failures" in
        ''|*[!0-9]*) failures=0 ;;
    esac
    failures=$((failures + 1))
    printf '%s\n' "$failures" > "$failure_file"
    printf '%s\n' "$failures"
}

ensure_workloads() {
    workload_failed=0
    for unit in \
        gatescope-market.service \
        gatescope-strategy.service \
        gatescope-research.service \
        gatescope-paper.service \
        gatescope-notification.service \
        gatescope-mcp.service \
        gatescope-executor-validation.service \
        gatescope-executor-testnet.service
    do
        if systemctl is-enabled --quiet "$unit" && ! systemctl is-active --quiet "$unit"; then
            echo "gatescope-healthcheck: restarting inactive workload $unit"
            systemctl restart "$unit" || workload_failed=1
        fi
    done
    return "$workload_failed"
}

paper_business_health() {
    # PostgreSQL peer authentication, no secrets sourced into this shell and no
    # raw failure/DSN output. Metadata only; never restart a worker on market 429.
    runuser -u postgres -- psql -X -A -t -q -v ON_ERROR_STOP=1 -d gatescope 2>/dev/null <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout='3s';
WITH required(workload,max_age) AS (
  VALUES ('paper-order-worker',45),('paper-legacy-worker',120),('shadow-worker',180)
)
SELECT r.workload || '|' || CASE
  WHEN h.workload IS NULL THEN 'MISSING'
  WHEN NOT h.running THEN 'NOT_RUNNING'
  WHEN h.heartbeat_at<now()-r.max_age*interval '1 second' THEN 'STALE'
  WHEN h.status_json->>'safe_error_code' IS NOT NULL THEN 'DEGRADED'
  WHEN h.status_json->>'last_success_at' IS NULL THEN 'NO_SUCCESS'
  WHEN (h.status_json->>'last_success_at')::timestamptz<now()-r.max_age*interval '1 second' THEN 'NO_RECENT_SUCCESS'
  ELSE 'HEALTHY' END
FROM required r LEFT JOIN worker_heartbeats h USING(workload) ORDER BY r.workload;
SELECT 'paper-protection-age|' || CASE WHEN EXISTS (
  SELECT 1 FROM paper_positions WHERE status='OPEN'
    AND coalesce(last_checked_at,opened_at)<now()-interval '60 seconds'
) THEN 'LAGGING' ELSE 'HEALTHY' END;
COMMIT;
SQL
}

healthy=1

# postgresql.service can be active even when the actual cluster is dead.
if pg_isready -q -h 127.0.0.1 -p 5432; then
    clear_failures database
else
    database_failures=$(record_failure database)
    echo "gatescope-healthcheck: database unavailable ($database_failures/$FAILURE_THRESHOLD)"
    if [ "$database_failures" -ge "$FAILURE_THRESHOLD" ] && ! systemctl is-active --quiet "$DATABASE_UNIT"; then
        systemctl reset-failed "$DATABASE_UNIT" || true
        systemctl start "$DATABASE_UNIT"
    fi
    exit 1
fi

if ! ensure_workloads; then
    healthy=0
fi

if ready "$LOCAL_READY_URL"; then
    clear_failures api
else
    healthy=0
    api_failures=$(record_failure api)
    echo "gatescope-healthcheck: local readiness failed ($api_failures/$FAILURE_THRESHOLD)"
    if [ "$api_failures" -ge "$FAILURE_THRESHOLD" ]; then
        echo "gatescope-healthcheck: restarting gatescope-api after consecutive readiness failures"
        systemctl restart gatescope-api.service
        sleep 4
        if ready "$LOCAL_READY_URL"; then
            clear_failures api
            healthy=1
        fi
    fi
fi

if [ "$healthy" -eq 1 ] && ready "$PUBLIC_READY_URL"; then
    clear_failures edge
elif [ "$healthy" -eq 1 ]; then
    healthy=0
    edge_failures=$(record_failure edge)
    echo "gatescope-healthcheck: public HTTPS readiness failed ($edge_failures/$FAILURE_THRESHOLD)"
    if [ "$edge_failures" -ge "$FAILURE_THRESHOLD" ]; then
        echo "gatescope-healthcheck: validating configuration and restarting nginx"
        if nginx -t; then
            systemctl restart nginx.service
            sleep 3
            if ready "$PUBLIC_READY_URL"; then
                clear_failures edge
                healthy=1
            fi
        fi
    fi
fi

if systemctl is-enabled --quiet gatescope-paper.service; then
    if business_status=$(paper_business_health); then
        # Query result contains only fixed workload/status labels. A blocked
        # protection loop must be visible even if the API and process are alive.
        if printf '%s\n' "$business_status" | grep -Eq '\|(MISSING|NOT_RUNNING|STALE|DEGRADED|NO_SUCCESS|NO_RECENT_SUCCESS|LAGGING)$'; then
            healthy=0
            echo "gatescope-healthcheck: PAPER business progress needs attention (no automatic restart)"
            printf '%s\n' "$business_status"
        elif [ "$(printf '%s\n' "$business_status" | grep -c '|HEALTHY$')" -ne 4 ]; then
            healthy=0
            echo "gatescope-healthcheck: PAPER business health response incomplete"
        fi
    else
        healthy=0
        echo "gatescope-healthcheck: PAPER business health unavailable"
    fi
fi

disk_used=$(df -Pk "$DISK_PATH" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}') || disk_used=
case "$disk_used" in
    ''|*[!0-9]*) echo "gatescope-healthcheck: disk capacity measurement unavailable" ;;
    *) if [ "$disk_used" -ge 85 ]; then
           echo "gatescope-healthcheck: capacity warning: disk ${disk_used}% used; preserve evidence and verify off-host backups before retention changes"
       fi ;;
esac

if [ "$healthy" -ne 1 ]; then
    exit 1
fi

echo "gatescope-healthcheck: all workloads and endpoints are healthy"
