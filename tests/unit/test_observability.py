from __future__ import annotations

from gatescope.observability import redact


def test_recursive_secret_redaction() -> None:
    value = redact(
        {
            "api_key": "synthetic-sensitive-value",
            "nested": {"Authorization": "Bearer synthetic-token-value", "safe": "BTC_USDT"},
            "message": "request failed for https://user:credential@example.invalid/path",
        }
    )
    assert value["api_key"] == "[REDACTED]"
    assert value["nested"]["Authorization"] == "[REDACTED]"
    assert value["nested"]["safe"] == "BTC_USDT"
    assert "credential" not in value["message"]

