from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.formal_app import (
    ExchangeAccountCreateRequest,
    ExchangeCredentialRotateRequest,
    ExchangeTestnetArmRequest,
    TestnetDrillCreateRequest as DrillCreateRequest,
)
from scripts.generate_formal_openapi import generate


def test_exchange_request_is_testnet_only_and_forbids_extra_fields() -> None:
    valid = {
        "alias": "Gate testnet",
        "venue": "GATE",
        "environment": "TESTNET",
        "market_type": "FUTURES_USDT",
        "api_key": "test-key-123456789",
        "api_secret": "test-secret-123456789",
        "acknowledge_no_withdrawal": True,
    }
    assert ExchangeAccountCreateRequest.model_validate(valid).environment == "TESTNET"
    with pytest.raises(ValidationError):
        ExchangeAccountCreateRequest.model_validate({**valid, "environment": "LIVE"})
    with pytest.raises(ValidationError):
        ExchangeAccountCreateRequest.model_validate({**valid, "withdrawal_permission": True})
    with pytest.raises(ValidationError):
        ExchangeCredentialRotateRequest.model_validate({
            "api_key": valid["api_key"],
            "api_secret": valid["api_secret"],
            "acknowledge_no_withdrawal": False,
        })


def test_exchange_routes_are_session_csrf_and_owner_recent_auth_bound() -> None:
    contract = generate()
    paths = {
        ("get", "/api/v2/exchange-accounts"),
        ("post", "/api/v2/exchange-accounts"),
        ("get", "/api/v2/exchange-accounts/{account_id}"),
        ("post", "/api/v2/exchange-accounts/{account_id}/credentials:rotate"),
        ("post", "/api/v2/exchange-accounts/{account_id}/validate"),
        ("delete", "/api/v2/exchange-accounts/{account_id}"),
        ("post", "/api/v2/exchange-accounts/{account_id}/testnet:arm"),
        ("post", "/api/v2/exchange-accounts/{account_id}/testnet:disarm"),
        ("get", "/api/v2/testnet-drills"),
        ("get", "/api/v2/execution-certifications"),
        ("post", "/api/v2/testnet-drills"),
        ("get", "/api/v2/testnet-drills/{public_drill_id}"),
        ("get", "/api/v2/testnet-drills/{public_drill_id}/timeline"),
    }
    for method, path in paths:
        operation = contract["paths"][path][method]
        assert operation["security"] == [{"SessionCookie": []}]
        assert "role:owner" in operation["x-rbac-actions"]
        if method not in {"get", "head"}:
            assert operation["x-csrf-header"] == "X-CSRF-Token"
            assert "recent-auth" in operation["x-rbac-actions"]


def test_testnet_arming_and_drill_contracts_are_fail_closed() -> None:
    arm = {
        "mfa_code": "123456",
        "acknowledge_testnet_only": True,
        "acknowledge_ip_whitelist": True,
        "acknowledge_no_withdrawal": True,
        "acknowledge_exclusive_account": True,
    }
    assert ExchangeTestnetArmRequest.model_validate(arm).acknowledge_testnet_only is True
    with pytest.raises(ValidationError):
        ExchangeTestnetArmRequest.model_validate({**arm, "acknowledge_no_withdrawal": False})
    with pytest.raises(ValidationError):
        ExchangeTestnetArmRequest.model_validate({**arm, "environment": "LIVE"})

    drill = DrillCreateRequest.model_validate({
        "paper_public_trade_id": "paper_00000000000000000000000000",
        "exchange_account_id": "10000000-0000-0000-0000-000000000001",
        "mfa_code": "123456",
    })
    assert not hasattr(drill, "quantity")
    for forbidden in ("quantity", "side", "stop_price", "take_profit_price", "environment"):
        with pytest.raises(ValidationError):
            DrillCreateRequest.model_validate({
                **drill.model_dump(), forbidden: "1",
            })
