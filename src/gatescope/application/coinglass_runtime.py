"""Bounded Coinglass/LSR connectivity checks for the market workload."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from gatescope.adapters.coinglass import (
    COINGLASS_ORIGIN,
    MAX_RETRY_AFTER_SECONDS,
    CoinglassClient,
    CoinglassError,
    CoinglassRateLimited,
    ProviderResponse,
    safe_provider_metadata,
)
from gatescope.adapters.postgres_coinglass import PostgresCoinglassRepository
from gatescope.adapters.postgres_coinglass_context import PostgresCoinglassContextRepository
from gatescope.credential_crypto import RsaOaepCredentialDecryptor


@dataclass
class _TestIdentity:
    client: CoinglassClient
    account: ProviderResponse | None = None
    quota_at: float | None = None


class CoinglassTestRuntime:
    MAX_IDENTITIES = 8
    QUOTA_SECONDS = 300

    def __init__(
        self,
        repository: PostgresCoinglassRepository,
        decryptor: RsaOaepCredentialDecryptor,
        worker_id: str,
        *,
        client_factory: Callable[[str], CoinglassClient] = CoinglassClient,
        provider_repository: PostgresCoinglassContextRepository | None = None,
    ) -> None:
        self.repository = repository
        self.decryptor = decryptor
        self.worker_id = worker_id
        self.client_factory = client_factory
        self.provider_repository = provider_repository or PostgresCoinglassContextRepository(repository.pool)
        self.identities: OrderedDict[tuple, _TestIdentity] = OrderedDict()

    def close(self) -> None:
        while self.identities:
            self.identities.popitem(last=False)[1].client.close()

    def _state(self, job, api_key: str) -> _TestIdentity:
        identity = (job.tenant_id, job.config_id, job.config_secret_version)
        for old in list(self.identities):
            if old[:2] == identity[:2] and old != identity:
                self.identities.pop(old).client.close()
        state = self.identities.get(identity)
        if state is None:
            state = _TestIdentity(self.client_factory(api_key))
            self.identities[identity] = state
            while len(self.identities) > self.MAX_IDENTITIES:
                self.identities.popitem(last=False)[1].client.close()
        self.identities.move_to_end(identity)
        owner = (job.tenant_id, job.owner_user_id, job.config_id, job.config_secret_version)

        def before_request(_path):
            waiting = self.provider_repository.provider_wait(*owner)
            if waiting:
                raise CoinglassRateLimited(waiting)

        state.client.set_request_hooks(
            before_request=before_request,
            on_rate_limit=lambda _path, seconds: self.provider_repository.pause_provider(*owner, seconds),
        )
        return state

    def run_one(self) -> bool:
        claimed = self.repository.claim_test(self.worker_id, self.decryptor)
        if claimed is None:
            return False
        job, api_key = claimed
        state = self._state(job, api_key)
        client = state.client
        checks: dict[str, dict[str, Any]] = {}
        retry_after: int | None = None
        halted = False
        owner = (job.tenant_id, job.owner_user_id, job.config_id, job.config_secret_version)

        def account():
            now = time.monotonic()
            if state.account is None or state.quota_at is None or now - state.quota_at >= self.QUOTA_SECONDS:
                response = client.get_account(force=True)
                client.configure_rate_limits(response.data)
                state.account, state.quota_at = response, now
            return state.account

        for name, operation in (
            ("account", account),
            ("coins_markets", lambda: client.get_coins_markets(per_page=10)),
            ("lsr", lambda: client.get_lsr_signal(job.test_symbol, mode="trader")),
        ):
            if halted:
                checks[name] = {
                    "ok": False,
                    "http_status": None,
                    "business_validation": "NOT_RUN",
                    "safe_error_code": "DEFERRED_AFTER_PROVIDER_ERROR",
                }
                continue
            try:
                waiting = self.provider_repository.provider_wait(*owner)
                if waiting:
                    raise CoinglassRateLimited(waiting)
                response = operation()
                checks[name] = self._success(name, response)
            except CoinglassRateLimited as exc:
                retry_after = min(MAX_RETRY_AFTER_SECONDS, max(retry_after or 0, 1, exc.retry_after_seconds))
                waiting = self.provider_repository.provider_wait(*owner)
                if waiting > 0:
                    retry_after = waiting
                else:
                    self.provider_repository.pause_provider(*owner, retry_after)
                halted = True
                checks[name] = {
                    "ok": False,
                    "http_status": 429,
                    "business_validation": "NOT_RUN",
                    "safe_error_code": exc.code,
                    "retry_after_seconds": retry_after,
                }
            except CoinglassError as exc:
                halted = True
                checks[name] = {
                    "ok": False,
                    "http_status": exc.http_status,
                    "business_validation": "FAILED",
                    "safe_error_code": exc.code,
                }
            except (TypeError, ValueError):
                halted = True
                checks[name] = {
                    "ok": False,
                    "http_status": None,
                    "business_validation": "FAILED",
                    "safe_error_code": "PROVIDER_TEST_INVALID_RESPONSE",
                }
        success = all(check.get("ok") is True for check in checks.values())
        error_code = None
        if not success:
            error_code = next(
                (str(check["safe_error_code"]) for check in checks.values() if check.get("safe_error_code")),
                "PROVIDER_TEST_FAILED",
            )
        self.repository.complete_test(
            job.tenant_id,
            job.id,
            expected_version=job.config_secret_version,
            success=success,
            result={
                "connected": success,
                "origin": COINGLASS_ORIGIN,
                "symbol": job.test_symbol,
                "checks": checks,
                "automatic_feature_eligible": False,
                "automatic_feature_reason": "PENDING_LICENSE_AND_POINT_IN_TIME_EVIDENCE",
            },
            safe_error_code=error_code,
            retry_after_seconds=retry_after,
        )
        return True

    @staticmethod
    def _success(name: str, response: ProviderResponse) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "http_status": 200,
            "business_validation": "PASSED",
            "elapsed_ms": response.elapsed_ms,
            "received_at": _iso(response.received_at),
        }
        if name == "lsr":
            result["business_validation"] = "SCHEMA_PASSED"
        if name == "coins_markets":
            result["items"] = len(response.data)
        elif name == "lsr":
            result["fields"] = sorted(str(key) for key in response.data.keys())[:40]
        elif name == "account":
            result["metadata"] = safe_provider_metadata(response.data)
        return result


def _iso(value: datetime) -> str:
    return value.isoformat()
