from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import anyio
import pytest


ORIGIN = "https://43.108.44.74"
MCP_URL = ORIGIN + "/mcp"
PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
ASM_PATH = "/.well-known/oauth-authorization-server"


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("check_mcp_oauth_discovery")


def mock_discovery(monkeypatch, probe, requests):
    """Use the real request/response types but never create a network transport."""
    state = {"sent": [], "closed": False, "storage_reads": 0, "storage_writes": 0}

    class Provider:
        def __init__(self, *, server_url, client_metadata, storage):
            assert server_url in {MCP_URL, MCP_URL + "/"}
            assert client_metadata.token_endpoint_auth_method == "none"
            assert client_metadata.scope == "mcp:connect market:read analysis:read"
            self.storage = storage

        async def async_auth_flow(self, request):
            assert request.method == "GET"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            assert await self.storage.get_tokens() is None
            assert await self.storage.get_client_info() is None
            state["storage_reads"] += 2
            try:
                for method, url in requests:
                    yield probe.httpx2.Request(method, url)
            finally:
                state["closed"] = True

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            assert timeout == 15
            assert follow_redirects is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, request):
            assert request.method == "GET", "The read-only diagnostic must not send any POST"
            assert request.headers["User-Agent"] == "GateScope-OAuth-Diagnostic/1.0"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            state["sent"].append((request.method, str(request.url)))
            if request.url.path.rstrip("/") == "/mcp":
                return probe.httpx2.Response(401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{ORIGIN}{PRM_PATH}"',
                }, content=b"{}")
            return probe.httpx2.Response(200, headers={"Content-Type": "application/json"}, content=b"{}")

    async def forbidden_write(self, value):
        state["storage_writes"] += 1
        raise AssertionError("TokenStorage must remain read-only")

    monkeypatch.setattr(probe, "OAuthClientProvider", Provider)
    monkeypatch.setattr(probe.httpx2, "AsyncClient", Client)
    monkeypatch.setattr(probe.EmptyStorage, "set_tokens", forbidden_write)
    monkeypatch.setattr(probe.EmptyStorage, "set_client_info", forbidden_write)
    return state


@pytest.mark.parametrize("url", [MCP_URL, MCP_URL + "/"])
def test_discovery_stops_before_registration_and_never_writes_credentials(monkeypatch, probe, url):
    planned = [("GET", url), ("GET", ORIGIN + PRM_PATH), ("GET", ORIGIN + ASM_PATH),
               ("POST", ORIGIN + "/oauth/register")]
    state = mock_discovery(monkeypatch, probe, planned)
    result = anyio.run(probe.inspect, url)
    assert result["ok"] is True and result["reached_registration"] is True
    assert result["registration_sent"] is False and result["credentials_used"] is False
    assert state["sent"] == planned[:3]
    assert state["storage_reads"] == 2 and state["storage_writes"] == 0
    assert state["closed"] is True
    assert result["steps"] == [
        {"method": "GET", "path": "/mcp/" if url.endswith("/") else "/mcp", "status": 401, "resource_challenge": True},
        {"method": "GET", "path": PRM_PATH, "status": 200, "resource_challenge": False},
        {"method": "GET", "path": ASM_PATH, "status": 200, "resource_challenge": False},
    ]


@pytest.mark.parametrize("method,target", [
    ("GET", "https://untrusted.example" + ASM_PATH),
    ("GET", ORIGIN + "/private/account"),
    ("GET", ORIGIN + ASM_PATH + "?token=private-test-value"),
    ("GET", ORIGIN + ASM_PATH + "#unexpected"),
    ("POST", ORIGIN + "/mcp"),
    ("POST", ORIGIN + "/oauth/token"),
    ("POST", "https://untrusted.example/oauth/register"),
    ("DELETE", ORIGIN + "/mcp"),
])
def test_discovery_rejects_off_origin_unknown_path_and_unexpected_mutation(monkeypatch, probe, method, target):
    state = mock_discovery(monkeypatch, probe, [("GET", MCP_URL), (method, target)])
    with pytest.raises(RuntimeError, match="UNEXPECTED_(DISCOVERY_TARGET|MUTATION)"):
        anyio.run(probe.inspect, MCP_URL)
    assert state["sent"] == [("GET", MCP_URL)]
    assert state["closed"] is True and state["storage_writes"] == 0


@pytest.mark.parametrize("url", [
    "http://43.108.44.74/mcp", "https:///mcp", ORIGIN + "/", ORIGIN + "/private",
    MCP_URL + "?token=private-test-value", MCP_URL + "#fragment", MCP_URL + "?", MCP_URL + "#",
    "https://user:private-test-value@43.108.44.74/mcp", "https://user@43.108.44.74/mcp",
    "https://@43.108.44.74/mcp", "https://:private-test-value@43.108.44.74/mcp",
])
def test_discovery_rejects_invalid_input_before_sdk_or_http_initialization(monkeypatch, probe, url):
    def no_initialization(**kwargs):
        raise AssertionError("Invalid URLs must not initialize an OAuth client or transport")

    monkeypatch.setattr(probe, "OAuthClientProvider", no_initialization)
    monkeypatch.setattr(probe.httpx2, "AsyncClient", no_initialization)
    with pytest.raises(ValueError, match="HTTPS_MCP_URL_REQUIRED"):
        anyio.run(probe.inspect, url)


def test_empty_storage_cannot_persist_client_or_tokens(probe):
    async def verify():
        storage = probe.EmptyStorage()
        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None
        for operation in (storage.set_tokens, storage.set_client_info):
            with pytest.raises(RuntimeError, match="READ_ONLY_DIAGNOSTIC"):
                await operation(object())

    anyio.run(verify)


def test_discovery_has_a_bounded_get_budget(monkeypatch, probe):
    state = mock_discovery(monkeypatch, probe, [("GET", MCP_URL)] * 20)
    result = anyio.run(probe.inspect, MCP_URL)
    assert result["ok"] is False
    assert result["registration_sent"] is False
    assert len(state["sent"]) == 8 and state["closed"] is True
    assert state["storage_writes"] == 0


def test_main_failure_is_safe_and_does_not_print_exception_details(monkeypatch, probe, capsys):
    private_text = "PRIVATE-CREDENTIAL-FIXTURE-DO-NOT-PRINT"

    async def failed_inspect(url):
        raise RuntimeError(private_text)

    monkeypatch.setattr(probe, "inspect", failed_inspect)
    monkeypatch.setattr(probe.logging, "disable", lambda level: None)
    monkeypatch.setattr(sys, "argv", ["check_mcp_oauth_discovery.py", MCP_URL])
    assert probe.main() == 1
    output = capsys.readouterr()
    assert private_text not in output.out and private_text not in output.err
    assert output.err == ""
    assert json.loads(output.out) == {"ok": False, "error_type": "RuntimeError",
                                     "registration_sent": False, "credentials_used": False}


def test_main_success_reports_only_safe_discovery_results(monkeypatch, probe, capsys):
    planned = [("GET", MCP_URL), ("GET", ORIGIN + PRM_PATH), ("GET", ORIGIN + ASM_PATH),
               ("POST", ORIGIN + "/oauth/register")]
    state = mock_discovery(monkeypatch, probe, planned)
    monkeypatch.setattr(probe.logging, "disable", lambda level: None)
    monkeypatch.setattr(sys, "argv", ["check_mcp_oauth_discovery.py", MCP_URL])
    assert probe.main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is True and report["registration_sent"] is False
    assert report["credentials_used"] is False and output.err == ""
    assert state["sent"] == planned[:3] and state["storage_writes"] == 0
