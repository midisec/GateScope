from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.formal_app import (
    PaperAccountCreateRequest,
    ProposalCreateRequest,
    ProposalVersionRequest,
    _telegram_message_update,
)
from scripts.generate_formal_openapi import generate


def test_clients_cannot_submit_trade_terms_in_proposal_or_approval() -> None:
    with pytest.raises(ValidationError):
        ProposalCreateRequest.model_validate({
            "execution_decision_id": "10000000-0000-0000-0000-000000000001",
            "client_request_id": "request-identifier-0001",
            "quantity": "999",
        })
    with pytest.raises(ValidationError):
        ProposalVersionRequest.model_validate({"proposal_version": 1, "stop_price": "1"})


def test_paper_account_policy_rejects_risk_expansion() -> None:
    with pytest.raises(ValidationError):
        PaperAccountCreateRequest.model_validate({
            "name": "unsafe", "initial_equity": "10000", "risk_fraction": "0.006",
        })
    with pytest.raises(ValidationError):
        PaperAccountCreateRequest.model_validate({
            "name": "unsafe", "initial_equity": "10000", "max_notional_fraction": "0.11",
        })


def test_telegram_commands_require_matching_private_chat_and_are_bounded() -> None:
    parsed = _telegram_message_update({
        "update_id": 1,
        "message": {
            "text": "/trade paper_00000000000000000000000000",
            "from": {"id": 123, "username": "alice"},
            "chat": {"id": 123, "type": "private"},
        },
    })
    assert parsed[-1].startswith("/trade paper_")
    with pytest.raises(ValueError, match="private"):
        _telegram_message_update({
            "update_id": 2,
            "message": {
                "text": "/trades",
                "from": {"id": 123},
                "chat": {"id": 999, "type": "group"},
            },
        })


def test_paper_routes_are_authenticated_csrf_classified_and_rbac_classified() -> None:
    contract = generate()
    expected = {
        ("post", "/api/v2/paper-accounts"): "proposal:create",
        ("post", "/api/v2/signals/{signal_id}/execution-decisions"): "proposal:create",
        ("post", "/api/v2/proposals"): "proposal:create",
        ("post", "/api/v2/proposals/{proposal_id}/approve"): "proposal:approve",
        ("post", "/api/v2/trades/{public_trade_id}:reconcile-paper"): "proposal:approve",
    }
    for (method, path), action in expected.items():
        operation = contract["paths"][path][method]
        assert operation["security"] == [{"SessionCookie": []}]
        assert operation["x-csrf-header"] == "X-CSRF-Token"
        assert action in operation["x-rbac-actions"]
