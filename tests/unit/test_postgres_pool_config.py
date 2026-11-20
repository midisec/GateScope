from unittest.mock import Mock

import pytest

from gatescope.adapters import postgres


def test_driver_qualified_url_is_normalized_before_pool_start(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(postgres, "ConnectionPool", factory)
    postgres.PostgresPool("postgresql+psycopg://fixture:fixture-secret@localhost/test")
    assert factory.call_args.kwargs["conninfo"] == "postgresql://fixture:fixture-secret@localhost/test"
    assert factory.call_args.kwargs["open"] is False


def test_invalid_connection_string_never_reaches_background_error_logger(monkeypatch, caplog):
    factory = Mock()
    monkeypatch.setattr(postgres, "ConnectionPool", factory)
    secret = "fixture-do-not-log"
    with pytest.raises(ValueError, match="POSTGRES_CONNECTION_CONFIGURATION_INVALID") as error:
        postgres.PostgresPool("bad-prefix-" + secret)
    assert secret not in str(error.value) and secret not in caplog.text
    factory.assert_not_called()
