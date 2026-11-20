"""Bounded, credential-free HTTPS gate before starting the OpenAI tunnel.

Uses only an explicit origin and GET requests, without environment configuration,
HTTP proxies, cookies, authorization headers, redirects, or response-body logs.
The deployed Linux CLI has a hard wall-clock alarm as well as per-I/O timeouts.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import signal
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

BODY_LIMIT = 16_384
MAX_ATTEMPTS = 30
IO_TIMEOUT = 3.0
PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
AS_PATH = "/.well-known/oauth-authorization-server"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ProbeError(Exception):
    def __init__(self, reason: str, *, retryable: bool = False, retry_after: float = 0) -> None:
        super().__init__(reason)
        self.reason, self.retryable, self.retry_after = reason, retryable, retry_after


def validate_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        raise ProbeError("invalid_origin") from None
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
            or any(character.isspace() or ord(character) < 32 for character in origin)
            or (port is not None and not 1 <= port <= 65535)):
        raise ProbeError("invalid_origin")
    return origin


def retry_after_seconds(value: str) -> float:
    try:
        if value.isdecimal():
            return min(float(value), 86_400)
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return max(0.0, min((instant - datetime.now(timezone.utc)).total_seconds(), 86_400))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def https_get(origin: str, path: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    """The only network boundary: no proxy environment or redirects are used."""
    parsed = urlsplit(origin)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout,
                                              context=ssl.create_default_context())
    try:
        connection.request("GET", path, headers={"Accept": "application/json, text/event-stream",
                            "User-Agent": "GateScope-Origin-Readiness/1", "Connection": "close"})
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        # Refuse redirects/transient error bodies before reading anything from
        # them. Only metadata success bodies are needed, each strictly bounded.
        body = response.read(BODY_LIMIT + 1) if response.status == 200 else b""
        if len(body) > BODY_LIMIT:
            raise ProbeError("body_too_large")
        return response.status, headers, body
    except ProbeError:
        raise
    except ssl.SSLCertVerificationError:
        raise ProbeError("tls_verification_failed") from None
    except (OSError, http.client.HTTPException):
        raise ProbeError("network_unavailable", retryable=True) from None
    finally:
        connection.close()


def read_metadata(status: int, headers: dict[str, str], body: bytes) -> dict:
    if 300 <= status < 400:
        raise ProbeError("redirect_refused")
    if status in RETRYABLE_STATUS:
        raise ProbeError("upstream_not_ready", retryable=True,
                         retry_after=retry_after_seconds(headers.get("retry-after", "")))
    if status != 200:
        raise ProbeError("unexpected_metadata_status")
    if len(body) > BODY_LIMIT:
        raise ProbeError("body_too_large")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ProbeError("invalid_metadata_content_type")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_metadata_json") from None
    if not isinstance(result, dict):
        raise ProbeError("invalid_metadata_json")
    return result


def probe_once(origin: str, deadline: float, *, fetch=https_get, clock=time.monotonic) -> None:
    def read(path):
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProbeError("deadline_exceeded")
        result = fetch(origin, path, min(IO_TIMEOUT, remaining))
        if clock() >= deadline:
            raise ProbeError("deadline_exceeded")
        return result

    protected = read_metadata(*read(PRM_PATH))
    if protected.get("resource") != origin + "/mcp" or protected.get("authorization_servers") != [origin]:
        raise ProbeError("protected_resource_mismatch")
    metadata = read_metadata(*read(AS_PATH))
    if metadata.get("issuer") != origin:
        raise ProbeError("authorization_issuer_mismatch")
    for name, path in (("authorization_endpoint", "/oauth/authorize"), ("token_endpoint", "/oauth/token"),
                       ("registration_endpoint", "/oauth/register")):
        if metadata.get(name) != origin + path:
            raise ProbeError("authorization_endpoint_mismatch")
    if (not isinstance(metadata.get("code_challenge_methods_supported"), list)
            or "S256" not in metadata["code_challenge_methods_supported"]):
        raise ProbeError("pkce_s256_missing")
    status, headers, _ = read("/mcp")
    if 300 <= status < 400:
        raise ProbeError("redirect_refused")
    if status in RETRYABLE_STATUS:
        raise ProbeError("upstream_not_ready", retryable=True,
                         retry_after=retry_after_seconds(headers.get("retry-after", "")))
    challenge = headers.get("www-authenticate", "")
    match = re.search(r'\bBearer\s+.*?\bresource_metadata\s*=\s*"([^"\r\n]+)"', challenge, flags=re.IGNORECASE)
    if status != 401 or match is None or match[1] not in {
        origin + PRM_PATH, origin + "/.well-known/oauth-protected-resource",
    }:
        raise ProbeError("mcp_discovery_challenge_missing")


def wait_until_ready(origin: str, timeout: float, *, fetch=https_get, clock=time.monotonic,
                     sleep=time.sleep) -> dict:
    attempts = 0
    try:
        validate_origin(origin)
        if not 1 <= timeout <= 120:
            raise ProbeError("invalid_timeout")
        deadline = clock() + timeout
        while attempts < MAX_ATTEMPTS and clock() < deadline:
            attempts += 1
            try:
                probe_once(origin, deadline, fetch=fetch, clock=clock)
                return {"ready": True, "attempts": attempts}
            except ProbeError as error:
                if not error.retryable:
                    raise
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                delay = max(min(float(attempts), 3.0), error.retry_after)
                sleep(min(delay, remaining))
        raise ProbeError("deadline_exceeded" if clock() >= deadline else "attempt_limit_reached")
    except ProbeError as error:
        return {"ready": False, "attempts": attempts, "failure": error.reason}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    report = {"ready": False, "attempts": 0, "failure": "probe_failed"}
    try:
        validate_origin(args.origin)
        if not 1 <= args.timeout <= 120:
            raise ProbeError("invalid_timeout")
        def deadline_reached(_signal, _frame):
            raise ProbeError("deadline_exceeded")
        # A hard bound also covers DNS/TLS or slowly streamed headers/bodies;
        # socket timeouts alone only bound inactivity, not total wall time.
        previous_handler = signal.signal(signal.SIGALRM, deadline_reached)
        try:
            signal.setitimer(signal.ITIMER_REAL, args.timeout)
            report = wait_until_ready(args.origin, args.timeout)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    except ProbeError as error:
        report["failure"] = error.reason
    except Exception:
        pass  # Never emit request URLs, response bodies, TLS details, or env data.
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
