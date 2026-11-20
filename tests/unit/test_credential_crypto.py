from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gatescope.credential_crypto import (
    CredentialMaterial,
    RsaOaepCredentialDecryptor,
    RsaOaepCredentialEncryptor,
    credential_fingerprint,
)


def _pair() -> tuple[RsaOaepCredentialEncryptor, RsaOaepCredentialDecryptor]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_der = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        RsaOaepCredentialEncryptor.from_base64_der(base64.urlsafe_b64encode(public_der).decode(), "test-v1"),
        RsaOaepCredentialDecryptor.from_base64_der(base64.urlsafe_b64encode(private_der).decode(), "test-v1"),
    )


def test_encrypt_only_admission_round_trip_is_context_bound() -> None:
    encryptor, decryptor = _pair()
    context = b"tenant:one:account:one:credential:one"
    material = CredentialMaterial("test-key-1234567890", "test-secret-1234567890")
    envelope = encryptor.protect(material, context=context)
    assert decryptor.reveal(envelope, context=context) == material
    assert not hasattr(encryptor, "reveal")
    with pytest.raises(ValueError):
        decryptor.reveal(envelope, context=b"tenant:two:account:one:credential:one")


def test_fingerprint_contains_no_api_key_characters() -> None:
    key_material = "key-material-that-must-not-appear"
    fingerprint = credential_fingerprint("GATE", key_material)
    assert fingerprint.startswith("SHA256:")
    assert key_material not in fingerprint
    assert len(fingerprint) == 19


def test_wrapping_key_policy_rejects_weak_rsa() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="3072"):
        RsaOaepCredentialEncryptor(private.public_key(), "weak")
