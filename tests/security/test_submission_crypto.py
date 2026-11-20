from __future__ import annotations

import base64
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from gatescope.submission_crypto import (
    SUBMISSION_MAX_CIPHERTEXT_BYTES,
    SUBMISSION_MAX_FUTURE_SKEW_SECONDS,
    SUBMISSION_MAX_PLAINTEXT_BYTES,
    SUBMISSION_MAX_AGE_SECONDS,
    RsaOaepSubmissionDecryptor,
    SubmissionEnvelope,
    SubmissionError,
    SubmissionReplayCache,
    encrypt_submission_for_test,
    submission_aad,
)


KEY_VERSION = "submission-test-v1"
TENANT_ID = "10000000-0000-0000-0000-000000000001"
PURPOSE = f"tenant:{TENANT_ID}:exchange_account_create"
SECRET = "never-echo-" + "this-api-secret"


@pytest.fixture(scope="module")
def rsa_pair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return private_key, private_key.public_key()


@pytest.fixture
def decryptor(
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> RsaOaepSubmissionDecryptor:
    private_key, public_key = rsa_pair
    return RsaOaepSubmissionDecryptor(
        private_key,
        public_key,
        KEY_VERSION,
        replay_cache=SubmissionReplayCache(capacity=100, ttl_seconds=180),
    )


def _envelope(
    public_key: rsa.RSAPublicKey,
    *,
    now: float = 2_000_000_000,
    purpose: str = PURPOSE,
    payload: dict[str, Any] | None = None,
) -> SubmissionEnvelope:
    return encrypt_submission_for_test(
        public_key,
        key_version=KEY_VERSION,
        purpose=purpose,
        request_id=str(uuid.uuid4()),
        issued_at=int(now * 1_000),
        payload=payload or {"api_key": "test-api-key", "api_secret": SECRET},
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _alter_base64url(value: str) -> str:
    decoded = bytearray(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    decoded[len(decoded) // 2] ^= 1
    return _b64url(decoded)


def _encrypt_inner_document(
    public_key: rsa.RSAPublicKey,
    *,
    outer: SubmissionEnvelope,
    document: dict[str, Any],
) -> SubmissionEnvelope:
    aad = submission_aad(
        key_version=outer.key_version,
        purpose=outer.purpose,
        request_id=outer.request_id,
        issued_at=outer.issued_at,
    )
    plaintext = json.dumps(document, separators=(",", ":")).encode("utf-8")
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = b"\x05" * 12
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
    wrapped = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=aad,
        ),
    )
    return outer.model_copy(update={
        "wrapped_key": _b64url(wrapped),
        "nonce": _b64url(nonce),
        "ciphertext": _b64url(ciphertext),
    })


def _assert_safe_error(
    decryptor: RsaOaepSubmissionDecryptor,
    envelope: SubmissionEnvelope,
    *,
    expected_code: str,
    expected_purpose: str = PURPOSE,
    now: float = 2_000_000_000,
) -> SubmissionError:
    with pytest.raises(SubmissionError) as caught:
        decryptor.decrypt(envelope, expected_purpose=expected_purpose, now=now)
    error = caught.value
    assert error.code == expected_code
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    return error


def test_round_trip_and_replay_are_single_use(
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])

    assert decryptor.decrypt(envelope, expected_purpose=PURPOSE, now=2_000_000_000) == {
        "api_key": "test-api-key",
        "api_secret": SECRET,
    }
    replay = _assert_safe_error(decryptor, envelope, expected_code="SUBMISSION_REPLAYED")
    assert replay.status_code == 409


@pytest.mark.parametrize("field", ["ciphertext", "wrapped_key"])
def test_authenticated_envelope_rejects_ciphertext_or_wrapped_key_tampering(
    field: str,
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    tampered = envelope.model_copy(update={field: _alter_base64url(getattr(envelope, field))})

    _assert_safe_error(decryptor, tampered, expected_code="SUBMISSION_DECRYPTION_FAILED")


@pytest.mark.parametrize("field", ["request_id", "issued_at"])
def test_authenticated_envelope_rejects_aad_metadata_tampering(
    field: str,
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    replacement: str | int
    replacement = str(uuid.uuid4()) if field == "request_id" else envelope.issued_at + 1
    tampered = envelope.model_copy(update={field: replacement})

    _assert_safe_error(decryptor, tampered, expected_code="SUBMISSION_DECRYPTION_FAILED")


def test_failed_authentication_does_not_consume_request_id(
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    tampered = envelope.model_copy(update={
        "ciphertext": _alter_base64url(envelope.ciphertext),
    })

    _assert_safe_error(decryptor, tampered, expected_code="SUBMISSION_DECRYPTION_FAILED")
    assert decryptor.decrypt(envelope, expected_purpose=PURPOSE, now=2_000_000_000)[
        "api_secret"
    ] == SECRET


def test_wrong_algorithm_purpose_and_key_version_are_rejected_before_decryption(
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    wrong_algorithm = envelope.model_copy(update={"alg": "RSA1_5+A128CBC"})
    wrong_purpose = envelope.model_copy(update={
        "purpose": f"tenant:{TENANT_ID}:telegram_bot_config",
    })
    wrong_key = envelope.model_copy(update={"key_version": "submission-retired-v0"})

    _assert_safe_error(
        decryptor, wrong_algorithm, expected_code="SUBMISSION_ALGORITHM_UNSUPPORTED",
    )
    _assert_safe_error(decryptor, wrong_purpose, expected_code="SUBMISSION_PURPOSE_MISMATCH")
    key_error = _assert_safe_error(
        decryptor, wrong_key, expected_code="SUBMISSION_KEY_VERSION_UNAVAILABLE",
    )
    assert key_error.status_code == 409


@pytest.mark.parametrize(
    "expected_purpose",
    [
        "tenant:20000000-0000-0000-0000-000000000002:exchange_account_create",
        (
            f"tenant:{TENANT_ID}:exchange_credential_rotate:"
            "30000000-0000-0000-0000-000000000003"
        ),
    ],
)
def test_purpose_scope_prevents_cross_tenant_or_cross_resource_use(
    expected_purpose: str,
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])

    _assert_safe_error(
        decryptor,
        envelope,
        expected_code="SUBMISSION_PURPOSE_MISMATCH",
        expected_purpose=expected_purpose,
    )


@pytest.mark.parametrize(
    "issued_offset",
    [-(SUBMISSION_MAX_AGE_SECONDS + 1), SUBMISSION_MAX_FUTURE_SKEW_SECONDS + 1],
)
def test_expired_and_future_envelopes_are_rejected(
    issued_offset: int,
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    now = 2_000_000_000
    envelope = encrypt_submission_for_test(
        rsa_pair[1],
        key_version=KEY_VERSION,
        purpose=PURPOSE,
        request_id=str(uuid.uuid4()),
        issued_at=int((now + issued_offset) * 1_000),
        payload={"api_secret": SECRET},
    )

    error = _assert_safe_error(
        decryptor, envelope, expected_code="SUBMISSION_EXPIRED", now=now,
    )
    assert error.status_code == 409


@pytest.mark.parametrize("field", ["wrapped_key", "nonce", "ciphertext"])
def test_invalid_base64url_is_rejected_without_decoder_details(
    field: str,
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    invalid = envelope.model_copy(update={field: "!" * len(getattr(envelope, field))})

    error = _assert_safe_error(
        decryptor, invalid, expected_code="INVALID_SUBMISSION_ENVELOPE",
    )
    assert "base64" not in str(error).lower()


def test_decoded_ciphertext_over_limit_is_rejected(
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])
    oversized = envelope.model_copy(update={
        "ciphertext": _b64url(b"x" * (SUBMISSION_MAX_CIPHERTEXT_BYTES + 1)),
    })

    _assert_safe_error(decryptor, oversized, expected_code="INVALID_SUBMISSION_ENVELOPE")


def test_wire_model_rejects_oversized_encoded_fields(
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    envelope = _envelope(rsa_pair[1])

    for field, oversized in (
        ("wrapped_key", "A" * 1_025),
        ("ciphertext", "A" * 22_001),
        ("key_version", "v" * 81),
    ):
        with pytest.raises(ValidationError):
            SubmissionEnvelope.model_validate({
                **envelope.model_dump(),
                field: oversized,
            })


def test_reference_encryptor_rejects_oversized_plaintext(
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    with pytest.raises(ValueError, match="plaintext is too large"):
        _envelope(rsa_pair[1], payload={"api_secret": "s" * SUBMISSION_MAX_PLAINTEXT_BYTES})


@pytest.mark.parametrize(
    "inner_update",
    [
        {"purpose": f"tenant:{TENANT_ID}:telegram_bot_config"},
        {"request_id": str(uuid.uuid4())},
        {"issued_at": 2_000_000_000_001},
        {"payload": "not-an-object"},
        {"unexpected": True},
    ],
)
def test_inner_metadata_must_exactly_match_authenticated_outer_envelope(
    inner_update: dict[str, Any],
    decryptor: RsaOaepSubmissionDecryptor,
    rsa_pair: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> None:
    outer = _envelope(rsa_pair[1])
    document: dict[str, Any] = {
        "purpose": outer.purpose,
        "request_id": outer.request_id,
        "issued_at": outer.issued_at,
        "payload": {"api_secret": SECRET},
    }
    document.update(inner_update)
    forged = _encrypt_inner_document(rsa_pair[1], outer=outer, document=document)

    _assert_safe_error(decryptor, forged, expected_code="INVALID_SUBMISSION_PAYLOAD")


def test_node_forge_envelope_interoperates_with_python_decryptor(
    decryptor: RsaOaepSubmissionDecryptor,
) -> None:
    node = shutil.which("node")
    web_dir = Path(__file__).resolve().parents[2] / "web"
    if node is None or not (web_dir / "node_modules/node-forge").is_dir():
        pytest.skip("run npm ci in web/ to enable the node-forge interoperability test")

    issued_at = 2_000_000_000_000
    request_id = str(uuid.uuid4())
    producer_input = {
        "public_key_spki": decryptor.key_info().public_key_spki,
        "key_version": KEY_VERSION,
        "purpose": PURPOSE,
        "request_id": request_id,
        "issued_at": issued_at,
        "payload": {"api_key": "forge-api-key", "api_secret": SECRET},
    }
    script = r"""
import fs from "node:fs";
import crypto from "node:crypto";
import forge from "node-forge";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const decode = (value) => Buffer.from(value.replace(/-/g, "+").replace(/_/g, "/"), "base64");
const encode = (value) => Buffer.from(value, "binary").toString("base64url");
const aad = Buffer.from(
  `gatescope-submission-v1\n${input.key_version}\n${input.purpose}\n${input.request_id}\n${input.issued_at}`,
  "utf8",
);
const plaintext = Buffer.from(JSON.stringify({
  purpose: input.purpose,
  request_id: input.request_id,
  issued_at: input.issued_at,
  payload: input.payload,
}), "utf8");
const publicKey = forge.pki.publicKeyFromAsn1(
  forge.asn1.fromDer(decode(input.public_key_spki).toString("binary")),
);
const dataKey = crypto.randomBytes(32);
const nonce = crypto.randomBytes(12);
const wrappedKey = publicKey.encrypt(dataKey.toString("binary"), "RSA-OAEP", {
  md: forge.md.sha256.create(),
  mgf1: { md: forge.md.sha256.create() },
  label: aad.toString("binary"),
});
const cipher = forge.cipher.createCipher("AES-GCM", dataKey.toString("binary"));
cipher.start({
  iv: nonce.toString("binary"),
  additionalData: aad.toString("binary"),
  tagLength: 128,
});
cipher.update(forge.util.createBuffer(plaintext.toString("binary"), "raw"));
if (!cipher.finish()) throw new Error("forge AES-GCM encryption failed");
const ciphertext = cipher.output.getBytes() + cipher.mode.tag.getBytes();
process.stdout.write(JSON.stringify({
  alg: "RSA-OAEP-256+A256GCM",
  key_version: input.key_version,
  request_id: input.request_id,
  issued_at: input.issued_at,
  purpose: input.purpose,
  wrapped_key: encode(wrappedKey),
  nonce: nonce.toString("base64url"),
  ciphertext: encode(ciphertext),
}));
"""
    produced = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        cwd=web_dir,
        input=json.dumps(producer_input),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr
    envelope = SubmissionEnvelope.model_validate_json(produced.stdout)

    assert decryptor.decrypt(
        envelope, expected_purpose=PURPOSE, now=2_000_000_000,
    ) == {"api_key": "forge-api-key", "api_secret": SECRET}
