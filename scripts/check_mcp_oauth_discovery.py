"""Read-only OAuth discovery using the actual MCP SDK; never sends registration.

Run from an external host as well as the deployment host. No token, account,
cookie, browser callback, login or OpenAI request is used. Only fixed paths,
HTTP statuses and booleans are printed; exception details remain private.
"""

from __future__ import annotations

import argparse
import json
import logging
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata


class EmptyStorage:
    async def get_tokens(self):
        return None

    async def get_client_info(self):
        return None

    async def set_tokens(self, tokens):
        raise RuntimeError("READ_ONLY_DIAGNOSTIC")

    async def set_client_info(self, client):
        raise RuntimeError("READ_ONLY_DIAGNOSTIC")


async def inspect(url: str) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"/mcp", "/mcp/"}
            or "?" in url or "#" in url or parsed.username is not None or parsed.password is not None):
        raise ValueError("HTTPS_MCP_URL_REQUIRED")
    origin = parsed.scheme + "://" + parsed.netloc
    callback = "https://chatgpt.com/connector_platform_oauth_redirect"
    client_metadata = OAuthClientMetadata.model_validate({
        "client_name": "GateScope Read-only OAuth Diagnostic", "redirect_uris": [callback],
        "token_endpoint_auth_method": "none", "response_types": ["code"],
        "grant_types": ["authorization_code", "refresh_token"],
        "scope": "mcp:connect market:read analysis:read",
    })
    provider = OAuthClientProvider(server_url=url, client_metadata=client_metadata, storage=EmptyStorage())
    flow = provider.async_auth_flow(httpx2.Request("GET", url))
    report = {"steps": [], "registration_sent": False, "credentials_used": False, "ok": False}
    allowed_paths = {"/mcp", "/mcp/", "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server",
                     "/.well-known/openid-configuration", "/oauth/register"}
    try:
        current = await flow.__anext__()
        async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
            for _ in range(8):
                target = urlsplit(str(current.url))
                if (target.scheme + "://" + target.netloc != origin or target.path not in allowed_paths
                        or target.query or target.fragment):
                    raise RuntimeError("UNEXPECTED_DISCOVERY_TARGET")
                if current.method != "GET":
                    if current.method != "POST" or target.path != "/oauth/register":
                        raise RuntimeError("UNEXPECTED_MUTATION")
                    report.update(ok=True, reached_registration=True)
                    break
                current.headers["User-Agent"] = "GateScope-OAuth-Diagnostic/1.0"
                response = await client.send(current)
                await response.aread()
                report["steps"].append({"method": "GET", "path": target.path, "status": response.status_code,
                    "resource_challenge": "resource_metadata=" in response.headers.get("www-authenticate", "")})
                current = await flow.asend(response)
    finally:
        await flow.aclose()
    return report


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://43.108.44.74/mcp")
    args = parser.parse_args()
    try:
        result = anyio.run(inspect, args.url)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "registration_sent": False, "credentials_used": False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
