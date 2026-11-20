from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from datetime import datetime, timezone

import pytest

from gatescope.adapters.coinglass import CoinglassRateLimited, CoinglassUnavailable, ProviderResponse
from gatescope.adapters.postgres_coinglass import _transient_failure
from gatescope.application.coinglass_runtime import CoinglassTestRuntime


def job(*, tenant=None, config=None, version=1):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant or uuid4(),
        owner_user_id=uuid4(),
        config_id=config or uuid4(),
        config_secret_version=version,
        test_symbol="BTC",
    )


def client():
    result = Mock()
    now = datetime.now(timezone.utc)
    result.get_account.return_value = ProviderResponse(
        {"permissions": ["lsr"], "rate_limits": {"coinglass": {"limit": 11, "window_seconds": 60}}},
        None,
        now,
        1,
    )
    result.get_coins_markets.return_value = ProviderResponse([{"symbol": "BTC"}], None, now, 1)
    result.get_lsr_signal.return_value = ProviderResponse({"signal": "neutral"}, None, now, 1)
    return result


def setup(jobs, clients):
    repository = Mock()
    repository.claim_test.side_effect = [(item, "cg_" + "x" * 64) for item in jobs]
    provider = Mock()
    provider.provider_wait.return_value = 0
    factory = Mock(side_effect=clients)
    runtime = CoinglassTestRuntime(
        repository, Mock(), "test", client_factory=factory, provider_repository=provider
    )
    return runtime, repository, provider, factory


def test_account_first_configures_pacing_and_429_defers_remaining_calls():
    identity = job()
    http = client()
    http.get_coins_markets.side_effect = CoinglassRateLimited(600)
    runtime, repo, provider, _ = setup([identity], [http])
    assert runtime.run_one()
    http.configure_rate_limits.assert_called_once_with(http.get_account.return_value.data)
    http.get_lsr_signal.assert_not_called()
    checks = repo.complete_test.call_args.kwargs["result"]["checks"]
    assert list(checks) == ["account", "coins_markets", "lsr"]
    assert checks["coins_markets"]["http_status"] == 429
    assert checks["lsr"]["business_validation"] == "NOT_RUN"
    assert repo.complete_test.call_args.kwargs["safe_error_code"] == "PROVIDER_RATE_LIMITED"
    provider.pause_provider.assert_called_once_with(
        identity.tenant_id, identity.owner_user_id, identity.config_id, 1, 600
    )
    hooks = http.set_request_hooks.call_args.kwargs
    hooks["before_request"]("/api/lsr/signal/BTC")
    provider.provider_wait.assert_called_with(
        identity.tenant_id, identity.owner_user_id, identity.config_id, 1
    )


def test_existing_cooldown_makes_no_http_calls_and_is_identity_scoped():
    a, b = job(), job()
    first, second = client(), client()
    runtime, repo, provider, _ = setup([a, b], [first, second])
    provider.provider_wait.side_effect = lambda tenant, *_: 300 if tenant == a.tenant_id else 0
    assert runtime.run_one()
    assert runtime.run_one()
    first.get_account.assert_not_called()
    first.get_coins_markets.assert_not_called()
    first.get_lsr_signal.assert_not_called()
    second.get_account.assert_called_once()
    second.get_coins_markets.assert_called_once()
    second.get_lsr_signal.assert_called_once()
    assert [call.kwargs["success"] for call in repo.complete_test.call_args_list] == [False, True]


def test_test_identity_lru_reuses_account_and_closes_rotation(monkeypatch):
    a, b = job(), job()
    rotated = job(tenant=a.tenant_id, config=a.config_id, version=2)
    first, second, third = client(), client(), client()
    runtime, _, _, factory = setup([a, b, a, rotated], [first, second, third])
    monkeypatch.setattr("gatescope.application.coinglass_runtime.time.monotonic", lambda: 100.0)
    for _ in range(4):
        assert runtime.run_one()
    assert factory.call_count == 3
    first.get_account.assert_called_once()
    assert first.get_coins_markets.call_count == 2
    first.close.assert_called_once()
    second.close.assert_not_called()
    runtime.close()
    second.close.assert_called_once()
    third.close.assert_called_once()


def test_test_identity_cache_is_bounded_and_refreshes_account_only_on_expiry(monkeypatch):
    a, b, c = job(), job(), job()
    first, second, third = client(), client(), client()
    runtime, _, _, _ = setup([a, b, a, a, c], [first, second, third])
    runtime.MAX_IDENTITIES = 2
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_runtime.time.monotonic", lambda: clock[0])
    assert runtime.run_one()
    assert runtime.run_one()
    assert runtime.run_one()
    first.get_account.assert_called_once()
    clock[0] = 401.0
    assert runtime.run_one()
    assert first.get_account.call_count == 2
    assert runtime.run_one()
    assert len(runtime.identities) == 2
    second.close.assert_called_once()
    first.close.assert_not_called()


def test_business_or_security_failure_is_not_classified_as_transient():
    for code in ("PROVIDER_BUSINESS_ERROR", "PROVIDER_SECURITY_ERROR", "PROVIDER_TEST_INVALID_RESPONSE"):
        assert not _transient_failure(code, {"checks": {}})
    assert not _transient_failure("PROVIDER_UNAVAILABLE", {"checks": {"other": {"http_status": 401}}})


def test_test_long_retry_after_is_preserved():
    http = client()
    http.get_account.side_effect = CoinglassRateLimited(9000)
    runtime, repo, provider, _ = setup([job()], [http])
    assert runtime.run_one()
    assert repo.complete_test.call_args.kwargs["retry_after_seconds"] == 9000
    assert provider.pause_provider.call_args.args[-1] == 9000


def test_test_runtime_reuses_http_hooks_original_cooldown_deadline():
    http = client()
    http.get_account.side_effect = CoinglassRateLimited(60)
    runtime, repo, provider, _ = setup([job()], [http])
    # No pause before HTTP; the HTTP hook has persisted the pause when its
    # exception reaches the test worker five seconds later.
    provider.provider_wait.side_effect = [0, 55]
    assert runtime.run_one()
    provider.pause_provider.assert_not_called()
    assert repo.complete_test.call_args.kwargs["retry_after_seconds"] == 55
    assert repo.complete_test.call_args.kwargs["result"]["checks"]["account"]["retry_after_seconds"] == 55


@pytest.mark.parametrize(
    "status,expected", [(None, True), (429, True), (502, True), (401, False), (403, False), (404, False)]
)
def test_transient_errors_never_mask_explicit_authentication_failure(status, expected):
    assert (
        _transient_failure(
            "PROVIDER_UNAVAILABLE",
            {
                "checks": {
                    "account": {"ok": False, "http_status": status, "safe_error_code": "PROVIDER_UNAVAILABLE"}
                }
            },
        )
        is expected
    )


def test_network_failure_defers_later_calls_without_faking_connectivity():
    http = client()
    http.get_account.side_effect = CoinglassUnavailable("safe network error")
    runtime, repo, _, _ = setup([job()], [http])
    assert runtime.run_one()
    http.get_coins_markets.assert_not_called()
    http.get_lsr_signal.assert_not_called()
    args = repo.complete_test.call_args.kwargs
    assert args["success"] is False
    assert args["result"]["connected"] is False
    assert _transient_failure(args["safe_error_code"], args["result"])
