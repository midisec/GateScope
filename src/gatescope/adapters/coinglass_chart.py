"""Bounded chart-only history reads through the existing fixed-origin client."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Mapping

from gatescope.adapters.coinglass import (
    CoinglassClient,
    CoinglassError,
    CoinglassRateLimited,
    MAX_RETRY_AFTER_SECONDS,
)
from gatescope.domain.coinglass_context import (
    HISTORY_SCOPE,
    METRICS,
    VERSION,
    canonical_hash,
    match_instrument,
    normalize_series,
)
from gatescope.domain.market import TIMEFRAME_SECONDS

HTTP_DEADLINE_SECONDS = 3.0
HTTP_MAX_ATTEMPTS = 1
MAX_FETCH_SECONDS = 60.0
_ENDPOINTS = (
    ("open-interest/history", ("oi",)),
    ("funding-rate/history", ("funding",)),
    ("global-long-short-account-ratio/history", ("lsr",)),
    ("liquidation/history", ("liquidation_long", "liquidation_short")),
)


def get_supported_pairs(client: CoinglassClient) -> Any:
    """Unwrapped exchange catalog; caller may cache within its credential scope."""
    return client._get(
        "/api/futures/supported-exchange-pairs",
        {},
        deadline_seconds=HTTP_DEADLINE_SECONDS,
        max_attempts=HTTP_MAX_ATTEMPTS,
    ).data


def fetch_chart(
    client: CoinglassClient,
    instrument: Mapping[str, Any],
    timeframe: str,
    now: int,
    limit: int = 256,
    *,
    supported_pairs: Any = None,
    funding_unit: str = "PERCENT",
    funding_unit_status: str = "UNKNOWN",
    skip_unverified_funding: bool = False,
    request_spacing_seconds: float = 0.0,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Return a secret-free, exact-contract snapshot with per-series failures.

    This performs at most one catalog and four history HTTP attempts. A 429
    halts subsequent requests, exposing a bounded retry hint to the scheduler.
    Unknown fee units are quarantined; raw values never get silently scaled.
    """
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("collection time must be epoch seconds")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
        raise ValueError("chart limit must be between 1 and 256")
    if not 0 <= request_spacing_seconds <= 3:
        raise ValueError("invalid provider pacing interval")
    started = time.monotonic()
    result: dict[str, Any] = {
        "version": VERSION,
        "status": "UNSUPPORTED",
        "instrument_id": str(instrument.get("id", instrument.get("instrument_id", ""))),
        "timeframe": timeframe,
        "scope": None,
        "match": {"status": "UNSUPPORTED", "reason": "TIMEFRAME_UNSUPPORTED"},
        "as_of": None,
        "fetched_at": now,
        "expires_at": now + 120,
        "series": [],
        "history_scope": HISTORY_SCOPE,
        "requested_limit": limit,
        "safe_errors": [],
        "retry_after_seconds": None,
        "automatic_feature_eligible": False,
        "automatic_feature_reason": "DESCRIPTIVE_CONTEXT_NOT_VALIDATED_ALPHA",
    }

    def finish() -> dict[str, Any]:
        result["snapshot_id"] = canonical_hash(result)
        return result

    if timeframe not in TIMEFRAME_SECONDS:
        return finish()
    # Invalid local identities should not spend a catalog request or leak a
    # user-supplied string into provider queries. Empty catalog is intentional.
    local = match_instrument(instrument, {"Gate": []})
    if local["reason"] != "NO_EXACT_CONTRACT":
        result["match"] = {key: local[key] for key in ("status", "reason")}
        return finish()
    if supported_pairs is None:
        try:
            supported_pairs = get_supported_pairs(client)
        except CoinglassError as exc:
            result["status"] = "RATE_LIMITED" if isinstance(exc, CoinglassRateLimited) else "ERROR"
            result["match"] = {"status": "UNSUPPORTED", "reason": "CATALOG_UNAVAILABLE"}
            result["safe_errors"].append({"group": "catalog", "code": exc.code})
            if isinstance(exc, CoinglassRateLimited):
                result["retry_after_seconds"] = min(MAX_RETRY_AFTER_SECONDS, max(1, exc.retry_after_seconds))
            return finish()
    match = match_instrument(instrument, supported_pairs)
    result["match"] = {key: match[key] for key in ("status", "reason")}
    result["scope"] = match["scope"]
    if match["status"] != "EXACT":
        return finish()

    def progress():
        if on_progress is None:
            return
        partial = copy.deepcopy(result)
        present = {series["id"] for series in partial["series"]}
        for metric_id in METRICS:
            if metric_id in present:
                continue
            unit_status = funding_unit_status if metric_id == "funding" else "VERIFIED"
            pending = normalize_series(
                metric_id,
                [],
                timeframe=timeframe,
                fetched_at=result["fetched_at"],
                limit=limit,
                unit=funding_unit if metric_id == "funding" else METRICS[metric_id][1],
                unit_status=unit_status,
            )
            if not (metric_id == "funding" and skip_unverified_funding and unit_status != "VERIFIED"):
                pending.update(status="DEFERRED", quality_flags=["COLLECTION_IN_PROGRESS"])
            pending.update(
                fetched_at=result["fetched_at"],
                expires_at=result["fetched_at"] + min(600, max(120, TIMEFRAME_SECONDS[timeframe])),
            )
            partial["series"].append(pending)
        points = [point for series in partial["series"] for point in series.get("points", [])]
        if not points:
            return
        partial.update(
            status="PARTIAL",
            refresh_status="COLLECTING",
            as_of=max(point["close_time"] for point in points),
            expires_at=result["fetched_at"] + min(600, max(120, TIMEFRAME_SECONDS[timeframe])),
        )
        partial["snapshot_id"] = canonical_hash(
            {key: value for key, value in partial.items() if key != "snapshot_id"}
        )
        on_progress(partial)

    rate_limited = False
    requested = False
    for endpoint, metric_ids in _ENDPOINTS:
        if metric_ids == ("funding",) and funding_unit_status != "VERIFIED" and skip_unverified_funding:
            result["series"].append(
                normalize_series(
                    "funding",
                    [],
                    timeframe=timeframe,
                    fetched_at=now,
                    limit=limit,
                    unit=funding_unit,
                    unit_status="UNKNOWN",
                )
            )
            continue
        if rate_limited or time.monotonic() - started >= MAX_FETCH_SECONDS:
            reason = "DEFERRED_AFTER_RATE_LIMIT" if rate_limited else "FETCH_BUDGET_EXHAUSTED"
            for metric_id in metric_ids:
                series = normalize_series(metric_id, [], timeframe=timeframe, fetched_at=now, limit=limit)
                series.update(status="DEFERRED", quality_flags=[reason])
                result["series"].append(series)
            continue
        query = {
            "exchange": "Gate",
            "symbol": match["scope"]["symbol"],
            "interval": timeframe,
            "limit": str(limit),
            "end_time": str(now * 1000),
        }
        if metric_ids == ("oi",):
            query["unit"] = "usd"
        try:
            if requested and request_spacing_seconds:
                time.sleep(request_spacing_seconds)
            requested = True
            response = client._get(
                "/api/futures/" + endpoint,
                query,
                deadline_seconds=HTTP_DEADLINE_SECONDS,
                max_attempts=HTTP_MAX_ATTEMPTS,
            )
            collected = max(now, int(response.received_at.timestamp()))
            result["fetched_at"] = max(result["fetched_at"], collected)
            has_new_points = False
            for metric_id in metric_ids:
                series = normalize_series(
                    metric_id,
                    response.data,
                    timeframe=timeframe,
                    fetched_at=collected,
                    limit=limit,
                    closed_at=now,
                    unit=funding_unit if metric_id == "funding" else METRICS[metric_id][1],
                    unit_status=funding_unit_status if metric_id == "funding" else "VERIFIED",
                )
                if metric_id == "funding":
                    series["funding_interval_seconds"] = match["scope"]["funding_interval_seconds"]
                series.update(
                    fetched_at=collected,
                    expires_at=collected + min(600, max(120, TIMEFRAME_SECONDS[timeframe])),
                    as_of=max((point["close_time"] for point in series["points"]), default=None),
                )
                has_new_points |= bool(series["points"])
                result["series"].append(series)
            if has_new_points:
                progress()
        except CoinglassError as exc:
            result["safe_errors"].append({"group": endpoint.split("/")[0], "code": exc.code})
            rate_limited = isinstance(exc, CoinglassRateLimited)
            if isinstance(exc, CoinglassRateLimited):
                result["retry_after_seconds"] = min(MAX_RETRY_AFTER_SECONDS, max(1, exc.retry_after_seconds))
            for metric_id in metric_ids:
                series = normalize_series(metric_id, [], timeframe=timeframe, fetched_at=now, limit=limit)
                series.update(status="RATE_LIMITED" if rate_limited else "ERROR", quality_flags=[exc.code])
                result["series"].append(series)
    ready = [series for series in result["series"] if series["status"] == "READY"]
    has_points = any(series["points"] for series in result["series"])
    result["status"] = (
        "READY"
        if len(ready) == len(METRICS)
        else "PARTIAL"
        if has_points
        else "RATE_LIMITED"
        if rate_limited
        else "EMPTY"
    )
    latest = [series["points"][-1]["close_time"] for series in result["series"] if series["points"]]
    result["as_of"] = max(latest) if latest else None
    result["expires_at"] = result["fetched_at"] + min(600, max(120, TIMEFRAME_SECONDS[timeframe]))
    return finish()
