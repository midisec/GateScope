from __future__ import annotations

import base64
import os
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gatescope.adapters.gate_credentials import GateProbeResult
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_exchange_accounts import PostgresExchangeAccountRepository
from gatescope.application.credential_validation import CredentialValidationMonitor
from gatescope.credential_crypto import (
    CredentialMaterial,
    RsaOaepCredentialDecryptor,
    RsaOaepCredentialEncryptor,
)
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, seed_two_tenants


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


class SuccessfulReadProbe:
    def probe(self, environment: str, material: CredentialMaterial) -> GateProbeResult:
        assert environment == "TESTNET"
        assert material.api_key == "integration-key-123456"
        assert material.api_secret == "integration-secret-123456"
        return GateProbeResult(
            {
                "schema_version": 1,
                "account_read": True,
                "futures_usdt_read": True,
                "trade_permission": "UNVERIFIED",
                "withdrawal_permission": "UNVERIFIED",
                "trading_ready": False,
            },
            ("API_PERMISSION_SCOPE_UNVERIFIED",),
        )


def _crypto_pair() -> tuple[RsaOaepCredentialEncryptor, RsaOaepCredentialDecryptor]:
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
        RsaOaepCredentialEncryptor.from_base64_der(base64.urlsafe_b64encode(public_der).decode(), "it-v1"),
        RsaOaepCredentialDecryptor.from_base64_der(base64.urlsafe_b64encode(private_der).decode(), "it-v1"),
    )


def test_encrypted_admission_worker_validation_and_role_separation(migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresExchangeAccountRepository(pool)
        account, _ = repository.create_account(
            TENANT_A,
            USER_A,
            alias="Gate integration testnet",
            material=CredentialMaterial("integration-key-123456", "integration-secret-123456"),
            encryptor=encryptor,
            no_withdrawal_attested=True,
        )
        assert account.status == "PENDING_VALIDATION"
        assert not account.trading_ready
        worker_id = "integration-validator"
        worker_subject = uuid5(NAMESPACE_URL, worker_id)
        cycle = CredentialValidationMonitor(
            repository,
            decryptor,
            SuccessfulReadProbe(),  # type: ignore[arg-type]
            worker_id=worker_id,
            worker_subject_id=worker_subject,
        ).run_once()
        assert cycle.succeeded == 1
        validated = repository.get_account(TENANT_A, USER_A, account.id)
        assert validated.status == "READ_VALIDATED"
        assert validated.capabilities["account_read"] is True
        assert validated.capabilities["trade_permission"] == "UNVERIFIED"
        assert not validated.trading_ready
    finally:
        pool.close()

    with psycopg.connect(migrated_database) as connection:
        stored = connection.execute(
            "SELECT ciphertext FROM secret_credentials WHERE exchange_account_id=%s", (account.id,),
        ).fetchone()
        assert stored is not None
        assert b"integration-key" not in bytes(stored[0])
        connection.execute("SET ROLE gatescope_api")
        connection.commit()
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
            connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
            assert connection.execute("SELECT count(*) FROM exchange_accounts").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT ciphertext FROM secret_credentials").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="cannot assert exchange validation"):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
                connection.execute(
                    "UPDATE exchange_accounts SET status='READ_VALIDATED' WHERE id=%s", (account.id,),
                )

    with psycopg.connect(migrated_database) as connection:
        connection.execute("SET ROLE gatescope_executor")
        connection.commit()
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_B),))
            connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
            assert connection.execute("SELECT count(*) FROM secret_credentials").fetchone()[0] == 0
