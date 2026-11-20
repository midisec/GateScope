from __future__ import annotations

from fastapi import Request

from apps.api.formal_app import (
    FormalLoginRequest,
    _legacy_plaintext_submission_allowed,
    _resolve_sensitive_submission,
)


def _request(
    *,
    scheme: str,
    host: str,
    client_host: str,
    forwarded_for: str | None = None,
) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": scheme,
        "path": "/api/v2/exchange-accounts",
        "raw_path": b"/api/v2/exchange-accounts",
        "query_string": b"",
        "headers": headers,
        "server": (host.split(":", 1)[0], 80),
        "client": (client_host, 49152),
    }
    return Request(scope)


def test_plaintext_secret_submission_is_rejected_on_public_http() -> None:
    request = _request(scheme="http", host="trade.example.com", client_host="203.0.113.9")

    assert not _legacy_plaintext_submission_allowed(request)


def test_legacy_plaintext_submission_is_not_a_remote_https_bypass() -> None:
    request = _request(scheme="https", host="trade.example.com", client_host="203.0.113.9")

    assert not _legacy_plaintext_submission_allowed(request)


def test_plaintext_secret_submission_does_not_trust_forwarded_for_loopback() -> None:
    request = _request(
        scheme="http",
        host="trade.example.com",
        client_host="203.0.113.9",
        forwarded_for="127.0.0.1",
    )

    assert not _legacy_plaintext_submission_allowed(request)


def test_plaintext_secret_submission_requires_both_loopback_client_and_host() -> None:
    loopback = _request(scheme="http", host="localhost:17758", client_host="127.0.0.1")
    remote_with_local_host = _request(
        scheme="http", host="localhost:17758", client_host="203.0.113.9",
    )
    local_with_public_host = _request(
        scheme="http", host="trade.example.com", client_host="127.0.0.1",
    )

    assert _legacy_plaintext_submission_allowed(loopback)
    assert not _legacy_plaintext_submission_allowed(remote_with_local_host)
    assert not _legacy_plaintext_submission_allowed(local_with_public_host)


def test_public_plaintext_body_is_rejected_without_echoing_secret() -> None:
    secret = "never-reflect-" + "this-password"
    request = _request(scheme="http", host="trade.example.com", client_host="203.0.113.9")
    request.state.trace_id = "0123456789abcdef"
    submitted = FormalLoginRequest(tenant="default", username="owner", password=secret)

    response = _resolve_sensitive_submission(
        submitted,
        request,
        decryptor=None,
        expected_purpose="auth_login",
        model_type=FormalLoginRequest,
    )

    assert response.status_code == 403
    assert b"ENCRYPTED_SUBMISSION_REQUIRED" in response.body
    assert secret.encode("utf-8") not in response.body


def test_loopback_plaintext_body_remains_compatible() -> None:
    request = _request(scheme="http", host="localhost:17758", client_host="127.0.0.1")
    request.state.trace_id = "0123456789abcdef"
    submitted = FormalLoginRequest(
        tenant="default", username="owner", password="loopback-password",
    )

    resolved = _resolve_sensitive_submission(
        submitted,
        request,
        decryptor=None,
        expected_purpose="auth_login",
        model_type=FormalLoginRequest,
    )

    assert isinstance(resolved, FormalLoginRequest)
    assert resolved.password == "loopback-password"
