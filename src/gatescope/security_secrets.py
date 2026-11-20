"""MFA secret protection, RFC 6238 verification and one-time recovery codes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class ProtectedSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class AesGcmSecretProtector:
    def __init__(self, key: bytes, key_version: str) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM key must contain exactly 32 bytes")
        if not key_version:
            raise ValueError("key version is required")
        self._cipher = AESGCM(key)
        self.key_version = key_version

    def protect(self, plaintext: bytes, *, context: bytes) -> ProtectedSecret:
        nonce = secrets.token_bytes(12)
        return ProtectedSecret(self._cipher.encrypt(nonce, plaintext, context), nonce, self.key_version)

    def reveal(self, protected: ProtectedSecret, *, context: bytes) -> bytes:
        if protected.key_version != self.key_version:
            raise ValueError("secret key version is unavailable")
        return self._cipher.decrypt(protected.nonce, protected.ciphertext, context)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, *, at: int | None = None, step_seconds: int = 30, digits: int = 6) -> tuple[str, int]:
    if step_seconds < 15 or not 6 <= digits <= 8:
        raise ValueError("TOTP parameters are outside the supported security policy")
    timestamp = int(time.time()) if at is None else int(at)
    step = timestamp // step_seconds
    padding = "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(secret.upper() + padding, casefold=True)
    except ValueError as exc:
        raise ValueError("TOTP secret is invalid") from exc
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits), step


def verify_totp(
    secret: str,
    supplied: str,
    *,
    at: int | None = None,
    last_accepted_step: int | None = None,
    drift_steps: int = 1,
) -> int | None:
    if not supplied.isascii() or not supplied.isdigit() or len(supplied) != 6:
        return None
    timestamp = int(time.time()) if at is None else int(at)
    for offset in range(-drift_steps, drift_steps + 1):
        expected, step = totp_code(secret, at=timestamp + offset * 30)
        if hmac.compare_digest(expected, supplied):
            if last_accepted_step is not None and step <= last_accepted_step:
                return None
            return step
    return None


def generate_recovery_codes(count: int = 10) -> tuple[str, ...]:
    if not 5 <= count <= 20:
        raise ValueError("recovery code count must be between 5 and 20")
    return tuple(
        f"{token[:5]}-{token[5:]}"
        for token in (base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=") for _ in range(count))
    )


def recovery_code_hash(code: str, pepper: bytes) -> str:
    if len(pepper) < 32:
        raise ValueError("recovery code pepper must contain at least 32 bytes")
    normalized = code.replace("-", "").strip().upper().encode("ascii", "strict")
    return hmac.new(pepper, normalized, hashlib.sha256).hexdigest()
