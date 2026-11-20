"""Fail-closed Coinglass/LSR client for the fixed user-authorized relay."""

from __future__ import annotations

import email.utils
import hashlib
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, cast
from urllib.parse import urlencode, urljoin, urlparse

import requests

COINGLASS_ORIGIN = "https://vip2.coinglass.site"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 2_147_483_647
SAFE_API_KEY = re.compile(r"^cg_[A-Za-z0-9]{32,128}$")


class CoinglassError(RuntimeError):
    code = "PROVIDER_ERROR"

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class CoinglassSecurityError(CoinglassError):
    code = "PROVIDER_SECURITY_ERROR"


class CoinglassRateLimited(CoinglassError):
    code = "PROVIDER_RATE_LIMITED"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("provider rate limited the request", http_status=429)
        self.retry_after_seconds = retry_after_seconds


class CoinglassSchemaError(CoinglassError):
    code = "PROVIDER_SCHEMA_ERROR"


class CoinglassBusinessError(CoinglassError):
    code = "PROVIDER_BUSINESS_ERROR"


class CoinglassUnavailable(CoinglassError):
    code = "PROVIDER_UNAVAILABLE"


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...


@dataclass(frozen=True)
class ProviderResponse:
    data: Any
    request_id: str | None
    received_at: datetime
    elapsed_ms: int


@dataclass
class _PacingState:
    interval: float = 6.0
    next_at: float = 0.0
    cooldown_until: float = 0.0


class CoinglassRequestPacer:
    """Shared per-credential pacing across chart and configuration-test threads.

    Account/catalog requests also consume a slot. The registry is bounded and
    never evicts an active cooldown to make room. Persistent cooldown hooks in
    the worker separately protect restarts; this object stores no credentials.
    """

    MAX_KEYS = 1024
    MAX_WAIT_SECONDS = 20.0

    def __init__(self, *, monotonic=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = monotonic, sleep
        self.lock = threading.Lock()
        self.states: OrderedDict[tuple[str, str], _PacingState] = OrderedDict()

    def _state(self, identity: str, group: str) -> _PacingState:
        key = (identity, group)
        if key not in self.states:
            if len(self.states) >= self.MAX_KEYS:
                now = self.clock()
                idle = next((key for key, state in self.states.items()
                             if max(state.next_at, state.cooldown_until) <= now), None)
                if idle is None:
                    raise CoinglassUnavailable("provider request scheduler is busy")
                del self.states[idle]
            self.states[key] = _PacingState()
        self.states.move_to_end(key)
        return self.states[key]

    def acquire(self, identity: str, group: str) -> None:
        deadline = self.clock() + self.MAX_WAIT_SECONDS
        for _ in range(8):
            with self.lock:
                now = self.clock()
                state = self._state(identity, group)
                if state.cooldown_until > now:
                    raise CoinglassRateLimited(max(1, math.ceil(state.cooldown_until - now)))
                wait = state.next_at - now
                if wait <= 0:
                    state.next_at = now + state.interval
                    return
                if now + wait > deadline:
                    raise CoinglassRateLimited(max(1, math.ceil(wait)))
            # Do not hold the lock: another request's 429 must take effect even
            # while this thread waits, and other users must remain independent.
            self.sleep(wait)
        raise CoinglassUnavailable("provider request scheduler is busy")

    def configure(self, identity: str, group: str, *, limit: float, window: float) -> None:
        with self.lock:
            state = self._state(identity, group)
            old_interval = state.interval
            state.interval = max(0.05, window / limit * 1.15)
            if state.next_at > self.clock() and state.interval > old_interval:
                state.next_at += state.interval - old_interval

    def defer(self, identity: str, seconds: int) -> None:
        # Relay-wide 429 scope is not guaranteed to be endpoint-local. Pause
        # both groups rather than trying another endpoint with the same key.
        with self.lock:
            until = self.clock() + seconds
            for group in ("coinglass", "lsr"):
                state = self._state(identity, group)
                state.cooldown_until = max(state.cooldown_until, until)


_SHARED_PACER = CoinglassRequestPacer()


class CoinglassClient:
    """Only sends X-API-Key after validating the exact HTTPS origin."""

    MAX_ATTEMPTS = 2
    ACCOUNT_CACHE_SECONDS = 60
    CIRCUIT_FAILURES = 5
    CIRCUIT_OPEN_SECONDS = 60

    def __init__(
        self,
        api_key: str,
        *,
        session: HttpSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        pacer: CoinglassRequestPacer | None = None,
        before_request: Callable[[str], None] | None = None,
        on_rate_limit: Callable[[str, int], None] | None = None,
    ) -> None:
        if SAFE_API_KEY.fullmatch(api_key) is None:
            raise ValueError("Coinglass API credential has an invalid format")
        self._api_key = api_key
        self._pacer = pacer if pacer is not None else _SHARED_PACER if session is None else None
        self._pacing_identity = hashlib.sha256(api_key.encode("ascii")).hexdigest()
        self._before_request, self._on_rate_limit = before_request, on_rate_limit
        if session is None:
            owned = requests.Session()
            owned.trust_env = False
            self._session = cast(HttpSession, owned)
        else:
            self._session = session
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_until = 0.0
        self._account_cache: tuple[float, ProviderResponse] | None = None

    def set_request_hooks(self, *, before_request=None, on_rate_limit=None) -> None:
        self._before_request, self._on_rate_limit = before_request, on_rate_limit

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if close is not None:
            close()

    def configure_rate_limits(self, account: Any) -> None:
        if self._pacer is None or not isinstance(account, dict):
            return
        rates = account.get("rate_limits")
        if not isinstance(rates, dict):
            return
        for group in ("coinglass", "lsr"):
            rate = rates.get(group)
            if not isinstance(rate, dict):
                continue
            try:
                limit, window = float(rate["limit"]), float(rate["window_seconds"])
                if (isinstance(rate["limit"], bool) or isinstance(rate["window_seconds"], bool)
                        or not 0 < limit <= 1_000_000 or not 0 < window <= 86400):
                    continue
                self._pacer.configure(self._pacing_identity, group, limit=limit, window=window)
            except (TypeError, ValueError, KeyError, OverflowError):
                continue

    def _prepare_request(self, path: str) -> None:
        if self._before_request is not None:
            self._before_request(path)
        if self._pacer is not None:
            self._pacer.acquire(self._pacing_identity, "lsr" if path.startswith("/api/lsr/") else "coinglass")
        # A different thread may have received 429 while we waited for a slot.
        if self._before_request is not None:
            self._before_request(path)

    def _limited(self, path: str, seconds: int) -> None:
        if self._pacer is not None:
            self._pacer.defer(self._pacing_identity, seconds)
        if self._on_rate_limit is not None:
            self._on_rate_limit(path, seconds)

    def get_coins_markets(self, *, per_page: int = 10) -> ProviderResponse:
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        result = self._get("/api/futures/coins-markets", {"per_page": str(per_page)})
        if not isinstance(result.data, list):
            self._failure()
            raise CoinglassSchemaError("coins-markets data must be a list")
        return result

    def get_lsr_signal(self, symbol: str, *, mode: str = "trader") -> ProviderResponse:
        normalized = symbol.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{1,20}", normalized) is None or mode != "trader":
            raise ValueError("LSR symbol or mode is invalid")
        result = self._get(f"/api/lsr/signal/{normalized}", {"mode": mode}, allow_unwrapped=True)
        if not isinstance(result.data, dict) or not result.data:
            self._failure()
            raise CoinglassSchemaError("LSR signal data must be a non-empty object")
        return result

    def get_account(self, *, force: bool = False) -> ProviderResponse:
        now = self._monotonic()
        with self._lock:
            cached = self._account_cache
        if not force and cached is not None and cached[0] > now:
            return cached[1]
        result = self._get("/api/gateway/account", {})
        if not isinstance(result.data, dict):
            self._failure()
            raise CoinglassSchemaError("account data must be an object")
        with self._lock:
            self._account_cache = (self._monotonic() + self.ACCOUNT_CACHE_SECONDS, result)
        return result

    def _get(
        self,
        path: str,
        query: Mapping[str, str],
        *,
        allow_unwrapped: bool = False,
        deadline_seconds: float = 12.0,
        max_attempts: int | None = None,
    ) -> ProviderResponse:
        attempts = self.MAX_ATTEMPTS if max_attempts is None else max_attempts
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= self.MAX_ATTEMPTS:
            raise ValueError("provider attempts must be within the bounded client policy")
        url = _safe_url(path, query)
        self._require_closed_circuit(self._monotonic())
        self._prepare_request(path)
        started = self._monotonic()
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._prepare_request(path)
            remaining = deadline_seconds - (self._monotonic() - started)
            if remaining <= 0:
                self._failure()
                raise CoinglassUnavailable("provider request deadline exceeded")
            try:
                response = self._session.get(
                    url,
                    headers={"X-API-Key": self._api_key, "Accept": "application/json"},
                    timeout=(3.0, min(8.0, remaining)),
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                self._failure()
                raise CoinglassUnavailable("provider network request failed") from exc
            if 300 <= response.status_code < 400:
                self._failure(open_immediately=True)
                raise CoinglassSecurityError(
                    "provider redirect was rejected", http_status=response.status_code,
                )
            if response.status_code == 429:
                retry_after = _retry_after(response.headers.get("Retry-After"))
                self._limited(path, retry_after)
                remaining = deadline_seconds - (self._monotonic() - started)
                if attempt >= attempts or retry_after > remaining:
                    self._failure()
                    raise CoinglassRateLimited(retry_after)
                self._sleep(float(retry_after))
                continue
            if response.status_code != 200:
                self._failure()
                raise CoinglassUnavailable(
                    f"provider returned HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > MAX_RESPONSE_BYTES:
                        self._failure()
                        raise CoinglassSchemaError("provider response exceeded the size limit")
                except ValueError as exc:
                    self._failure()
                    raise CoinglassSchemaError("provider content length is invalid") from exc
            if len(response.content) > MAX_RESPONSE_BYTES:
                self._failure()
                raise CoinglassSchemaError("provider response exceeded the size limit")
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                self._failure()
                raise CoinglassSchemaError("provider returned invalid JSON") from exc
            # Some relays transport upstream business 429 inside HTTP 200.
            # Treat the documented numeric code exactly, not arbitrary text.
            if isinstance(payload, dict) and str(payload.get("code")) == "429":
                retry_after = _retry_after(response.headers.get("Retry-After"))
                self._limited(path, retry_after)
                self._failure()
                raise CoinglassRateLimited(retry_after)
            try:
                data = _business_data(payload, allow_unwrapped=allow_unwrapped)
            except CoinglassError:
                self._failure()
                raise
            received = datetime.now(timezone.utc)
            elapsed_ms = max(0, math.ceil((self._monotonic() - started) * 1_000))
            request_id = _safe_request_id(response.headers)
            self._success()
            return ProviderResponse(data, request_id, received, elapsed_ms)
        raise AssertionError("bounded attempt loop did not terminate")

    def _require_closed_circuit(self, now: float) -> None:
        with self._lock:
            if self._circuit_until > now:
                raise CoinglassUnavailable("provider circuit is open")
            if self._circuit_until:
                self._circuit_until = 0.0
                self._consecutive_failures = 0

    def _success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._circuit_until = 0.0

    def _failure(self, *, open_immediately: bool = False) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if open_immediately or self._consecutive_failures >= self.CIRCUIT_FAILURES:
                self._circuit_until = self._monotonic() + self.CIRCUIT_OPEN_SECONDS


def _safe_url(path: str, query: Mapping[str, str]) -> str:
    if not path.startswith("/api/") or ".." in path or "//" in path:
        raise CoinglassSecurityError("provider path is invalid")
    url = urljoin(f"{COINGLASS_ORIGIN}/", path.removeprefix("/"))
    if query:
        url = f"{url}?{urlencode(query)}"
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "vip2.coinglass.site"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CoinglassSecurityError("provider origin validation failed")
    return url


def _business_data(payload: Any, *, allow_unwrapped: bool) -> Any:
    if not isinstance(payload, dict):
        raise CoinglassSchemaError("provider response must be an object")
    if "code" in payload:
        if str(payload["code"]) != "0":
            raise CoinglassBusinessError("provider business request was rejected")
        if "data" not in payload:
            raise CoinglassSchemaError("provider response is missing data")
        return payload["data"]
    if allow_unwrapped and payload:
        return payload
    raise CoinglassSchemaError("provider response is missing business code")


def _retry_after(value: str | None) -> int:
    if value is None:
        return 1
    stripped = value.strip()
    if stripped.isdigit():
        # Do not turn a multi-hour Retry-After into an early one-hour retry.
        # Extremely large values fail closed for the lifetime of this service.
        if len(stripped) > 10:
            return MAX_RETRY_AFTER_SECONDS
        return max(1, min(int(stripped), MAX_RETRY_AFTER_SECONDS))
    try:
        parsed = email.utils.parsedate_to_datetime(stripped)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = math.ceil((parsed - datetime.now(timezone.utc)).total_seconds())
        return max(1, min(seconds, MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError, OverflowError):
        return 1


def _safe_request_id(headers: Mapping[str, str]) -> str | None:
    candidate = headers.get("X-Request-ID") or headers.get("X-Trace-ID")
    if candidate is None or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", candidate) is None:
        return None
    return candidate


def safe_provider_metadata(value: Any) -> Any:
    """Remove provider identity/credential fields before persistence or display."""

    blocked = ("key", "token", "secret", "password", "email", "phone", "user_id", "account_id")
    if isinstance(value, dict):
        return {
            str(key): safe_provider_metadata(item)
            for key, item in list(value.items())[:100]
            if not any(fragment in str(key).lower() for fragment in blocked)
        }
    if isinstance(value, list):
        return [safe_provider_metadata(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:512]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)
