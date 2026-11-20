from __future__ import annotations

import pytest

from gatescope.adapters.postgres_oauth import OAuthError, PostgresOAuthRepository


ORIGIN = "https://43.108.44.74"
RESOURCE = ORIGIN + "/mcp"
ALIAS = "https://tunnel-service.gateway.unified-0.internal.api.openai.org/v1/mcp/tunnel_0123456789abcdef0123456789abcdef"


def test_one_configured_transport_alias_maps_to_same_canonical_resource():
    repo = PostgresOAuthRepository(None, public_origin=ORIGIN, resource_aliases=(ALIAS,))
    assert repo.canonical_resource(ALIAS) == RESOURCE
    assert repo.canonical_resource(RESOURCE) == RESOURCE
    assert repo.resource == RESOURCE and repo.issuer == ORIGIN
    assert repo.resource_aliases == (ALIAS,)


def test_alias_is_disabled_by_default_and_never_inferred_from_environment(monkeypatch):
    monkeypatch.setenv("GATESCOPE_MCP_OAUTH_TUNNEL_RESOURCE", ALIAS)
    repo = PostgresOAuthRepository(None, public_origin=ORIGIN)
    assert repo.canonical_resource(RESOURCE) == RESOURCE
    with pytest.raises(OAuthError) as error:
        repo.canonical_resource(ALIAS)
    assert error.value.error == "invalid_target"


@pytest.mark.parametrize("aliases", [
    (ALIAS, ALIAS), [ALIAS], ALIAS, None, (None,), ("",), (RESOURCE,),
    (ALIAS + "/",), (ALIAS + "/tools",), (ALIAS + "?",), (ALIAS + "?x=1",), (ALIAS + "#",),
    (ALIAS + "#fragment",), (ALIAS + ";params",), (ALIAS + "\n",), (" " + ALIAS,),
    (ALIAS.replace("https://", "http://"),), (ALIAS.replace("https://", "https://user@"),),
    (ALIAS.replace("https://", "https://@"),), (ALIAS.replace(".org/", ".org:443/"),),
    (ALIAS.replace("unified-0", "unified-1"),), (ALIAS.replace(".api.openai.org", ".api.openai.org.evil.example"),),
    (ALIAS.replace("tunnel_0123456789abcdef0123456789abcdef", "tunnel_0123456789ABCDEF0123456789ABCDEF"),),
    (ALIAS[:-1],), (ALIAS + "0",), (ALIAS.replace("/v1/mcp/", "/v1/other/"),),
])
def test_unsafe_or_ambiguous_alias_configuration_fails_closed(aliases):
    with pytest.raises(ValueError, match="OAUTH_TUNNEL_RESOURCE_ALIAS_CONFIGURATION_INVALID"):
        PostgresOAuthRepository(None, public_origin=ORIGIN, resource_aliases=aliases)


@pytest.mark.parametrize("requested", [
    None, "", RESOURCE + "/", ALIAS + "/", ALIAS + "?x=1", ALIAS + "#fragment", ALIAS + ";params",
    ALIAS.replace("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"),
    ALIAS.replace("unified-0", "unified-1"), ALIAS.replace(".org/", ".org:443/"),
    ALIAS.replace("https://", "https://user@"), ALIAS.replace("/v1/mcp/", "/v1/mcp/../mcp/"),
    ALIAS.replace("tunnel_", "tunnel%5f"), ALIAS + "\n", "https://other.example/mcp",
])
def test_only_exact_configured_alias_is_accepted(requested):
    repo = PostgresOAuthRepository(None, public_origin=ORIGIN, resource_aliases=(ALIAS,))
    with pytest.raises(OAuthError) as error:
        repo.canonical_resource(requested)
    assert error.value.error == "invalid_target"
