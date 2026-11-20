from __future__ import annotations

from pathlib import Path

import pytest

from gatescope.config import ConfigurationError, RuntimeSettings, strict_bool


def test_phase0_defaults_fail_closed(monkeypatch, tmp_path: Path) -> None:
    for name in [
        "LIVE_TRADING_ENABLED", "GATESCOPE_EXECUTION_ENVIRONMENT", "GATESCOPE_DB_PATH",
        "GATESCOPE_PUBLIC_ORIGIN", "RENDER_EXTERNAL_URL", "GATESCOPE_COOKIE_SECURE",
        "GATESCOPE_BOOTSTRAP_USERNAME", "GATESCOPE_BOOTSTRAP_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)
    settings = RuntimeSettings.from_env(tmp_path)
    assert settings.live_trading_enabled is False
    assert settings.execution_environment.value == "PAPER"
    assert settings.start_workers is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_live_flag_cannot_enable_adapter(monkeypatch, tmp_path: Path, value: str) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", value)
    with pytest.raises(ConfigurationError, match="unavailable"):
        RuntimeSettings.from_env(tmp_path)


def test_fuzzy_boolean_rejected(monkeypatch) -> None:
    monkeypatch.setenv("EXAMPLE_FLAG", "truthy-ish")
    with pytest.raises(ConfigurationError):
        strict_bool("EXAMPLE_FLAG", False)


def test_https_requires_secure_cookie(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATESCOPE_PUBLIC_ORIGIN", "https://example.invalid")
    monkeypatch.setenv("GATESCOPE_COOKIE_SECURE", "false")
    with pytest.raises(ConfigurationError, match="secure cookies"):
        RuntimeSettings.from_env(tmp_path)


def test_web_api_refuses_embedded_workers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GATESCOPE_START_WORKERS", "true")
    with pytest.raises(ConfigurationError, match="dedicated worker"):
        RuntimeSettings.from_env(tmp_path)
