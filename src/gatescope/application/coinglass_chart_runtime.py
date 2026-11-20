"""Single-flight chart warming, separately scheduled from public market ingestion."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from gatescope.adapters.coinglass import MAX_RETRY_AFTER_SECONDS, CoinglassClient, CoinglassRateLimited
from gatescope.adapters.coinglass_chart import fetch_chart, get_supported_pairs
from gatescope.adapters.postgres_coinglass_context import PERIODS
from gatescope.credential_crypto import CredentialEnvelope
from gatescope.domain.coinglass_cache import epoch
from gatescope.domain.coinglass_context import match_instrument


@dataclass
class _IdentityState:
    client: CoinglassClient
    pairs: object = None
    pairs_at: float = 0.0
    quota_at: float | None = None
    cooldown_until: float = 0.0
    rate_limit_backoff: int = 0


class CoinglassChartRuntime:
    MAX_IDENTITIES = 8

    def __init__(self, repository, decryptor):
        self.repository, self.decryptor = repository, decryptor
        self.last_seed = None
        self.identities: OrderedDict[tuple, _IdentityState] = OrderedDict()

    def _client_state(self, job):
        config = job["config"]
        identity = (job["tenant"], config[0], config[1])
        context = f"gatescope:v1:tenant:{job['tenant']}:coinglass-config:{config[0]}:secret:{config[1]}"
        if config[6] != context:
            raise ValueError("CREDENTIAL_CONTEXT_MISMATCH")
        # Rotation never reuses the prior version's client, catalog or quota.
        for old in list(self.identities):
            if old[:2] == identity[:2] and old != identity:
                self.identities.pop(old).client.close()
        state = self.identities.get(identity)
        if state is None:
            material = self.decryptor.reveal(
                CredentialEnvelope(
                    ciphertext=bytes(config[2]),
                    nonce=bytes(config[3]),
                    wrapped_data_key=bytes(config[4]),
                    key_version=config[5],
                ),
                context=context.encode(),
            )
            state = _IdentityState(CoinglassClient(material.api_key))
            self.identities[identity] = state
            while len(self.identities) > self.MAX_IDENTITIES:
                self.identities.popitem(last=False)[1].client.close()
        self.identities.move_to_end(identity)
        tenant, user, config_id, version = job["tenant"], job["user"], config[0], config[1]

        def before_request(_path):
            waiting = self.repository.provider_wait(tenant, user, config_id, version)
            if waiting:
                raise CoinglassRateLimited(waiting)

        state.client.set_request_hooks(
            before_request=before_request,
            on_rate_limit=lambda _path, seconds: self.repository.pause_provider(
                tenant, user, config_id, version, seconds
            ),
        )
        return state

    def close(self):
        while self.identities:
            self.identities.popitem(last=False)[1].client.close()

    def _pause(self, job, seconds, state=None):
        seconds = min(MAX_RETRY_AFTER_SECONDS, max(1, int(seconds)))
        owner = (job["tenant"], job["user"], job["config"][0], job["config"][1])
        waiting = self.repository.provider_wait(*owner)
        if waiting > 0:
            # The HTTP hook already fixed the deadline at the actual 429.
            # Catching/completing later must not restart the relative timeout.
            seconds = waiting
        else:
            self.repository.pause_provider(*owner, seconds)
        if state is not None:
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + seconds)
        return seconds

    def run_one(self):
        clock = time.monotonic()
        if self.last_seed is None or clock - self.last_seed > 60:
            self.repository.seed()
            self.last_seed = clock
        job = self.repository.claim()
        if not job:
            return False
        delay = max(120, min(PERIODS[job["timeframe"]], 600))
        state = None
        provider_attempted = False
        try:
            # Local spot/wrong-venue/incomplete identities are known without a
            # credential or provider catalog. Keep the adapter's exact response
            # contract, but its local guard must return before client access.
            if match_instrument(job["instrument"], {"Gate": []})["reason"] != "NO_EXACT_CONTRACT":
                bundle = fetch_chart(
                    None,  # type: ignore[arg-type]  # Local-only, fail-closed path.
                    job["instrument"],
                    job["timeframe"],
                    int(datetime.now(timezone.utc).timestamp()),
                    limit=256,
                    supported_pairs={"Gate": []},
                    funding_unit_status="UNKNOWN",
                    skip_unverified_funding=True,
                )
                self.repository.complete(job, bundle, delay=3600)
                return True
            config = job["config"]
            waiting = self.repository.provider_wait(job["tenant"], job["user"], config[0], config[1])
            if waiting:
                raise CoinglassRateLimited(waiting)
            state = self._client_state(job)
            if state.cooldown_until > clock:
                raise CoinglassRateLimited(max(1, int(state.cooldown_until - clock) + 1))
            provider_attempted = True
            if state.quota_at is None or clock - state.quota_at > 300:
                # Only use rate metadata to pace work, never expose account data.
                account = state.client._get(
                    "/api/gateway/account", {}, allow_unwrapped=True, deadline_seconds=3.0, max_attempts=1
                ).data
                state.client.configure_rate_limits(account)
                state.quota_at = clock
            if state.pairs is None or clock - state.pairs_at > 3600:
                state.pairs = get_supported_pairs(state.client)
                state.pairs_at = clock
            bundle = fetch_chart(
                state.client,
                job["instrument"],
                job["timeframe"],
                int(datetime.now(timezone.utc).timestamp()),
                limit=256,
                supported_pairs=state.pairs,
                funding_unit_status="UNKNOWN",
                skip_unverified_funding=True,
                on_progress=lambda partial: self.repository.publish_progress(job, partial),
            )
            if bundle.get("status") in {"READY", "PARTIAL"}:
                delay = _refresh_delay(bundle, job["timeframe"], int(datetime.now(timezone.utc).timestamp()))
            retry = bundle.get("retry_after_seconds")
            if retry:
                delay = _retry_delay(state, self._pause(job, retry, state), advance=True)
            elif bundle.get("status") in {"READY", "PARTIAL"}:
                state.rate_limit_backoff = 0
            elif bundle.get("status") in {"UNSUPPORTED", "MISMATCH", "AMBIGUOUS"}:
                delay = 3600
        except CoinglassRateLimited as exc:
            if state is None:
                # A persisted cooldown can be observed before decrypting. Reuse
                # an existing identity's backoff without creating a client.
                state = self.identities.get((job["tenant"], job["config"][0], job["config"][1]))
            retry = self._pause(job, exc.retry_after_seconds, state)
            delay = _retry_delay(state, retry, advance=provider_attempted)
            bundle = {
                "status": "RATE_LIMITED",
                "series": [],
                "reason": "PROVIDER_RATE_LIMITED",
                "retry_after_seconds": retry,
            }
        except Exception:
            # No provider payload, credential, URL query or exception text in logs.
            delay = max(delay, 300)
            bundle = {"status": "ERROR", "series": [], "reason": "COINGLASS_REFRESH_FAILED"}
        self.repository.complete(job, bundle, delay=delay)
        return True


def _retry_delay(state, cooldown, *, advance):
    """Retry incomplete rate-limited collection separately from the normal TTL.

    Only attempted provider collections advance backoff; observing an existing
    cooldown is not a new failure. The persisted Retry-After always takes
    precedence, including when it exceeds the five-minute backoff ceiling.
    """
    if state is not None and advance:
        state.rate_limit_backoff = min(300, max(30, state.rate_limit_backoff * 2))
    return max(cooldown, state.rate_limit_backoff if state is not None else 0, 30)


def _refresh_delay(bundle, timeframe, now):
    """Schedule before the earliest collected metric expires, not the last one.

    Eligibility is not a completion-time promise: the real request pacer and
    persisted Retry-After still gate all subsequent provider access.
    """
    ttl = max(120, min(PERIODS[timeframe], 600))
    expiries = [
        epoch(series.get("expires_at"))
        for series in bundle.get("series", [])
        if isinstance(series, dict)
        and series.get("status") in {"READY", "PARTIAL"}
        and series.get("points")
        and series.get("unit_status") == "VERIFIED"
    ]
    expiries = [value for value in expiries if value > 0]
    if not expiries and epoch(bundle.get("expires_at")):
        expiries = [epoch(bundle["expires_at"])]
    return max(1, min(ttl - 5, min(expiries) - now - 5 if expiries else ttl - 5))
