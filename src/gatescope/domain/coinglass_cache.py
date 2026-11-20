"""Failure-tolerant Coinglass cache projections without rewriting data time."""

from __future__ import annotations

import copy
import math
from typing import Any

from gatescope.domain.coinglass_context import METRICS, TIMEFRAME_SECONDS, canonical_hash

TRANSIENT = {"ERROR", "RATE_LIMITED", "DEFERRED", "EMPTY", "PARTIAL"}
RETAINABLE_SERIES = {"ERROR", "RATE_LIMITED", "DEFERRED", "EMPTY"}
UNSAFE_MATCHES = {"MISMATCH", "AMBIGUOUS"}


def epoch(value: Any, default: int = 0) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 253402300799:
        return int(value)
    return default


def _points(series: Any) -> bool:
    return isinstance(series, dict) and isinstance(series.get("points"), list) and bool(series["points"])


def merge_refresh(
    previous: dict,
    incoming: dict,
    *,
    instrument_id: str,
    timeframe: str,
    now: int,
    ttl: int,
    retry_at: int = 0,
) -> dict:
    """Caller must already have checked config version, owner and lease identity.

    Cached point time/availability are copied verbatim. Stale retained series
    never carry READY/PARTIAL status, so existing review logic excludes them.
    """
    old, result = copy.deepcopy(previous), copy.deepcopy(incoming)
    identity = {"instrument_id": instrument_id, "timeframe": timeframe}
    incoming_identity_ok = all(result.get(key, value) == value for key, value in identity.items())
    if not incoming_identity_ok:
        result = {
            "status": "MISMATCH",
            "series": [],
            "match": {"status": "MISMATCH", "reason": "CACHE_IDENTITY_MISMATCH"},
        }
    result.update(identity)
    result["fetched_at"] = epoch(result.get("fetched_at"), now)
    result["expires_at"] = epoch(result.get("expires_at"), now + ttl)
    attempt_status = str(result.get("status", "ERROR"))
    result.update(
        last_attempt_at=now,
        refresh_status=attempt_status,
        retry_at=retry_at,
        retry_after_seconds=max(0, retry_at - now),
        cache_retained=False,
    )
    old_match = old.get("match") if isinstance(old.get("match"), dict) else {}
    new_match = result.get("match") if isinstance(result.get("match"), dict) else {}
    explicit_unsupported = attempt_status in {"UNSUPPORTED", "MISMATCH", "AMBIGUOUS"}
    same_identity = all(old.get(key) == value for key, value in identity.items())
    same_scope = (
        isinstance(old.get("scope"), dict)
        and bool(old["scope"])
        and (result.get("scope") is None or result.get("scope") == old["scope"])
    )
    can_retain = (
        incoming_identity_ok
        and same_identity
        and same_scope
        and old_match.get("status") == "EXACT"
        and attempt_status in TRANSIENT
        and not explicit_unsupported
        and new_match.get("status") not in UNSAFE_MATCHES
        and (new_match.get("status") != "UNSUPPORTED" or new_match.get("reason") == "CATALOG_UNAVAILABLE")
    )
    old_series = {item.get("id"): item for item in (old.get("series") or []) if _points(item)}
    new_series = [item for item in (result.get("series") or []) if isinstance(item, dict)]
    present = {item.get("id") for item in new_series}
    if can_retain:
        new_series.extend(
            {"id": name, "status": "DEFERRED", "points": []} for name in old_series if name not in present
        )
    merged = []
    retained = 0
    fresh_usable = False
    for item in new_series:
        prior = old_series.get(item.get("id"))
        if (
            can_retain
            and prior is not None
            and not _points(item)
            and item.get("status") in RETAINABLE_SERIES
            and prior.get("status") in {"READY", "PARTIAL", "STALE"}
            and prior.get("unit_status") == "VERIFIED"
            and prior.get("native_timeframe") == timeframe
            and all(
                key not in item or item[key] == prior.get(key)
                for key in ("unit", "unit_status", "population", "native_timeframe", "unit_source")
            )
        ):
            saved = copy.deepcopy(prior)
            saved.update(
                status="STALE",
                cache_retained=True,
                refresh_status=item.get("status"),
                fetched_at=epoch(prior.get("fetched_at"), epoch(old.get("fetched_at"))),
                expires_at=epoch(prior.get("expires_at"), epoch(old.get("expires_at"))),
                as_of=prior.get("as_of", old.get("as_of")),
            )
            saved["quality_flags"] = sorted(
                set(prior.get("quality_flags", [])) | {"CACHED_AFTER_REFRESH_FAILURE", "SOURCE_STALE"}
            )
            merged.append(saved)
            retained += 1
        else:
            item.update(
                cache_retained=False,
                fetched_at=epoch(item.get("fetched_at"), result["fetched_at"]),
                expires_at=epoch(item.get("expires_at"), result["expires_at"]),
            )
            if _points(item):
                item.setdefault(
                    "as_of",
                    max(
                        (
                            epoch(point.get("close_time"))
                            for point in item["points"]
                            if isinstance(point, dict)
                        ),
                        default=0,
                    ),
                )
                fresh_usable |= item.get("status") in {"READY", "PARTIAL"}
            merged.append(item)
    result["series"] = merged
    if retained:
        result["cache_retained"] = True
        result["quality_flags"] = sorted(
            set(result.get("quality_flags", [])) | {"CACHED_AFTER_REFRESH_FAILURE"}
        )
        if result.get("scope") is None:
            result["scope"] = copy.deepcopy(old["scope"])
            result["match"] = copy.deepcopy(old_match)
        if fresh_usable:
            result["status"] = "PARTIAL"
        else:
            result.update(
                status="STALE",
                fetched_at=epoch(old.get("fetched_at")),
                as_of=old.get("as_of"),
                expires_at=epoch(old.get("expires_at")),
            )
    result["snapshot_id"] = canonical_hash(
        {key: value for key, value in result.items() if key != "snapshot_id"}
    )
    return result


def project_cache(
    payload: dict,
    *,
    now: int,
    requested: bool,
    due_at: int,
    last_updated: int = 0,
    provider_retry_at: int = 0,
) -> dict:
    result = copy.deepcopy(payload)
    attempt = epoch(result.get("last_attempt_at"), last_updated or epoch(result.get("fetched_at")))
    retry_at = epoch(result.get("retry_at"))
    if not retry_at and result.get("retry_after_seconds"):
        # Legacy snapshots stored only a relative hint. Anchor it to the old
        # attempt, NEVER to the time this snapshot is read.
        retry_at = attempt + epoch(result.get("retry_after_seconds")) if attempt else 0
    retry_at = max(retry_at, provider_retry_at)
    remaining = max(0, retry_at - now)
    result.update(
        last_attempt_at=attempt,
        retry_at=retry_at,
        retry_after_seconds=remaining,
        refresh_status=result.get("refresh_status", result.get("status", "NOT_CACHED")),
        cache_retained=bool(result.get("cache_retained")),
        next_refresh_at=max(due_at, retry_at),
        refresh_requested=requested,
    )
    result["expires_at"] = epoch(result.get("expires_at"))
    expired_limit = remaining == 0 and (
        result.get("refresh_status") == "RATE_LIMITED"
        or result.get("reason") == "PROVIDER_RATE_LIMITED"
        or any(
            isinstance(error, dict) and error.get("code") == "PROVIDER_RATE_LIMITED"
            for error in result.get("safe_errors", [])
        )
        or any(
            isinstance(series, dict)
            and (series.get("status") == "RATE_LIMITED" or series.get("refresh_status") == "RATE_LIMITED")
            for series in result.get("series", [])
        )
    )
    if expired_limit:
        result["last_refresh_status"] = result["refresh_status"]
        result["refresh_status"] = "QUEUED" if requested else "DEFERRED"
        if result.get("safe_errors"):
            result["last_safe_errors"] = copy.deepcopy(result["safe_errors"])
            result["safe_errors"] = [
                error
                for error in result["safe_errors"]
                if not isinstance(error, dict) or error.get("code") != "PROVIDER_RATE_LIMITED"
            ]
        if result.get("reason") == "PROVIDER_RATE_LIMITED":
            result["last_reason"] = result.pop("reason")
        for series in result.get("series", []):
            if not isinstance(series, dict):
                continue
            if series.get("status") == "RATE_LIMITED":
                series["status"] = "DEFERRED"
            historical_flags = {"PROVIDER_RATE_LIMITED", "DEFERRED_AFTER_RATE_LIMIT"}
            if historical_flags.intersection(series.get("quality_flags", [])):
                series["last_quality_flags"] = copy.deepcopy(series["quality_flags"])
                series["quality_flags"] = [
                    flag for flag in series["quality_flags"] if flag not in historical_flags
                ]
            if isinstance(series, dict) and series.get("refresh_status") == "RATE_LIMITED":
                series["last_refresh_status"] = "RATE_LIMITED"
                series["refresh_status"] = result["refresh_status"]
    if remaining == 0 and result.get("status") in {"RATE_LIMITED", "DEFERRED", "QUEUED"}:
        result["status"] = "QUEUED" if requested else "DEFERRED"
        for series in result.get("series", []):
            if isinstance(series, dict) and series.get("status") == "RATE_LIMITED":
                series["status"] = "DEFERRED"
    if result.get("expires_at", 0) < now and result.get("status") in {"READY", "PARTIAL"}:
        result["status"] = "STALE"
        result["quality_flags"] = sorted(set(result.get("quality_flags", [])) | {"CACHE_EXPIRED"})
    result["snapshot_id"] = canonical_hash(
        {key: value for key, value in result.items() if key != "snapshot_id"}
    )
    return result


def cached_evidence(payload: dict, *, now: int) -> tuple[int, int]:
    """Count genuine same-dimensional points; STALE is displayable, not fresh alpha."""
    if (
        not isinstance(payload.get("match"), dict)
        or payload["match"].get("status") != "EXACT"
        or not isinstance(payload.get("scope"), dict)
        or not payload["scope"]
        or not isinstance(payload.get("instrument_id"), str)
        or not payload["instrument_id"].strip()
        or payload.get("timeframe") not in TIMEFRAME_SECONDS
    ):
        return 0, 0
    count, latest = 0, 0
    for series in payload.get("series", []):
        if not isinstance(series, dict):
            continue
        metric = METRICS.get(series.get("id"))
        if (
            not metric
            or series.get("status") not in {"READY", "PARTIAL", "STALE"}
            or series.get("unit_status") != "VERIFIED"
            or series.get("unit") != metric[1]
            or series.get("population") != metric[3]
            or series.get("native_timeframe") != payload.get("timeframe")
        ):
            continue
        for point in series.get("points", []):
            if not isinstance(point, dict):
                continue
            available, opened, closed = (
                epoch(point.get(key), -1) for key in ("available_at", "time", "close_time")
            )
            value = point.get("value")
            if (
                0 <= opened < closed <= available <= now
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                count += 1
                latest = max(latest, available)
    return count, latest


def project_refresh_state(
    payload: dict,
    *,
    now: int,
    exists: bool,
    eligible: bool,
    pending_since: int = 0,
    lease_until: int = 0,
    collection_started_at: int = 0,
) -> dict:
    """Queue state comes from the scheduler row, never from this GET's request flag."""
    result = copy.deepcopy(payload)
    due = epoch(result.get("next_refresh_at"))
    retry = epoch(result.get("retry_at"))
    if not exists:
        state = "NOT_REQUESTED"
    elif lease_until > now:
        state = "COLLECTING"
    elif not eligible:
        state = "IDLE"
    elif retry > now:
        state = "COOLDOWN"
    elif due > now:
        state = "SCHEDULED"
    else:
        state = "QUEUED"
    poll = {"COLLECTING": 3, "QUEUED": 5, "IDLE": 0, "NOT_REQUESTED": 0}.get(state)
    if poll is None:
        poll = max(1, retry - now) if state == "COOLDOWN" else max(5, due - now)
    count, latest = cached_evidence(result, now=now)
    result.update(
        refresh_state=state,
        queued_at=pending_since or None,
        queue_age_seconds=max(0, now - pending_since) if pending_since else 0,
        collection_started_at=collection_started_at if state == "COLLECTING" else None,
        next_eligible_at=due if exists and eligible else None,
        next_poll_after_seconds=poll,
        cache_available=count > 0,
        cached_points_count=count,
        last_success_at=latest or None,
    )
    result["snapshot_id"] = canonical_hash(
        {key: value for key, value in result.items() if key != "snapshot_id"}
    )
    return result
