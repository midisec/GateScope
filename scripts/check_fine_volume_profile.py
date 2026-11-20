"""Check staged VPVR against production market facts using read-only transactions.

The ASGI router is tested in-process with a synthetic diagnostic session; this is
not a production login test. It reads only public instruments/candles. No provider
requests, migrations, credentials, account settings or order writes are performed.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.volume_profile import build_volume_profile_router
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from prepare_service_envs import SECRET_DIR, read_env


class ReadOnlyPool(PostgresPool):
    @contextmanager
    def public_transaction(self):
        with super().public_transaction() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            yield connection


def diagnostic_app(pool):
    app = FastAPI()

    @app.middleware("http")
    async def diagnostic_session(request: Request, call_next):
        request.state.trace_id = "vpvr-readonly-diagnostic"
        request.state.auth_session = SimpleNamespace(
            user_id=UUID("88d72a6a-8c25-4323-a7e5-67ebd1ee193c"),
            tenant_id=UUID("88d72a6a-8c25-4323-a7e5-67ebd1ee193d"),
            fully_authenticated=True,
        ) if request.headers.get("X-Diagnostic-Session") == "1" else None
        return await call_next(request)

    app.include_router(build_volume_profile_router(pool))
    return app


def main():
    logging.disable(logging.CRITICAL)
    vpvr_env = read_env(SECRET_DIR / "formal-runtime.env")
    pool = ReadOnlyPool(vpvr_env["GATESCOPE_DATABASE_URL"], min_size=1, max_size=2)
    pool.open()
    results = []
    try:
        reader = PostgresMarketReadDatabase(pool)
        with TestClient(diagnostic_app(pool)) as client:
            for symbol, days in (("BTC_USDT", 3), ("ETH_USDT", 3), ("SOL_USDT", 3), ("龙虾_USDT", 3), ("BTC_USDT", 34), ("BTC_USDT", 180)):
                instruments = reader.search_instruments(symbol, "futures:usdt", 10, 0)
                instrument = next(item for item in instruments if item["symbol"] == symbol)
                latest = reader.latest_candle_time(instrument["id"], "1h")
                assert latest is not None
                end = latest + 3600
                params = {"instrument_id": instrument["id"], "timeframe": "1h",
                          "from_time": end - days * 86400, "to_time": end, "rows": 96, "value_area": 70}
                if not results:
                    assert client.get("/api/v2/market/volume-profile", params=params).status_code == 401
                started = time.perf_counter()
                response = client.get("/api/v2/market/volume-profile", params=params, headers={"X-Diagnostic-Session": "1"})
                elapsed = round((time.perf_counter() - started) * 1000)
                assert response.status_code == 200, f"VPVR_HTTP_{response.status_code}"
                payload = response.json()
                assert payload["instrument_id"] == instrument["id"] and payload["timeframe"] == "1h"
                assert payload["from_time"] == params["from_time"] and payload["to_time"] == params["to_time"]
                assert payload["source_timeframe"] == ("1m" if days <= 34 else "15m")
                assert 0 < payload["used_bars"] <= payload["expected_bars"] <= 50000
                assert 0 < payload["coverage"] <= 1
                profile = payload["profile"]
                assert profile["status"] == "AVAILABLE"
                assert abs(sum(row["volumeFraction"] for row in profile["bins"]) - 1) < 1e-8
                assert 0 < profile["val"] <= profile["poc"] <= profile["vah"]
                started = time.perf_counter()
                repeat = client.get("/api/v2/market/volume-profile", params=params, headers={"X-Diagnostic-Session": "1"})
                cached_ms = round((time.perf_counter() - started) * 1000)
                assert repeat.status_code == 200 and repeat.json()["generated_at"] == payload["generated_at"]
                results.append({"symbol": symbol, "days": days, "source": payload["source_timeframe"],
                                "expected": payload["expected_bars"], "used": payload["used_bars"],
                                "coverage": payload["coverage"], "status": payload["status"],
                                "poc": profile["poc"], "elapsed_ms": elapsed, "cached_ms": cached_ms})
        print(json.dumps({"status": "PASSED", "data": "REAL_CACHED_MARKET_READ_ONLY", "checks": results}, ensure_ascii=False))
    finally:
        pool.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
