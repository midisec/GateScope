"""Credential-free, bounded public Gate quotes for PAPER matching only."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Timeout

from gatescope.domain.paper_matching import BookLevel, ContractSpec, OrderBook, validate_symbol

PUBLIC_ORIGIN = "https://api.gateio.ws"
PUBLIC_PREFIX = "/api/v4/futures/usdt"
MAX_RESPONSE_BYTES = 512 * 1024
REQUEST_BUDGET_SECONDS = 5.0
METADATA_TTL_SECONDS = 30.0
MAX_METADATA_CACHE = 128


class PaperMarketUnavailable(ValueError):
    """Safe reason code; never exposes upstream bodies, cookies or request URLs."""

    def __init__(self, reason: str, *, retry_after: float | None = None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


@dataclass(frozen=True)
class MarketSnapshot:
    spec: ContractSpec
    book: OrderBook
    mark_price: Decimal | None
    funding_rate: Decimal | None
    metadata_as_of: datetime
    funding_interval_seconds: int | None = None

    def to_dict(self) -> dict:
        return {"spec": self.spec.to_dict(), "book": self.book.to_dict(),
                "mark_price": str(self.mark_price) if self.mark_price is not None else None,
                "funding_rate": str(self.funding_rate) if self.funding_rate is not None else None,
                "funding_rate_unit": "DECIMAL_RATIO_PER_FUNDING_INTERVAL" if self.funding_rate is not None else None,
                "funding_interval_seconds": self.funding_interval_seconds,
                "funding_simulated": False, "metadata_as_of": self.metadata_as_of.isoformat(),
                "metadata_ttl_seconds": METADATA_TTL_SECONDS,
                "mark_price_use": "INFORMATIONAL_METADATA_SNAPSHOT_NOT_EXECUTION_PRICE",
                "execution_mode": "PAPER", "source": "GATE_PUBLIC_REST"}


def _number(value, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PaperMarketUnavailable(f"GATE_INVALID_{name}")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise PaperMarketUnavailable(f"GATE_INVALID_{name}") from None
    if not result.is_finite() or (positive and result <= 0):
        raise PaperMarketUnavailable(f"GATE_INVALID_{name}")
    return result


def _timestamp(value, name: str) -> datetime:
    timestamp = _number(value, name, positive=True)
    # Gate's order_book current/update are fractional Unix SECONDS, not ms.
    if not Decimal("946684800") <= timestamp < Decimal("4133980800"):
        raise PaperMarketUnavailable(f"GATE_INVALID_{name}")
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise PaperMarketUnavailable(f"GATE_INVALID_{name}") from None


class GatePaperMarket:
    """Only GET contract metadata and depth; no credential or host argument.

    Socket connect/read limits and a five-second monotonic request budget bound
    normal operation. Like requests itself, OS DNS stalls are not a hard real-
    time guarantee; stale/incomplete responses always fail closed before a fill.
    """

    def __init__(
        self, session=None, *, clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
        depth_limit: int = 100,
    ) -> None:
        if depth_limit not in {50, 100}:
            raise ValueError("paper order book depth limit must be 50 or 100")
        self.session = session if session is not None else requests.Session()
        # In particular do not use GateClient: its retry/redirect behavior is
        # intended for historical public downloads, not executable paper quotes.
        self.session.trust_env = False
        self.session.auth = None
        if isinstance(self.session, requests.Session):
            self.session.headers.clear()
            self.session.cookies.clear()
            self.session.proxies.clear()
            self.session.mount("https://", HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4))
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.monotonic, self.sleep, self.depth_limit = monotonic, sleep, depth_limit
        self._cache: OrderedDict[str, tuple[float, dict, datetime]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _retry_after(self, raw: str) -> float:
        try:
            result = float(raw)
        except (TypeError, ValueError):
            try:
                result = (parsedate_to_datetime(raw) - self.clock()).total_seconds()
            except (TypeError, ValueError, OverflowError):
                result = 30.0
        if not math.isfinite(result) or result < 0 or result > 86400:
            return 30.0
        return result

    def _json(self, path: str, params: dict, deadline: float) -> dict:
        for attempt in range(2):
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise PaperMarketUnavailable("GATE_QUOTE_BUDGET_EXCEEDED")
            timeout = Timeout(total=remaining, connect=min(1.0, remaining), read=min(1.0, remaining))
            try:
                if isinstance(self.session, requests.Session):
                    # Do not echo Set-Cookie values from an earlier public
                    # response. The adapter never needs a server-side session.
                    self.session.cookies.clear()
                with self.session.get(
                    PUBLIC_ORIGIN + PUBLIC_PREFIX + path, params=params,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity",
                             "User-Agent": "gatescope-paper/1.0", "X-Gate-Size-Decimal": "1"},
                    # requests forwards urllib3 Timeout objects directly; its stubs omit that overload.
                    timeout=timeout,  # type: ignore[arg-type]
                    allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code == 429:
                        retry = self._retry_after(response.headers.get("Retry-After", "30"))
                        # Long waits belong to a scheduler; never block a user's
                        # simulated order for an upstream rate-limit window.
                        if attempt == 0 and retry <= 0.25 and self.monotonic() + retry + 0.5 < deadline:
                            self.sleep(retry)
                            continue
                        raise PaperMarketUnavailable("GATE_RATE_LIMITED", retry_after=retry)
                    if response.status_code != 200:
                        raise PaperMarketUnavailable("GATE_PUBLIC_REQUEST_FAILED")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                        raise PaperMarketUnavailable("GATE_RESPONSE_TOO_LARGE")
                    blocks, size = [], 0
                    for block in response.iter_content(1024):
                        size += len(block)
                        if size > MAX_RESPONSE_BYTES:
                            raise PaperMarketUnavailable("GATE_RESPONSE_TOO_LARGE")
                        if self.monotonic() > deadline:
                            raise PaperMarketUnavailable("GATE_QUOTE_BUDGET_EXCEEDED")
                        blocks.append(block)
                    if self.monotonic() > deadline:
                        raise PaperMarketUnavailable("GATE_QUOTE_BUDGET_EXCEEDED")
                    try:
                        value = json.loads(b"".join(blocks), parse_float=Decimal,
                                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeError, RecursionError):
                        raise PaperMarketUnavailable("GATE_INVALID_JSON") from None
                    if not isinstance(value, dict) or "label" in value:
                        raise PaperMarketUnavailable("GATE_INVALID_PUBLIC_RESPONSE")
                    return value
            except requests.RequestException:
                raise PaperMarketUnavailable("GATE_PUBLIC_NETWORK_UNAVAILABLE") from None
        raise PaperMarketUnavailable("GATE_RATE_LIMITED", retry_after=30)

    def _metadata(self, symbol: str, deadline: float) -> tuple[dict, datetime]:
        with self._cache_lock:
            cached = self._cache.get(symbol)
            if cached is not None and self.monotonic() - cached[0] <= METADATA_TTL_SECONDS:
                self._cache.move_to_end(symbol)
                return cached[1], cached[2]
        value = self._json("/contracts/" + quote(symbol, safe=""), {}, deadline)
        # Validate before caching; upstream errors must never become a success
        # cache entry, and expired metadata is never used as a fallback.
        self._spec(symbol, value)
        as_of = self.clock()
        with self._cache_lock:
            self._cache[symbol] = (self.monotonic(), value, as_of)
            self._cache.move_to_end(symbol)
            while len(self._cache) > MAX_METADATA_CACHE:
                self._cache.popitem(last=False)
        return value, as_of

    @staticmethod
    def _spec(symbol: str, value: dict) -> ContractSpec:
        if value.get("name") != symbol or value.get("type") != "direct":
            raise PaperMarketUnavailable("GATE_UNSUPPORTED_CONTRACT")
        if value.get("in_delisting") is not False or value.get("status", "trading") != "trading":
            raise PaperMarketUnavailable("GATE_CONTRACT_NOT_TRADING")
        try:
            return ContractSpec(
                symbol=symbol, multiplier=_number(value.get("quanto_multiplier"), "MULTIPLIER", positive=True),
                price_tick=_number(value.get("order_price_round"), "PRICE_TICK", positive=True),
                min_contracts=_number(value.get("order_size_min"), "MIN_SIZE", positive=True),
                max_contracts=_number(value.get("order_size_max"), "MAX_SIZE", positive=True),
                taker_fee_rate=_number(value.get("taker_fee_rate"), "TAKER_FEE"),
                market_max_contracts=_number(value["market_order_size_max"], "MARKET_MAX_SIZE", positive=True)
                if value.get("market_order_size_max") not in (None, "", "0", 0) else None,
            )
        except ValueError as exc:
            if isinstance(exc, PaperMarketUnavailable):
                raise
            raise PaperMarketUnavailable("GATE_INVALID_CONTRACT_SPEC") from None

    def get(self, symbol: str) -> MarketSnapshot:
        symbol = validate_symbol(symbol)
        deadline = self.monotonic() + REQUEST_BUDGET_SECONDS
        metadata, as_of = self._metadata(symbol, deadline)
        spec = self._spec(symbol, metadata)
        raw = self._json("/order_book", {"contract": symbol, "limit": self.depth_limit,
                                        "with_id": "true", "interval": "0"}, deadline)
        captured = self.clock()
        sequence = raw.get("id")
        if type(sequence) is not int or sequence <= 0:
            raise PaperMarketUnavailable("GATE_INVALID_BOOK_SEQUENCE")
        try:
            def levels(name: str) -> tuple[BookLevel, ...]:
                values = raw.get(name)
                if (not isinstance(values, list) or not 1 <= len(values) <= self.depth_limit
                        or any(not isinstance(row, dict) for row in values)):
                    raise PaperMarketUnavailable("GATE_INVALID_BOOK_DEPTH")
                return tuple(BookLevel(_number(row.get("p"), "BOOK_PRICE", positive=True),
                                       _number(row.get("s"), "BOOK_SIZE", positive=True))
                             for row in values)
            book = OrderBook(symbol, levels("bids"), levels("asks"), _timestamp(raw.get("current"), "BOOK_CURRENT"),
                             captured, updated_at=_timestamp(raw.get("update"), "BOOK_UPDATE"),
                             sequence_id=str(sequence))
            book.assert_fresh(captured)
            for level in (*book.bids, *book.asks):
                if level.price % spec.price_tick != 0:
                    raise PaperMarketUnavailable("GATE_BOOK_PRICE_TICK_MISMATCH")
            mark = _number(metadata["mark_price"], "MARK_PRICE", positive=True) if metadata.get("mark_price") is not None else None
            funding = _number(metadata["funding_rate"], "FUNDING_RATE") if metadata.get("funding_rate") is not None else None
            interval = metadata.get("funding_interval")
            if interval is not None and (type(interval) is not int or not 1 <= interval <= 86400):
                raise PaperMarketUnavailable("GATE_INVALID_FUNDING_INTERVAL")
            if self.monotonic() > deadline:
                raise PaperMarketUnavailable("GATE_QUOTE_BUDGET_EXCEEDED")
            return MarketSnapshot(spec, book, mark, funding, as_of, interval)
        except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
            if isinstance(exc, PaperMarketUnavailable):
                raise
            raise PaperMarketUnavailable("GATE_INVALID_ORDER_BOOK") from None

    def close(self) -> None:
        self.session.close()
