from __future__ import annotations

import base64
import hashlib
import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_telegram_config import (
    PostgresTelegramConfigRepository,
    TelegramConfigNotFound,
)
from gatescope.credential_crypto import RsaOaepCredentialDecryptor, RsaOaepCredentialEncryptor
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, seed_two_tenants


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def _crypto_pair() -> tuple[RsaOaepCredentialEncryptor, RsaOaepCredentialDecryptor]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_der = private_key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        RsaOaepCredentialEncryptor.from_base64_der(
            base64.urlsafe_b64encode(public_der).decode(),
            "telegram-integration-v1",
        ),
        RsaOaepCredentialDecryptor.from_base64_der(
            base64.urlsafe_b64encode(private_der).decode(),
            "telegram-integration-v1",
        ),
    )


def test_telegram_bot_config_encryption_lookup_isolation_and_disable(
    migrated_database: str,
) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    token = "123456789:" + "T" * 35
    webhook_id = "W" * 32
    webhook_secret = "S" * 32
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresTelegramConfigRepository(pool)
        saved = repository.save(
            TENANT_A,
            USER_A,
            bot_username="gatescope_test_bot",
            bot_token=token,
            allowed_user_ids=(),
            webhook_id=webhook_id,
            webhook_secret=webhook_secret,
            encryptor=encryptor,
            status="ACTIVE",
            last_safe_error_code=None,
        )
        assert saved.configured
        assert saved.bot_username == "gatescope_test_bot"
        assert saved.allowed_user_ids == ()
        assert saved.token_fingerprint.startswith("SHA256:")
        assert token not in repr(saved)
        assert webhook_secret not in repr(saved)
        assert repository.get(TENANT_B, USER_B) is None

        webhook = repository.resolve_webhook(webhook_id)
        assert webhook is not None
        assert webhook.tenant_id == TENANT_A
        assert webhook.allowed_user_ids == frozenset()
        assert webhook.accepts_secret(webhook_secret)
        assert not webhook.accepts_secret("X" * 32)
        assert repository.resolve_webhook("X" * 32) is None
        assert repository.delivery_token(TENANT_A, uuid4(), decryptor) == token

        with psycopg.connect(migrated_database) as connection:
            stored = connection.execute(
                """SELECT secret_ciphertext,wrapped_data_key,webhook_secret_hash,
                          encryption_context,token_fingerprint
                     FROM telegram_bot_configs WHERE tenant_id=%s""",
                (TENANT_A,),
            ).fetchone()
            assert stored is not None
            assert token.encode() not in bytes(stored[0])
            assert webhook_secret.encode() not in bytes(stored[0])
            assert token.encode() not in bytes(stored[1])
            assert webhook_secret.encode() not in bytes(stored[1])
            assert stored[2] == hashlib.sha256(webhook_secret.encode()).hexdigest()
            assert token not in str(stored[3])
            assert webhook_secret not in str(stored[3])
            assert token not in str(stored[4])

        with psycopg.connect(migrated_database) as connection:
            connection.execute("SET ROLE gatescope_api")
            connection.commit()
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_B),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_B),))
                tenant_b_count = connection.execute(
                    "SELECT count(*) FROM telegram_bot_configs",
                ).fetchone()
                assert tenant_b_count is not None and tenant_b_count[0] == 0
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
                tenant_a_count = connection.execute(
                    "SELECT count(*) FROM telegram_bot_configs",
                ).fetchone()
                assert tenant_a_count is not None and tenant_a_count[0] == 1

        repository.disable(TENANT_A, USER_A)
        disabled = repository.get(TENANT_A, USER_A)
        assert disabled is not None and disabled.status == "DISABLED"
        assert repository.resolve_webhook(webhook_id) is None
        with pytest.raises(TelegramConfigNotFound):
            repository.delivery_token(TENANT_A, uuid4(), decryptor)
        with pytest.raises(TelegramConfigNotFound):
            repository.disable(TENANT_A, USER_A)
    finally:
        pool.close()
