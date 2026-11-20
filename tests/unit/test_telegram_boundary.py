from __future__ import annotations

import pytest

from apps.api.formal_app import NotificationRouteRequest, _positive_id_set, _telegram_start_update
from gatescope.adapters.postgres_notifications import _valid_token


def test_telegram_start_requires_matching_private_chat() -> None:
    token = "A" * 43
    assert _telegram_start_update({
        "update_id": 9,
        "message": {
            "text": f"/start {token}",
            "from": {"id": 123, "username": "alice"},
            "chat": {"id": 123, "type": "private"},
        },
    }) == (9, 123, 123, "alice", token)

    with pytest.raises(ValueError, match="private"):
        _telegram_start_update({
            "update_id": 10,
            "message": {
                "text": f"/start {token}",
                "from": {"id": 123},
                "chat": {"id": 456, "type": "group"},
            },
        })


def test_link_token_and_allowlist_validation_fail_closed() -> None:
    assert _valid_token("A" * 43) == "A" * 43
    with pytest.raises(ValueError, match="token"):
        _valid_token("A" * 39)
    with pytest.raises(ValueError, match="token"):
        _valid_token("A" * 42 + ".")
    assert _positive_id_set("123, 456") == frozenset({123, 456})
    assert _positive_id_set("") == frozenset()
    with pytest.raises(RuntimeError, match="invalid ID"):
        _positive_id_set("0")


def test_risk_critical_route_cannot_be_silenced() -> None:
    payload = {
        "name": "Risk",
        "channel_id": "10000000-0000-0000-0000-000000000001",
        "event_types": ["RISK_CRITICAL"],
        "bypass_quiet_rules": [{"event_type": "RISK_CRITICAL", "environments": ["PAPER"]}],
    }
    with pytest.raises(ValueError, match="RISK_CRITICAL"):
        NotificationRouteRequest.model_validate(payload)
    payload["bypass_quiet_rules"] = [{"event_type": "RISK_CRITICAL", "environments": []}]
    assert NotificationRouteRequest.model_validate(payload).max_per_minute == 10
