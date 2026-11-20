"""Private research is explicit consent, never a default or refresh escalation."""
import pytest
from pydantic import ValidationError

from apps.api.formal_app import McpTokenCreateRequest
from gatescope.adapters.postgres_mcp import _scopes as pat_scopes
from gatescope.adapters.postgres_oauth import (
    OAUTH_DEFAULT_SCOPES, OAUTH_SCOPES, OAuthError, _approved_scopes, _scopes,
)


def test_registered_capability_is_not_default_authorization_or_consent():
    assert "research:read" in _scopes(None)
    assert _scopes(None, OAUTH_SCOPES, default=OAUTH_DEFAULT_SCOPES) == OAUTH_DEFAULT_SCOPES
    assert _approved_scopes(OAUTH_SCOPES, None) == OAUTH_DEFAULT_SCOPES
    assert _approved_scopes(OAUTH_SCOPES, list(OAUTH_SCOPES)) == OAUTH_SCOPES
    assert _approved_scopes(OAUTH_SCOPES, ["mcp:connect", "research:read"]) == ("mcp:connect", "research:read")


def test_paper_permissions_require_new_explicit_consent_and_are_not_research():
    assert len(OAUTH_SCOPES) == 6
    assert {"paper:read", "paper:trade"} <= set(_scopes(None))
    assert not {"paper:read", "paper:trade", "research:read"} & set(_approved_scopes(OAUTH_SCOPES, None))
    selected = ["mcp:connect", "paper:read", "paper:trade"]
    assert _approved_scopes(OAUTH_SCOPES, selected) == tuple(selected)
    assert "research:read" not in _approved_scopes(OAUTH_SCOPES, selected)
    assert _approved_scopes(OAUTH_SCOPES, ["mcp:connect", "paper:read"]) == ("mcp:connect", "paper:read")
    assert pat_scopes(selected) == tuple(sorted(selected))


@pytest.mark.parametrize("selected", [["mcp:connect", "paper:trade"], ["mcp:connect", "research:read", "paper:trade"]])
def test_paper_trade_without_paper_read_is_rejected_at_every_scope_boundary(selected):
    with pytest.raises(OAuthError, match="paper:read"):
        _scopes(" ".join(selected))
    with pytest.raises(OAuthError, match="paper:read"):
        _approved_scopes(OAUTH_SCOPES, selected)
    with pytest.raises(ValueError, match="paper:read"):
        pat_scopes(selected)


def test_existing_read_connections_cannot_refresh_into_paper_trading():
    for previous in [OAUTH_DEFAULT_SCOPES, (*OAUTH_DEFAULT_SCOPES, "research:read"), ("mcp:connect", "paper:read")]:
        with pytest.raises(OAuthError):
            _scopes("mcp:connect paper:read paper:trade", previous)


@pytest.mark.parametrize("selected", [[], (), "research:read", ["research:read"], ["mcp:connect", {}],
    ["mcp:connect", "alerts:read"], ["mcp:connect", "trade:write"], ["mcp:connect", "mcp:connect"]])
def test_invalid_or_non_read_research_selection_fails_closed(selected):
    with pytest.raises(OAuthError) as error:
        _approved_scopes(OAUTH_SCOPES, selected)
    assert error.value.error == "invalid_scope"


def test_pending_request_client_ceiling_and_old_refresh_cannot_gain_research():
    with pytest.raises(OAuthError):
        _approved_scopes(OAUTH_DEFAULT_SCOPES, list(OAUTH_SCOPES))
    with pytest.raises(OAuthError):
        _scopes(" ".join(OAUTH_SCOPES), OAUTH_DEFAULT_SCOPES)
    assert _scopes(None, OAUTH_DEFAULT_SCOPES) == OAUTH_DEFAULT_SCOPES
    assert _scopes(None, OAUTH_SCOPES) == OAUTH_SCOPES  # Already granted refresh can retain it.
    assert _scopes("mcp:connect", OAUTH_SCOPES) == ("mcp:connect",)
    with pytest.raises(OAuthError):
        _scopes("mcp:connect research:read", ("mcp:connect",))


def test_unsupported_persisted_scope_is_rejected_even_when_in_client_or_grant():
    with pytest.raises(OAuthError):
        _scopes(None, ("mcp:connect", "alerts:read"))


def test_manual_token_default_unchanged_and_explicit_research_accepted():
    value = {"label": "Research", "mfa_code": "123456"}
    assert McpTokenCreateRequest(**value).scopes == list(OAUTH_DEFAULT_SCOPES)
    selected = [*OAUTH_SCOPES, "alerts:read"]
    assert McpTokenCreateRequest(**value, scopes=selected).scopes == selected
    assert "research:read" in pat_scopes(selected)
    for scopes in [["research:read"], ["mcp:connect", "orders:write"], [*selected, "research:read"]]:
        with pytest.raises(ValidationError):
            McpTokenCreateRequest(**value, scopes=scopes)
