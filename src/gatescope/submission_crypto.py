"""Browser-to-API secret submission envelopes.

This layer protects sensitive JSON before it enters an HTTP request body.  It
is deliberately independent from the at-rest credential envelopes: the API
must be able to unwrap a submission, validate it, and immediately pass the
validated secret to the existing storage boundary.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field


SUBMISSION_ALGORITHM = "RSA-OAEP-256+A256GCM"
SUBMISSION_MAX_AGE_SECONDS = 120
SUBMISSION_MAX_FUTURE_SKEW_SECONDS = 30
SUBMISSION_MAX_PLAINTEXT_BYTES = 16_384
SUBMISSION_MAX_CIPHERTEXT_BYTES = SUBMISSION_MAX_PLAINTEXT_BYTES + 16
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUEST_ID_RE = r"^[A-Za-z0-9_-]{22,64}$"
_PURPOSE_RE = r"^[a-z][a-z0-9_.:-]{2,159}$"


class SubmissionEnvelope(BaseModel):
    """Wire format accepted by sensitive API operations."""

    model_config = ConfigDict(extra="forbid")

    alg: str = Field(pattern=r"^RSA-OAEP-256\+A256GCM$")
    key_version: str = Field(min_length=1, max_length=80)
    request_id: str = Field(pattern=_REQUEST_ID_RE)
    issued_at: int = Field(ge=1_600_000_000_000, le=9_999_999_999_999)
    purpose: str = Field(pattern=_PURPOSE_RE)
    wrapped_key: str = Field(min_length=128, max_length=1_024)
    nonce: str = Field(min_length=16, max_length=16)
    ciphertext: str = Field(min_length=24, max_length=22_000)


class EncryptedSubmissionRequest(BaseModel):
    """Explicit wrapper keeps encrypted and legacy plaintext bodies unambiguous."""

    model_config = ConfigDict(extra="forbid")

    encrypted_submission: SubmissionEnvelope


class SubmissionError(ValueError):
    """An intentionally safe, stable failure at the submission boundary."""

    def __init__(self, code: str, *, status_code: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class SubmissionReplayCache:
    """A process-local bounded single-use request-id cache.

    Multi-process deployments still need sticky routing or a shared cache for
    global replay exclusion.  The cryptographic time window bounds exposure in
    either case.
    """

    def __init__(self, *, capacity: int = 10_000, ttl_seconds: int = 180) -> None:
        if capacity < 100 or ttl_seconds < SUBMISSION_MAX_AGE_SECONDS:
            raise ValueError("submission replay cache configuration is invalid")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def accept_once(self, request_id: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        cutoff = current - self._ttl_seconds
        with self._lock:
            while self._entries:
                oldest_key, oldest_at = next(iter(self._entries.items()))
                if oldest_at >= cutoff:
                    break
                self._entries.pop(oldest_key, None)
            if request_id in self._entries:
                return False
            self._entries[request_id] = current
            self._entries.move_to_end(request_id)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return True


@dataclass(frozen=True)
class SubmissionKeyInfo:
    key_version: str
    public_key_spki: str


def submission_aad(
    *, key_version: str, purpose: str, request_id: str, issued_at: int,
) -> bytes:
    """Canonical AAD shared by Web Crypto and the API."""

    return (
        f"gatescope-submission-v1\n{key_version}\n{purpose}\n{request_id}\n{issued_at}"
    ).encode("utf-8")


class RsaOaepSubmissionDecryptor:
    def __init__(
        self,
        private_key: rsa.RSAPrivateKey,
        public_key: rsa.RSAPublicKey,
        key_version: str,
        *,
        replay_cache: SubmissionReplayCache | None = None,
    ) -> None:
        if private_key.key_size < 3072 or public_key.key_size < 3072:
            raise ValueError("submission wrapping RSA key must be at least 3072 bits")
        if private_key.public_key().public_numbers() != public_key.public_numbers():
            raise ValueError("submission public/private key pair does not match")
        if not key_version or len(key_version) > 80:
            raise ValueError("submission wrapping key version is invalid")
        self._private_key = private_key
        self._public_key = public_key
        self.key_version = key_version
        self._replay_cache = replay_cache or SubmissionReplayCache()

    @classmethod
    def from_base64_der(
        cls, public_value: str, private_value: str, key_version: str,
    ) -> "RsaOaepSubmissionDecryptor":
        try:
            public_key = serialization.load_der_public_key(_decode_key(public_value))
            private_key = serialization.load_der_private_key(
                _decode_key(private_value), password=None,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("submission wrapping key configuration is invalid") from exc
        if not isinstance(public_key, rsa.RSAPublicKey) or not isinstance(
            private_key, rsa.RSAPrivateKey,
        ):
            raise ValueError("submission wrapping keys must be RSA")
        return cls(private_key, public_key, key_version)

    def key_info(self) -> SubmissionKeyInfo:
        encoded = self._public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return SubmissionKeyInfo(self.key_version, _encode_base64url(encoded))

    def decrypt(
        self,
        envelope: SubmissionEnvelope,
        *,
        expected_purpose: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if envelope.alg != SUBMISSION_ALGORITHM:
            raise SubmissionError("SUBMISSION_ALGORITHM_UNSUPPORTED")
        if envelope.key_version != self.key_version:
            raise SubmissionError("SUBMISSION_KEY_VERSION_UNAVAILABLE", status_code=409)
        if envelope.purpose != expected_purpose:
            raise SubmissionError("SUBMISSION_PURPOSE_MISMATCH")
        current = time.time() if now is None else now
        issued_seconds = envelope.issued_at / 1_000
        if (
            issued_seconds < current - SUBMISSION_MAX_AGE_SECONDS
            or issued_seconds > current + SUBMISSION_MAX_FUTURE_SKEW_SECONDS
        ):
            raise SubmissionError("SUBMISSION_EXPIRED", status_code=409)

        wrapped_key = _decode_wire(envelope.wrapped_key, "wrapped_key", maximum=1_024)
        expected_wrapped_size = self._private_key.key_size // 8
        if len(wrapped_key) != expected_wrapped_size:
            raise SubmissionError("INVALID_SUBMISSION_ENVELOPE")
        nonce = _decode_wire(envelope.nonce, "nonce", maximum=12)
        if len(nonce) != 12:
            raise SubmissionError("INVALID_SUBMISSION_ENVELOPE")
        ciphertext = _decode_wire(
            envelope.ciphertext, "ciphertext", maximum=SUBMISSION_MAX_CIPHERTEXT_BYTES,
        )
        if len(ciphertext) < 17:
            raise SubmissionError("INVALID_SUBMISSION_ENVELOPE")

        aad = submission_aad(
            key_version=envelope.key_version,
            purpose=envelope.purpose,
            request_id=envelope.request_id,
            issued_at=envelope.issued_at,
        )
        data_key = bytearray()
        plaintext = bytearray()
        try:
            data_key.extend(
                self._private_key.decrypt(
                    wrapped_key,
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=aad,
                    ),
                )
            )
            if len(data_key) != 32:
                raise SubmissionError("SUBMISSION_DECRYPTION_FAILED")
            plaintext.extend(AESGCM(bytes(data_key)).decrypt(nonce, ciphertext, aad))
            if not plaintext or len(plaintext) > SUBMISSION_MAX_PLAINTEXT_BYTES:
                raise SubmissionError("SUBMISSION_DECRYPTION_FAILED")
            decoded = json.loads(plaintext)
        except SubmissionError:
            raise
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SubmissionError("SUBMISSION_DECRYPTION_FAILED") from exc
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
            for index in range(len(data_key)):
                data_key[index] = 0

        if not isinstance(decoded, dict) or set(decoded) != {
            "purpose", "request_id", "issued_at", "payload",
        }:
            raise SubmissionError("INVALID_SUBMISSION_PAYLOAD")
        if (
            decoded.get("purpose") != envelope.purpose
            or decoded.get("request_id") != envelope.request_id
            or decoded.get("issued_at") != envelope.issued_at
            or not isinstance(decoded.get("payload"), dict)
        ):
            raise SubmissionError("INVALID_SUBMISSION_PAYLOAD")
        replay_key = f"{self.key_version}:{envelope.request_id}"
        if not self._replay_cache.accept_once(replay_key, now=current):
            raise SubmissionError("SUBMISSION_REPLAYED", status_code=409)
        return dict(decoded["payload"])


def encrypt_submission_for_test(
    public_key: rsa.RSAPublicKey,
    *,
    key_version: str,
    purpose: str,
    request_id: str,
    issued_at: int,
    payload: Mapping[str, Any],
) -> SubmissionEnvelope:
    """Reference producer used by interoperability tests (browsers use Web Crypto)."""

    aad = submission_aad(
        key_version=key_version,
        purpose=purpose,
        request_id=request_id,
        issued_at=issued_at,
    )
    plaintext = json.dumps(
        {
            "purpose": purpose,
            "request_id": request_id,
            "issued_at": issued_at,
            "payload": dict(payload),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(plaintext) > SUBMISSION_MAX_PLAINTEXT_BYTES:
        raise ValueError("submission plaintext is too large")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = AESGCM.generate_key(bit_length=128)[:12]
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
    wrapped_key = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=aad,
        ),
    )
    return SubmissionEnvelope(
        alg=SUBMISSION_ALGORITHM,
        key_version=key_version,
        request_id=request_id,
        issued_at=issued_at,
        purpose=purpose,
        wrapped_key=_encode_base64url(wrapped_key),
        nonce=_encode_base64url(nonce),
        ciphertext=_encode_base64url(ciphertext),
    )


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_key(value: str) -> bytes:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("submission wrapping key is required")
    try:
        return base64.b64decode(
            cleaned + "=" * (-len(cleaned) % 4), altchars=b"-_", validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("submission wrapping key is not valid base64url") from exc


def _decode_wire(value: str, field: str, *, maximum: int) -> bytes:
    if not value or len(value) > maximum * 2 or _BASE64URL_RE.fullmatch(value) is None:
        raise SubmissionError("INVALID_SUBMISSION_ENVELOPE")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise SubmissionError("INVALID_SUBMISSION_ENVELOPE") from exc
    if len(decoded) > maximum:
        raise SubmissionError("INVALID_SUBMISSION_ENVELOPE")
    return decoded
