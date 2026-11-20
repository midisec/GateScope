from __future__ import annotations

import pytest

from apps.executor.main import main


def test_executor_rejects_live_trading_before_loading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("GATESCOPE_EXECUTOR_WORKLOAD", "gate-testnet-execution")
    monkeypatch.setenv("GATESCOPE_EXECUTION_ENVIRONMENT", "TESTNET")
    with pytest.raises(RuntimeError, match="LIVE trading remains hard-disabled"):
        main()


def test_testnet_order_workload_requires_exact_testnet_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("GATESCOPE_EXECUTOR_WORKLOAD", "gate-testnet-execution")
    monkeypatch.setenv("GATESCOPE_EXECUTION_ENVIRONMENT", "PAPER")
    with pytest.raises(RuntimeError, match="requires explicit TESTNET"):
        main()


def test_unknown_executor_workload_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("GATESCOPE_EXECUTOR_WORKLOAD", "live-execution")
    monkeypatch.setenv("GATESCOPE_EXECUTION_ENVIRONMENT", "LIVE")
    with pytest.raises(RuntimeError, match="workload is not allowlisted"):
        main()
