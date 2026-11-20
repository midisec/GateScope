from __future__ import annotations

import base64
from datetime import datetime, timezone

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gatescope.adapters.coinglass import CoinglassRateLimited, CoinglassUnavailable, ProviderResponse
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_coinglass import PostgresCoinglassRepository
from gatescope.application.coinglass_runtime import CoinglassTestRuntime
from gatescope.credential_crypto import RsaOaepCredentialDecryptor, RsaOaepCredentialEncryptor
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, seed_two_tenants


API_KEY = "cg_" + "a" * 64


def _crypto_pair() -> tuple[RsaOaepCredentialEncryptor, RsaOaepCredentialDecryptor]:
    private = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
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
        RsaOaepCredentialEncryptor.from_base64_der(
            base64.urlsafe_b64encode(public_der).decode(),
            "coinglass-it-v1",
        ),
        RsaOaepCredentialDecryptor.from_base64_der(
            base64.urlsafe_b64encode(private_der).decode(),
            "coinglass-it-v1",
        ),
    )


class SuccessfulClient:
    def __init__(self, api_key: str) -> None:
        assert api_key == API_KEY

    def set_request_hooks(self, **hooks) -> None:
        self.hooks = hooks

    def configure_rate_limits(self, account: dict) -> None:
        assert isinstance(account, dict)

    def close(self) -> None:
        pass

    @staticmethod
    def get_coins_markets(*, per_page: int) -> ProviderResponse:
        assert per_page == 10
        return ProviderResponse([{"symbol": "BTC"}], None, datetime.now(timezone.utc), 11)

    @staticmethod
    def get_lsr_signal(symbol: str, *, mode: str) -> ProviderResponse:
        assert (symbol, mode) == ("BTC", "trader")
        return ProviderResponse({"signal": "neutral"}, None, datetime.now(timezone.utc), 12)

    @staticmethod
    def get_account(*, force: bool) -> ProviderResponse:
        assert force
        return ProviderResponse(
            {"permissions": ["lsr"], "api_key": "must-not-persist"},
            None,
            datetime.now(timezone.utc),
            13,
        )


def test_encrypted_web_config_is_tested_by_market_worker(
    migrated_database: str,
) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresCoinglassRepository(pool)
        config, queued = repository.save_and_enqueue(
            TENANT_A,
            USER_A,
            api_key=API_KEY,
            test_symbol="btc",
            encryptor=encryptor,
        )
        assert config.status == "PENDING_TEST"
        assert queued.status == "QUEUED"
        assert repository.latest_test(TENANT_A, USER_A).id == queued.id
        assert repository.latest_test(TENANT_B, USER_A) is None
        runtime = CoinglassTestRuntime(
            repository,
            decryptor,
            "market-integration",
            client_factory=SuccessfulClient,  # type: ignore[arg-type]
        )
        assert runtime.run_one()
        completed = repository.get_test(TENANT_A, USER_A, queued.id)
        assert completed.status == "SUCCEEDED"
        assert completed.result["checks"]["coins_markets"]["http_status"] == 200
        assert completed.result["checks"]["lsr"]["business_validation"] == "SCHEMA_PASSED"
        assert "api_key" not in completed.result["checks"]["account"]["metadata"]
        active = repository.get(TENANT_A, USER_A)
        assert active is not None and active.status == "ACTIVE"
        assert repository.latest_test(TENANT_A, USER_A).result == completed.result
    finally:
        pool.close()

    with psycopg.connect(migrated_database) as connection:
        stored = connection.execute(
            "SELECT secret_ciphertext FROM coinglass_configs WHERE tenant_id=%s",
            (TENANT_A,),
        ).fetchone()
        assert stored is not None and API_KEY.encode() not in bytes(stored[0])

        connection.execute("SET ROLE gatescope_api")
        connection.commit()
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_B),))
            connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
            assert connection.execute("SELECT count(*) FROM coinglass_configs").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "SELECT * FROM gatescope_security.claim_coinglass_test_job('api-must-not-claim')"
                ).fetchone()

    with psycopg.connect(migrated_database) as connection:
        connection.execute("SET ROLE gatescope_market")
        connection.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
                connection.execute("SELECT set_config('app.subject_id', %s, true)", (str(USER_A),))
                connection.execute(
                    "UPDATE coinglass_configs SET secret_ciphertext='\\x00' WHERE tenant_id=%s",
                    (TENANT_A,),
                )


@pytest.mark.parametrize("failure", [CoinglassRateLimited(60), CoinglassUnavailable("network unavailable")])
def test_transient_validation_preserves_active_capabilities_and_rotation_is_fail_closed(
    migrated_database: str,
    failure,
) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresCoinglassRepository(pool)
        initial, _ = repository.save_and_enqueue(
            TENANT_A, USER_A, api_key=API_KEY, test_symbol="btc", encryptor=encryptor
        )
        successful = CoinglassTestRuntime(
            repository, decryptor, "successful", client_factory=SuccessfulClient
        )
        assert successful.run_one()
        active = repository.get(TENANT_A, USER_A)
        assert active.status == "ACTIVE" and active.capability

        class FailingClient(SuccessfulClient):
            lsr_calls = 0

            @staticmethod
            def get_account(*, force):
                # An incomplete test must not replace validated capabilities with
                # this partial/changed account response.
                return ProviderResponse({"permissions": []}, None, datetime.now(timezone.utc), 1)

            @staticmethod
            def get_coins_markets(*, per_page):
                raise failure

            @classmethod
            def get_lsr_signal(cls, symbol, *, mode):
                cls.lsr_calls += 1
                return super().get_lsr_signal(symbol, mode=mode)

        queued = repository.enqueue_test(TENANT_A, USER_A, test_symbol="btc")
        unsuccessful = CoinglassTestRuntime(repository, decryptor, "transient", client_factory=FailingClient)
        assert unsuccessful.run_one()
        done = repository.get_test(TENANT_A, USER_A, queued.id)
        unchanged = repository.get(TENANT_A, USER_A)
        assert done.status == "FAILED" and done.result["connected"] is False
        assert done.result["checks"]["lsr"]["business_validation"] == "NOT_RUN"
        assert FailingClient.lsr_calls == 0
        assert unchanged.status == "ACTIVE" and unchanged.capability == active.capability
        assert unchanged.last_safe_error_code == failure.code

        rotated, _ = repository.save_and_enqueue(
            TENANT_A, USER_A, api_key=API_KEY, test_symbol="btc", encryptor=encryptor
        )
        assert rotated.secret_version == initial.secret_version + 1
        assert unsuccessful.run_one()
        pending = repository.get(TENANT_A, USER_A)
        assert pending.status == "PENDING_TEST" and pending.capability == {}
        successful.close()
        unsuccessful.close()
    finally:
        pool.close()


@pytest.mark.parametrize("http_status", [401, 403])
def test_explicit_auth_failure_does_not_preserve_active_configuration(
    migrated_database: str, http_status: int
) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresCoinglassRepository(pool)
        repository.save_and_enqueue(TENANT_A, USER_A, api_key=API_KEY, test_symbol="btc", encryptor=encryptor)
        runtime = CoinglassTestRuntime(repository, decryptor, "active", client_factory=SuccessfulClient)
        assert runtime.run_one()

        class UnauthorizedClient(SuccessfulClient):
            @staticmethod
            def get_account(*, force):
                raise CoinglassUnavailable("authentication rejected", http_status=http_status)

        repository.enqueue_test(TENANT_A, USER_A, test_symbol="btc")
        invalid = CoinglassTestRuntime(repository, decryptor, "rejected", client_factory=UnauthorizedClient)
        assert invalid.run_one()
        config = repository.get(TENANT_A, USER_A)
        assert config.status == "VALIDATION_FAILED" and config.capability == {}
        runtime.close()
        invalid.close()
    finally:
        pool.close()


def test_old_validation_completion_cannot_activate_rotated_credentials(migrated_database: str) -> None:
    seed_two_tenants(migrated_database)
    encryptor, decryptor = _crypto_pair()
    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresCoinglassRepository(pool)
        old, _ = repository.save_and_enqueue(
            TENANT_A, USER_A, api_key=API_KEY, test_symbol="btc", encryptor=encryptor
        )
        claimed, _ = repository.claim_test("old-version", decryptor)
        assert claimed.owner_user_id == USER_A
        rotated, _ = repository.save_and_enqueue(
            TENANT_A, USER_A, api_key=API_KEY, test_symbol="btc", encryptor=encryptor
        )
        repository.complete_test(
            TENANT_A,
            claimed.id,
            expected_version=old.secret_version,
            success=True,
            result={"checks": {"account": {"metadata": {"permissions": ["lsr"]}}}},
            safe_error_code=None,
        )
        final = repository.get(TENANT_A, USER_A)
        assert final.secret_version == rotated.secret_version and final.status == "PENDING_TEST"
        assert final.capability == {}
    finally:
        pool.close()
