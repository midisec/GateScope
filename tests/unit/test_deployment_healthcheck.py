from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/gatescope_healthcheck.sh"


def runner(tmp_path, monkeypatch, *, database=True, deployment=True):
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls"
    systemctl = """#!/bin/sh
echo "$*" >> "$CALLS"
if [ "${2-}" = "--quiet" ]; then
    command=$1
    shift 2
    set -- "$command" "$@"
fi
case "$1:$2" in
  is-enabled:gatescope-api.service) exit "$DEPLOYMENT_DISABLED" ;;
  is-enabled:gatescope-executor-testnet.service) exit 1 ;;
  is-enabled:*) exit 0 ;;
  is-active:postgresql@14-main.service) exit 1 ;;
  is-active:*) exit 0 ;;
esac
exit 0
"""
    for name, body in {
        "systemctl": systemctl,
        "pg_isready": '#!/bin/sh\nexit "$DB_UNAVAILABLE"\n',
        "curl": '#!/bin/sh\nprintf \'%s\\n\' \'{"status":"ready"}\'\n',
        "runuser": '#!/bin/sh\ncat >/dev/null\nprintf \'%s\\n\' "$BUSINESS_STATUS"\nexit "${BUSINESS_FAILURE:-0}"\n',
        "df": '#!/bin/sh\nprintf \'Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 100 20 80 %s%% /\\n\' "${DISK_USED:-20}"\n',
    }.items():
        path = binary / name
        path.write_text(body)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("DB_UNAVAILABLE", "0" if database else "1")
    monkeypatch.setenv("DEPLOYMENT_DISABLED", "0" if deployment else "1")
    monkeypatch.setenv("GATESCOPE_HEALTH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BUSINESS_STATUS", "paper-legacy-worker|HEALTHY\npaper-order-worker|HEALTHY\nshadow-worker|HEALTHY\npaper-protection-age|HEALTHY")
    return lambda: subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, timeout=5), calls


def test_disabled_deployment_is_not_restarted(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch, deployment=False)
    assert run().returncode == 0
    assert "restart" not in calls.read_text()
    assert "is-active" not in calls.read_text()


def test_disabled_executor_stays_disabled(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch)
    assert run().returncode == 0
    assert "is-active --quiet gatescope-executor-testnet.service" not in calls.read_text()
    assert "restart" not in calls.read_text()


def test_database_recovery_precedes_workload_recovery(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch, database=False)
    for _ in range(2):
        assert run().returncode == 1
    assert "start postgresql@14-main.service" not in calls.read_text()
    assert run().returncode == 1
    assert "start postgresql@14-main.service" in calls.read_text()
    assert "restart gatescope" not in calls.read_text()


def test_business_failure_is_reported_without_restart_storm(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch)
    monkeypatch.setenv("BUSINESS_STATUS", "paper-order-worker|DEGRADED")
    result = run()
    assert result.returncode == 1 and "business progress" in result.stdout
    assert "restart" not in calls.read_text()


def test_business_probe_failure_does_not_leak_or_restart(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch)
    monkeypatch.setenv("BUSINESS_FAILURE", "1")
    result = run()
    assert result.returncode == 1 and "business health unavailable" in result.stdout
    assert "restart" not in calls.read_text()


def test_capacity_warning_never_deletes_records_or_restarts(tmp_path, monkeypatch):
    run, calls = runner(tmp_path, monkeypatch)
    monkeypatch.setenv("DISK_USED", "87")
    result = run()
    assert result.returncode == 0 and "disk 87%" in result.stdout
    assert "restart" not in calls.read_text()
