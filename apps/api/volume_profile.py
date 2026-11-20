"""Authenticated, rate-limited public-market VPVR reads (no ingestion/writes)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from gatescope.adapters.postgres_volume_profile import PostgresVolumeProfileRepository
from gatescope.domain.volume_profile import CACHE_SECONDS, VolumeProfileInputError, plan_volume_profile
from gatescope.security import SlidingWindowLimiter


class ProfileBusy(Exception):
    pass


class ProfileResultCache:
    """Per-router process-local cap: 2 DB/CPU jobs, 64 results, 30 seconds.

Concurrent identical requests do not duplicate work or block an HTTP thread:
they receive a short Retry-After. Cached objects contain public market data
only. Auth and per-user quota are still checked before every cache hit.
"""
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._pending = set()

    def get(self, key, factory):
        now = time.monotonic()
        with self._lock:
            for stale in [key for key, (until, _) in self._cache.items() if until <= now]:
                self._cache.pop(stale)
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key][1]
            if key in self._pending or len(self._pending) >= 2:
                raise ProfileBusy()
            self._pending.add(key)
        try:
            result = factory()
            with self._lock:
                # TTL starts with generation, not after a potentially slow query.
                remaining = max(0, result["expires_at"]-time.time())
                if remaining:
                    self._cache[key] = (time.monotonic()+min(CACHE_SECONDS, remaining), result)
                    while len(self._cache) > 64:
                        self._cache.popitem(last=False)
            return result
        finally:
            with self._lock:
                self._pending.discard(key)


def build_volume_profile_router(pool):
    router = APIRouter(prefix="/api/v2/market", tags=["volume-profile"])
    repository, limiter, cache = PostgresVolumeProfileRepository(pool), SlidingWindowLimiter(), ProfileResultCache()

    @router.get("/volume-profile")
    def volume_profile(request: Request, response: Response, instrument_id: UUID,
                       timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"],
                       from_time: int = Query(ge=0), to_time: int = Query(ge=1),
                       rows: int = Query(default=48, ge=24, le=96), value_area: int = Query(default=70, ge=50, le=95)):
        session = getattr(request.state, "auth_session", None)
        if session is None:
            raise HTTPException(401, "请先登录")
        allowed, retry = limiter.allow(f"vpvr:{session.tenant_id}:{session.user_id}", 60, 60)
        if not allowed:
            raise HTTPException(429, "请求较频繁，请稍后重试", headers={"Retry-After": str(retry)})
        response.headers["Cache-Control"] = "private, no-store"
        asof = int(time.time())
        try:
            plan = plan_volume_profile(timeframe, from_time, to_time, rows=rows, value_area=value_area, asof=asof)
            key = (str(instrument_id), timeframe, from_time, to_time, rows, value_area)
            return cache.get(key, lambda: repository.get(instrument_id, plan, asof=asof))
        except VolumeProfileInputError as exc:
            raise HTTPException(422, str(exc)) from None
        except LookupError:
            raise HTTPException(404, "交易标的不存在") from None
        except ProfileBusy:
            raise HTTPException(429, "成交量分布计算繁忙，请稍后重试", headers={"Retry-After": "2"}) from None
        except Exception:
            # Includes pool exhaustion/timeouts: no SQL, connection string,
            # implementation details or provider credentials escape to clients.
            raise HTTPException(503, "成交量分布暂不可用，请稍后重试", headers={"Retry-After": "5"}) from None

    return router
