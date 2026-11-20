from __future__ import annotations

from datetime import datetime, timezone, timedelta
from uuid import UUID

import psycopg

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository
from gatescope.adapters.postgres_market import PostgresMarketReadDatabase


TENANT = UUID("10000000-0000-0000-0000-000000000001")
USER = UUID("10000000-0000-0000-0000-000000000011")
INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")
WATCHLIST = UUID("40000000-0000-0000-0000-000000000044")


def test_formal_market_subscription_revision_and_heartbeat(migrated_database: str) -> None:
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            """INSERT INTO tenants (id,slug,name,status,created_at,updated_at)
               VALUES (%s,'market-test','Market Test','ACTIVE',now(),now())""",
            (TENANT,),
        )
        connection.execute(
            """INSERT INTO users
                 (id,normalized_username,display_username,password_hash,status,
                  password_changed_at,created_at,updated_at)
               VALUES (%s,'market-owner','Market Owner','fixture','ACTIVE',now(),now(),now())""",
            (USER,),
        )
        connection.execute(
            """INSERT INTO memberships
                 (id,tenant_id,user_id,role,status,created_at,updated_at)
               VALUES (gen_random_uuid(),%s,%s,'OWNER','ACTIVE',now(),now())""",
            (TENANT, USER),
        )
        connection.execute(
            """INSERT INTO instruments
                 (id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
               VALUES (%s,'GATE','FUTURES_USDT','BTC_USDT','BTC','USDT','USDT','ACTIVE',now(),now())""",
            (INSTRUMENT,),
        )
        connection.execute(
            """INSERT INTO watchlists
                 (id,tenant_id,owner_user_id,name,created_at,updated_at)
               VALUES (%s,%s,%s,'Primary',now(),now())""",
            (WATCHLIST, TENANT, USER),
        )
        connection.execute(
            """INSERT INTO watchlist_items
                 (id,tenant_id,watchlist_id,instrument_id,position,default_timeframe,created_at,updated_at)
               VALUES (gen_random_uuid(),%s,%s,%s,0,'15m',now(),now())""",
            (TENANT, WATCHLIST, INSTRUMENT),
        )

    pool = PostgresPool(migrated_database)
    pool.open()
    try:
        repository = PostgresMarketIngestRepository(pool)
        subscriptions = repository.subscriptions()
        assert {item.timeframe for item in subscriptions} == {"1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"}
        assert {
            (item.instrument_id, item.venue, item.market, item.symbol, item.settle_asset)
            for item in subscriptions
        } == {(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT")}
        timestamp = 1_788_840_000
        observed = datetime.fromtimestamp(timestamp + 1_800, tz=timezone.utc)
        row = (timestamp, 100.0, 102.0, 99.0, 101.0, 42.0)
        assert repository.upsert_final_bars(subscriptions[0], [row], observed_at=observed) == 1
        assert repository.upsert_final_bars(subscriptions[0], [row], observed_at=observed + timedelta(seconds=1)) == 0
        corrected = (timestamp, 100.0, 102.0, 99.0, 101.5, 42.0)
        assert repository.upsert_final_bars(subscriptions[0], [corrected], observed_at=observed + timedelta(seconds=2)) == 1
        assert repository.upsert_final_bars(subscriptions[0], [row], observed_at=observed) == 0
        assert repository.latest_open_time(subscriptions[0]) == timestamp
        repository.heartbeat(
            "integration-market",
            running=True,
            status={"catalog_count": 1, "subscription_count": 1, "inserted_bars": 2},
        )
        states = repository.sync_states(subscriptions)
        assert len(states) == 8
        subscription = subscriptions[0]
        now = datetime.now(timezone.utc)
        plan = repository.history_plan(subscription, now)
        assert plan["missing_bars"] > 0 and plan["gap"]
        assert (plan["gap"][1] - plan["gap"][0]) // 900 <= 899
        repository.mark_live(subscription, now)
        repository.mark_history(subscription, plan, now, state="BACKFILLING", delay=5)
        live_time = int(now.timestamp()) // 900 * 900
        live = (live_time, 101, 104, 100, 103, 15)
        repository.upsert_live_bar(subscription, [live], observed_at=now)
        reader = PostgresMarketReadDatabase(pool)
        # Stale instruments still expose their last cached candle, not an empty chart.
        assert reader.latest_candle_time(str(INSTRUMENT), "15m") == timestamp
        assert reader.latest_candle_time(str(INSTRUMENT), "1m") is None
        assert reader.latest_candle_time("invalid-id", "15m") is None
        cached = reader.cache_status(str(INSTRUMENT), '15m')
        assert cached["active"] and not cached["stale"]
        assert cached["live_bar"]["is_final"] is False
        assert not reader.get_canonical_candles(str(INSTRUMENT), '15m', live_time, live_time, 1)
        assert reader.watchlist_cache(TENANT, USER)[0]["price"] == "103"
        assert reader.watchlist_cache(TENANT, UUID("10000000-0000-0000-0000-000000000012")) == []
        final_rows = [(live_time - 900 * offset, 101, 104, 100, 103, 15) for offset in (1, 2, 4)]
        repository.upsert_final_bars(subscription, final_rows, observed_at=now)
        gap_plan = repository.history_plan(subscription, now)
        assert gap_plan["internal_gap"]
        assert gap_plan["gap"] == (live_time - 2700, live_time - 2700)
        repository.upsert_final_bars(subscription, [(live_time - 2700, 101, 104, 100, 103, 15)], observed_at=now)
        assert repository.history_plan(subscription, now)["gap"][1] < live_time - 2700
        repository.pause_provider(now, 123)
        assert all(s["next_live_at"] >= now + timedelta(seconds=123) for s in repository.sync_states(subscriptions).values())
        repository.sync_states([])
        assert repository.provider_retry_at() >= now + timedelta(seconds=123)
        assert not reader.cache_status(str(INSTRUMENT), '15m')["active"]
        recent = live_time - 900
        repository.upsert_final_bars(subscription, [(recent,101,104,100,103,15)], observed_at=now)
        assert reader.latest_candle_time(str(INSTRUMENT), "15m") == recent
    finally:
        pool.close()

    with psycopg.connect(migrated_database) as connection:
        revisions = connection.execute(
            """SELECT revision,quality_flags FROM bars
                WHERE instrument_id=%s AND timeframe='15m' AND open_time=to_timestamp(%s) ORDER BY revision""",
            (INSTRUMENT, timestamp),
        ).fetchall()
        heartbeat = connection.execute(
            "SELECT running,status_json FROM worker_heartbeats WHERE workload='market-worker'"
        ).fetchone()
    assert revisions == [
        (1, ["EXCHANGE_FINALITY_ASSUMED", "HISTORICAL_BACKFILL", "REST_POLLING"]),
        (2, ["EXCHANGE_FINALITY_ASSUMED", "HISTORICAL_BACKFILL", "REST_POLLING"]),
    ]
    assert heartbeat is not None and heartbeat[0] is True
    assert heartbeat[1]["subscription_count"] == 1
