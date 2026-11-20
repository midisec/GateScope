from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest

from gatescope.adapters.coinglass import CoinglassRateLimited
from gatescope.application.coinglass_chart_runtime import CoinglassChartRuntime, _refresh_delay


def runtime():
    tenant, user, config = uuid4(), uuid4(), uuid4()
    job = {
        "tenant": tenant,
        "user": user,
        "instrument": {
            "id": uuid4(),
            "venue": "GATE",
            "market": "FUTURES_USDT",
            "symbol": "BTC_USDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "settle_asset": "USDT",
        },
        "timeframe": "1m",
        "config": (
            config,
            1,
            b"cipher",
            b"0" * 12,
            b"wrapped",
            "v1",
            f"gatescope:v1:tenant:{tenant}:coinglass-config:{config}:secret:1",
        ),
    }
    repo = Mock()
    repo.claim.return_value = job
    repo.provider_wait.return_value = 0
    decryptor = Mock()
    decryptor.reveal.return_value.api_key = "cg_" + "x" * 64
    return CoinglassChartRuntime(repo, decryptor), repo, decryptor


def test_runtime_uses_real_decryptor_contract_and_integer_clock(monkeypatch):
    worker, repo, decryptor = runtime()
    client = Mock()
    pairs = {"Gate": []}
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock(return_value=client)
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs", Mock(return_value=pairs)
    )
    fetch = Mock(return_value={"status": "PARTIAL", "series": []})
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    assert worker.run_one()
    assert isinstance(fetch.call_args.args[3], int)
    assert fetch.call_args.kwargs["funding_unit_status"] == "UNKNOWN"
    assert fetch.call_args.kwargs["skip_unverified_funding"] is True
    assert "request_spacing_seconds" not in fetch.call_args.kwargs
    decryptor.reveal.assert_called_once()
    assert repo.complete.call_args.kwargs["delay"] == 115
    repo.claim.return_value = None
    assert not worker.run_one()


def test_rate_limit_stops_until_retry_without_sleeping(monkeypatch):
    worker, repo, _ = runtime()
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs",
        Mock(side_effect=CoinglassRateLimited(600)),
    )
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock())
    assert worker.run_one()
    assert repo.complete.call_args.kwargs["delay"] == 600
    assert repo.complete.call_args.args[1]["reason"] == "PROVIDER_RATE_LIMITED"
    assert repo.complete.call_args.args[1]["status"] == "RATE_LIMITED"
    repo.pause_provider.assert_called_once()
    repo.claim.return_value = None
    assert not worker.run_one()


def test_bad_context_does_not_decrypt_or_disclose(monkeypatch):
    worker, repo, decryptor = runtime()
    config = list(repo.claim.return_value["config"])
    config[-1] = "invalid"
    repo.claim.return_value["config"] = tuple(config)
    assert worker.run_one()
    decryptor.reveal.assert_not_called()
    assert repo.complete.call_args.args[1] == {
        "status": "ERROR",
        "series": [],
        "reason": "COINGLASS_REFRESH_FAILED",
    }


@pytest.mark.parametrize(
    "identity",
    [
        {"market": "SPOT"},
        {"venue": "OTHER"},
        {"base_asset": None},
        {"symbol": ""},
    ],
)
def test_unsupported_local_identity_never_decrypts_or_requests_provider(monkeypatch, identity):
    worker, repo, decryptor = runtime()
    repo.claim.return_value["instrument"].update(identity)
    factory, catalog = Mock(), Mock()
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", factory)
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.get_supported_pairs", catalog)
    assert worker.run_one()
    decryptor.reveal.assert_not_called()
    factory.assert_not_called()
    catalog.assert_not_called()
    repo.provider_wait.assert_not_called()
    bundle = repo.complete.call_args.args[1]
    assert bundle["status"] == "UNSUPPORTED"
    assert bundle["series"] == []
    assert bundle["snapshot_id"]
    assert bundle["instrument_id"] == str(repo.claim.return_value["instrument"]["id"])
    assert repo.complete.call_args.kwargs["delay"] == 3600


def _new_job(job, *, rotate=False):
    changed = deepcopy(job)
    if not rotate:
        changed["tenant"], changed["user"] = uuid4(), uuid4()
    config = list(changed["config"])
    config[0] = config[0] if rotate else uuid4()
    config[1] = config[1] + 1 if rotate else 1
    config[-1] = f"gatescope:v1:tenant:{changed['tenant']}:coinglass-config:{config[0]}:secret:{config[1]}"
    changed["config"] = tuple(config)
    return changed


def test_identity_switch_keeps_client_catalog_and_account_quota(monkeypatch):
    worker, repo, decryptor = runtime()
    a = repo.claim.return_value
    b = _new_job(a)
    repo.claim.side_effect = [a, b, a]
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    clients = [Mock(), Mock()]
    for client in clients:
        client._get.return_value.data = {"rate_limits": {"coinglass": {"limit": 11, "window_seconds": 60}}}
    factory = Mock(side_effect=clients)
    catalog = Mock(return_value={"Gate": []})
    fetch = Mock(return_value={"status": "PARTIAL", "series": []})
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", factory)
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.get_supported_pairs", catalog)
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    for _ in range(3):
        assert worker.run_one()
        clock[0] += 60
    assert factory.call_count == decryptor.reveal.call_count == catalog.call_count == 2
    assert [call.args[0] for call in fetch.call_args_list] == [clients[0], clients[1], clients[0]]
    for client in clients:
        client._get.assert_called_once()
        client.configure_rate_limits.assert_called_once_with(client._get.return_value.data)
    hooks = clients[0].set_request_hooks.call_args.kwargs
    hooks["before_request"]("/api/futures/open-interest/history")
    assert repo.provider_wait.call_args.args == (a["tenant"], a["user"], a["config"][0], 1)
    hooks["on_rate_limit"]("/api/futures/open-interest/history", 42)
    assert repo.pause_provider.call_args.args[-1] == 42


def test_tenant_cooldown_does_not_pause_other_tenants(monkeypatch):
    worker, repo, _ = runtime()
    a = repo.claim.return_value
    b = _new_job(a)
    repo.claim.side_effect = [a, b, a]
    repo.provider_wait.side_effect = lambda tenant, *_: 600 if tenant == a["tenant"] else 0
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", factory)
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs", Mock(return_value={})
    )
    fetch = Mock(return_value={"status": "PARTIAL", "series": []})
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    for _ in range(3):
        assert worker.run_one()
        clock[0] += 6
    assert factory.call_count == fetch.call_count == 1
    assert [call.args[1]["status"] for call in repo.complete.call_args_list] == [
        "RATE_LIMITED",
        "PARTIAL",
        "RATE_LIMITED",
    ]


def test_lru_eviction_and_rotation_close_obsolete_clients(monkeypatch):
    worker, repo, _ = runtime()
    worker.MAX_IDENTITIES = 2
    a = repo.claim.return_value
    b = _new_job(a)
    c = _new_job(a)
    rotation = _new_job(c, rotate=True)
    repo.claim.side_effect = [a, b, a, c, rotation]
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    clients = [Mock() for _ in range(4)]
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock(side_effect=clients)
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs", Mock(return_value={})
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.fetch_chart",
        Mock(return_value={"status": "PARTIAL", "series": []}),
    )
    for _ in range(5):
        assert worker.run_one()
        clock[0] += 60
    clients[0].close.assert_not_called()
    clients[1].close.assert_called_once()
    clients[2].close.assert_called_once()
    clients[3].close.assert_not_called()
    assert len(worker.identities) == 2
    worker.close()
    assert not worker.identities
    clients[0].close.assert_called_once()
    clients[3].close.assert_called_once()


def test_ready_jobs_do_not_idle_behind_an_additional_batch_budget(monkeypatch):
    worker, repo, _ = runtime()
    a = repo.claim.return_value
    b = _new_job(a)
    repo.claim.side_effect = [a, a, b]
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    clients = [Mock(), Mock()]
    for client in clients:
        client._get.return_value.data = {"rate_limits": {"coinglass": {"limit": 11, "window_seconds": 60}}}
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock(side_effect=clients)
    )
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs", Mock(return_value={})
    )
    fetch = Mock(return_value={"status": "PARTIAL", "series": []})
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    assert worker.run_one()
    clock[0] = 106.0
    assert worker.run_one()
    assert repo.complete.call_count == fetch.call_count == 2
    repo.defer.assert_not_called()
    clock[0] = 112.0
    assert worker.run_one()
    assert repo.complete.call_count == fetch.call_count == 3
    # This replaces the old redundant 54.5 second batch throttle. Actual calls
    # remain gated inside CoinglassClient by the separately tested shared pacer.
    assert clients[0]._get.call_count == clients[1]._get.call_count == 1


def test_long_retry_after_is_not_shortened_to_one_hour(monkeypatch):
    worker, repo, _ = runtime()
    repo.provider_wait.return_value = 9000
    client_factory = Mock()
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", client_factory)
    assert worker.run_one()
    client_factory.assert_not_called()
    repo.pause_provider.assert_not_called()
    assert repo.complete.call_args.kwargs["delay"] == 9000
    assert repo.complete.call_args.args[1]["retry_after_seconds"] == 9000


def test_runtime_reuses_existing_deadline_instead_of_restarting_retry_after():
    worker, repo, _ = runtime()
    repo.provider_wait.return_value = 55
    assert worker._pause(repo.claim.return_value, 60) == 55
    repo.pause_provider.assert_not_called()


def test_runtime_publishes_progress_with_the_original_job_lease(monkeypatch):
    worker, repo, _ = runtime()
    original = repo.claim.return_value
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock())
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.get_supported_pairs", Mock(return_value={})
    )
    partial = {
        "status": "PARTIAL",
        "scope": {"symbol": "BTC_USDT"},
        "series": [{"id": "oi", "points": [{"value": 1}]}],
    }

    def fetch(*args, **kwargs):
        kwargs["on_progress"](partial)
        return {"status": "PARTIAL", "series": []}

    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    assert worker.run_one()
    repo.publish_progress.assert_called_once_with(original, partial)
    repo.complete.assert_called_once()


@pytest.mark.parametrize("timeframe, ttl", [("1m", 120), ("5m", 300), ("15m", 600), ("1h", 600)])
def test_refresh_eligibility_precedes_cache_ttl(timeframe, ttl):
    assert _refresh_delay({"status": "PARTIAL", "series": []}, timeframe, 1000) == ttl - 5


def test_refresh_uses_first_ready_metric_expiry_and_ignores_old_retained_expiry():
    bundle = {
        "expires_at": 1600,
        "series": [
            {"status": "READY", "points": [{"value": 1}], "unit_status": "VERIFIED", "expires_at": 1100},
            {"status": "READY", "points": [{"value": 2}], "unit_status": "VERIFIED", "expires_at": 1110},
            {"status": "STALE", "points": [{"value": 3}], "unit_status": "VERIFIED", "expires_at": 950},
        ],
    }
    assert _refresh_delay(bundle, "15m", 1000) == 95
    assert _refresh_delay(bundle, "15m", 1098) == 1


def test_refresh_legacy_falls_back_to_original_bundle_expiry():
    assert _refresh_delay({"expires_at": 1090, "series": []}, "15m", 1000) == 85


def _mock_rate_limit_provider(monkeypatch, fetch):
    client = Mock()
    monkeypatch.setattr(
        "gatescope.application.coinglass_chart_runtime.CoinglassClient", Mock(return_value=client)
    )
    catalog = Mock(return_value={"Gate": []})
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.get_supported_pairs", catalog)
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.fetch_chart", fetch)
    return client, catalog


@pytest.mark.parametrize("retry, expected", [(10, 30), (61, 61), (9000, 9000)])
def test_rate_limited_partial_retries_missing_data_without_waiting_for_normal_ttl(
    monkeypatch, retry, expected
):
    worker, repo, _ = runtime()
    repo.claim.return_value["timeframe"] = "8h"
    partial = {"status": "PARTIAL", "series": [], "retry_after_seconds": retry}
    fetch = Mock(return_value=partial)
    _mock_rate_limit_provider(monkeypatch, fetch)
    assert worker.run_one()
    fetch.assert_called_once()
    assert repo.complete.call_args.args[1] is partial
    assert repo.complete.call_args.kwargs["delay"] == expected
    assert repo.pause_provider.call_args.args[-1] == retry


@pytest.mark.parametrize("phase", ["account", "catalog", "chart"])
def test_initial_rate_limit_exception_uses_retry_backoff_not_long_timeframe_ttl(monkeypatch, phase):
    worker, repo, _ = runtime()
    repo.claim.return_value["timeframe"] = "8h"
    fetch = Mock(return_value={"status": "PARTIAL", "series": []})
    client, catalog = _mock_rate_limit_provider(monkeypatch, fetch)
    target = {"account": client._get, "catalog": catalog, "chart": fetch}[phase]
    target.side_effect = CoinglassRateLimited(10)
    assert worker.run_one()
    target.assert_called_once()
    assert repo.complete.call_args.kwargs["delay"] == 30
    assert repo.complete.call_args.args[1]["retry_after_seconds"] == 10


def test_consecutive_rate_limits_back_off_with_ceiling_and_reset_after_success(monkeypatch):
    worker, repo, _ = runtime()
    repo.claim.return_value["timeframe"] = "8h"
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    limited = {"status": "PARTIAL", "series": [], "retry_after_seconds": 10}
    fetch = Mock(
        side_effect=[deepcopy(limited) for _ in range(6)]
        + [
            {"status": "PARTIAL", "series": []},
            limited,
        ]
    )
    _mock_rate_limit_provider(monkeypatch, fetch)
    for _ in range(8):
        assert worker.run_one()
        clock[0] += 400
    assert [call.kwargs["delay"] for call in repo.complete.call_args_list] == [
        30,
        60,
        120,
        240,
        300,
        300,
        595,
        30,
    ]
    assert fetch.call_count == 8  # Retry is scheduled, never an internal request loop.


def test_rate_limit_backoff_is_identity_scoped(monkeypatch):
    worker, repo, _ = runtime()
    a = repo.claim.return_value
    b = _new_job(a)
    repo.claim.side_effect = [a, b, a]
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    fetch = Mock(return_value={"status": "PARTIAL", "series": [], "retry_after_seconds": 10})
    _mock_rate_limit_provider(monkeypatch, fetch)
    for _ in range(3):
        assert worker.run_one()
        clock[0] += 60
    assert [call.kwargs["delay"] for call in repo.complete.call_args_list] == [30, 30, 60]


def test_observing_existing_cooldown_does_not_count_as_another_rate_limit_failure(monkeypatch):
    worker, repo, decryptor = runtime()
    clock = [100.0]
    monkeypatch.setattr("gatescope.application.coinglass_chart_runtime.time.monotonic", lambda: clock[0])
    fetch = Mock(return_value={"status": "PARTIAL", "series": [], "retry_after_seconds": 10})
    _mock_rate_limit_provider(monkeypatch, fetch)
    assert worker.run_one()
    clock[0] = 101.0
    repo.provider_wait.return_value = 9
    assert worker.run_one()
    fetch.assert_called_once()
    decryptor.reveal.assert_called_once()
    repo.pause_provider.assert_called_once()  # Existing Retry-After is not re-anchored.
    clock[0] = 200.0
    repo.provider_wait.return_value = 0
    assert worker.run_one()
    assert fetch.call_count == 2
    assert [call.kwargs["delay"] for call in repo.complete.call_args_list] == [30, 30, 60]
