from __future__ import annotations

import json
import time
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.responses import JSONResponse

from apps.api.formal_app import FormalLoginRequest, UserCreateRequest, _resolve_sensitive_submission
from gatescope.submission_crypto import (
    EncryptedSubmissionRequest,
    RsaOaepSubmissionDecryptor,
    encrypt_submission_for_test,
)
from scripts.generate_formal_openapi import generate


def _request(*, host: str, client_host: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/login",
        "raw_path": b"/auth/login",
        "query_string": b"",
        "headers": [(b"host", host.encode("ascii"))],
        "server": (host.split(":", 1)[0], 80),
        "client": (client_host, 49152),
    }
    request = Request(scope)
    request.state.trace_id = "0123456789abcdef"
    return request


def test_remote_http_accepts_envelope_but_rejects_plaintext() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    decryptor = RsaOaepSubmissionDecryptor(private, private.public_key(), "boundary-v1")
    envelope = encrypt_submission_for_test(
        private.public_key(),
        key_version="boundary-v1",
        purpose="auth_login",
        request_id=str(uuid4()),
        issued_at=int(time.time() * 1_000),
        payload={"tenant": "default", "username": "owner", "password": "not-returned"},
    )
    remote = _request(host="trade.example.com", client_host="203.0.113.9")
    resolved = _resolve_sensitive_submission(
        EncryptedSubmissionRequest(encrypted_submission=envelope),
        remote,
        decryptor=decryptor,
        expected_purpose="auth_login",
        model_type=FormalLoginRequest,
    )
    assert isinstance(resolved, FormalLoginRequest)
    assert resolved.username == "owner"

    rejected = _resolve_sensitive_submission(
        FormalLoginRequest(tenant="default", username="owner", password="not-returned"),
        remote,
        decryptor=decryptor,
        expected_purpose="auth_login",
        model_type=FormalLoginRequest,
    )
    assert isinstance(rejected, JSONResponse)
    assert json.loads(rejected.body)["error"]["code"] == "ENCRYPTED_SUBMISSION_REQUIRED"


def test_public_submission_key_contract_is_not_session_protected() -> None:
    operation = generate()["paths"]["/api/v2/security/submission-key"]["get"]
    assert "security" not in operation


def test_user_creation_password_is_encrypted_and_tenant_bound() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    decryptor = RsaOaepSubmissionDecryptor(private, private.public_key(), "admin-test")
    payload = {"username": "product-user", "initial_password": "never-reflect-password-123", "role": "VIEWER"}
    envelope = encrypt_submission_for_test(
        private.public_key(), key_version="admin-test", purpose="tenant:tenant-a:user_create",
        request_id=str(uuid4()), issued_at=int(time.time() * 1000), payload=payload,
    )
    remote = _request(host="trade.example.com", client_host="203.0.113.9")
    resolved = _resolve_sensitive_submission(
        EncryptedSubmissionRequest(encrypted_submission=envelope), remote, decryptor=decryptor,
        expected_purpose="tenant:tenant-a:user_create", model_type=UserCreateRequest,
    )
    assert isinstance(resolved, UserCreateRequest) and resolved.username == "product-user"
    wrong_tenant = _resolve_sensitive_submission(
        EncryptedSubmissionRequest(encrypted_submission=envelope), remote, decryptor=decryptor,
        expected_purpose="tenant:tenant-b:user_create", model_type=UserCreateRequest,
    )
    assert isinstance(wrong_tenant, JSONResponse) and wrong_tenant.status_code == 422
    plaintext = _resolve_sensitive_submission(
        UserCreateRequest(**payload), remote, decryptor=decryptor,
        expected_purpose="tenant:tenant-a:user_create", model_type=UserCreateRequest,
    )
    assert isinstance(plaintext, JSONResponse) and plaintext.status_code == 403
    assert payload["initial_password"].encode() not in plaintext.body + wrong_tenant.body
