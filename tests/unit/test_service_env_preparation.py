from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.prepare_service_envs import ensure_coinglass_wrap_key, ensure_submission_wrap_key


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_submission_key_generation_is_complete_and_idempotent() -> None:
    values: dict[str, str] = {}
    ensure_submission_wrap_key(values)

    public_key = serialization.load_der_public_key(
        _decode(values["GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY"])
    )
    private_key = serialization.load_der_private_key(
        _decode(values["GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY"]), password=None,
    )
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert public_key.key_size == private_key.key_size == 3072
    assert public_key.public_numbers() == private_key.public_key().public_numbers()

    original = dict(values)
    ensure_submission_wrap_key(values)
    assert values == original


def test_submission_key_generation_rejects_partial_configuration() -> None:
    with pytest.raises(RuntimeError, match="incomplete"):
        ensure_submission_wrap_key({"GATESCOPE_SUBMISSION_KEY_VERSION": "partial-v1"})


def test_coinglass_key_generation_is_complete_and_idempotent() -> None:
    values: dict[str, str] = {}
    ensure_coinglass_wrap_key(values)
    public_key = serialization.load_der_public_key(
        _decode(values["GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY"])
    )
    private_key = serialization.load_der_private_key(
        _decode(values["GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY"]), password=None,
    )
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert public_key.public_numbers() == private_key.public_key().public_numbers()

    original = dict(values)
    ensure_coinglass_wrap_key(values)
    assert values == original
