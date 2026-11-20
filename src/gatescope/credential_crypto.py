"""Encrypt-only credential admission using a hybrid RSA/AES envelope.

The Web API receives only an RSA public key.  It can create envelopes but it
cannot recover their plaintext.  The credential validation workload receives
the matching private key and is the only process able to unwrap the data key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _decode_base64url(value: str) -> bytes:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("credential wrapping key is required")
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("credential wrapping key is not valid base64url") from exc


def _validate_context(context: bytes) -> None:
    if not 16 <= len(context) <= 512:
        raise ValueError("credential encryption context must contain 16 to 512 bytes")


@dataclass(frozen=True)
class CredentialEnvelope:
    ciphertext: bytes
    nonce: bytes
    wrapped_data_key: bytes
    key_version: str


@dataclass(frozen=True, repr=False)
class CredentialMaterial:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:
        return "CredentialMaterial(api_key=[REDACTED], api_secret=[REDACTED])"


class RsaOaepCredentialEncryptor:
    """Public-key-only envelope writer suitable for the API process."""

    def __init__(self, public_key: rsa.RSAPublicKey, key_version: str) -> None:
        if public_key.key_size < 3072:
            raise ValueError("credential wrapping RSA key must be at least 3072 bits")
        if not key_version or len(key_version) > 80:
            raise ValueError("credential wrapping key version is invalid")
        self._public_key = public_key
        self.key_version = key_version

    @classmethod
    def from_base64_der(cls, value: str, key_version: str) -> "RsaOaepCredentialEncryptor":
        key = serialization.load_der_public_key(_decode_base64url(value))
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("credential wrapping key must be RSA")
        return cls(key, key_version)

    def protect(self, material: CredentialMaterial, *, context: bytes) -> CredentialEnvelope:
        _validate_context(context)
        payload = bytearray(
            json.dumps(
                {"api_key": material.api_key, "api_secret": material.api_secret},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        data_key = bytearray(secrets.token_bytes(32))
        nonce = secrets.token_bytes(12)
        try:
            ciphertext = AESGCM(bytes(data_key)).encrypt(nonce, bytes(payload), context)
            wrapped = self._public_key.encrypt(
                bytes(data_key),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=context,
                ),
            )
            return CredentialEnvelope(ciphertext, nonce, wrapped, self.key_version)
        finally:
            for index in range(len(payload)):
                payload[index] = 0
            for index in range(len(data_key)):
                data_key[index] = 0


class RsaOaepCredentialDecryptor:
    """Private envelope reader; instantiate only in the validation workload."""

    def __init__(self, private_key: rsa.RSAPrivateKey, key_version: str) -> None:
        if private_key.key_size < 3072:
            raise ValueError("credential wrapping RSA key must be at least 3072 bits")
        if not key_version or len(key_version) > 80:
            raise ValueError("credential wrapping key version is invalid")
        self._private_key = private_key
        self.key_version = key_version

    @classmethod
    def from_base64_der(cls, value: str, key_version: str) -> "RsaOaepCredentialDecryptor":
        key = serialization.load_der_private_key(_decode_base64url(value), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("credential wrapping key must be RSA")
        return cls(key, key_version)

    def reveal(self, envelope: CredentialEnvelope, *, context: bytes) -> CredentialMaterial:
        _validate_context(context)
        if envelope.key_version != self.key_version:
            raise ValueError("credential wrapping key version is unavailable")
        data_key = bytearray(
            self._private_key.decrypt(
                envelope.wrapped_data_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=context,
                ),
            )
        )
        plaintext = bytearray()
        try:
            plaintext.extend(AESGCM(bytes(data_key)).decrypt(envelope.nonce, envelope.ciphertext, context))
            decoded: Any = json.loads(plaintext)
            if not isinstance(decoded, dict):
                raise ValueError("credential envelope payload is invalid")
            api_key = decoded.get("api_key")
            api_secret = decoded.get("api_secret")
            if not isinstance(api_key, str) or not isinstance(api_secret, str):
                raise ValueError("credential envelope payload is invalid")
            return CredentialMaterial(api_key=api_key, api_secret=api_secret)
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
            for index in range(len(data_key)):
                data_key[index] = 0


def credential_fingerprint(venue: str, api_key: str) -> str:
    """Return a non-reversible display identifier; never retain key characters."""

    normalized_venue = venue.strip().upper()
    if not normalized_venue or not api_key:
        raise ValueError("venue and API key are required")
    digest = hashlib.sha256(f"{normalized_venue}\0{api_key}".encode("utf-8")).hexdigest()
    return f"SHA256:{digest[-12:].upper()}"
