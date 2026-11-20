from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Barrier

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.legacy_sqlite import legacy_instrument_uuid
from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
METADATA = {"name": "BTC_USDT", "type": "direct", "quanto_multiplier": "0.0001",
            "order_price_round": "0.1", "order_size_min": "0.1", "order_size_max": "1000000",
            "status": "trading", "in_delisting": False, "create_time": 1669688556,
            "taker_fee_rate": "0.0005", "risk_limit_max": "10000000", "funding_interval": 28800,
            "mark_price": "60000", "position_size": "20000", "funding_rate": "0.0001"}
ROW = {"id": "futures:usdt:BTC_USDT", "market": "futures", "settle": "usdt",
       "symbol": "BTC_USDT", "base": "BTC", "quote": "USDT", "status": "tradable",
       "metadata": METADATA}
INSTRUMENT = legacy_instrument_uuid(ROW["id"])


def open_repo(database_url):
    pool = PostgresPool(make_conninfo(database_url, options="-c role=gatescope_market"), max_size=2)
    pool.open()
    return pool, PostgresMarketIngestRepository(pool)


def test_rule_revisions_ignore_quotes_retain_rules_and_allow_restoration(migrated_database):
    pool, repo = open_repo(migrated_database)
    try:
        assert repo.refresh_catalog([ROW], observed_at=NOW) == 1
        with psycopg.connect(migrated_database) as c:
            initial = c.execute("SELECT xmin::text,updated_at FROM instruments WHERE id=%s", (INSTRUMENT,)).fetchone()
        noise = {**METADATA, "mark_price": "90000", "position_size": "99999", "funding_rate": "-0.001",
                 "orderbook_id": 999, "config_change_time": 1880000000}
        assert repo.refresh_catalog([{**ROW, "metadata": noise}], observed_at=NOW + timedelta(minutes=30)) == 0
        with psycopg.connect(migrated_database) as c:
            assert c.execute("SELECT xmin::text,updated_at FROM instruments WHERE id=%s", (INSTRUMENT,)).fetchone() == initial
            assert c.execute("SELECT count(*) FROM instrument_revisions WHERE instrument_id=%s", (INSTRUMENT,)).fetchone()[0] == 1
        # A -> B -> A must remain three distinct historical transitions.
        repo.refresh_catalog([{**ROW, "metadata": {**METADATA, "order_price_round": "0.01"}}], observed_at=NOW + timedelta(hours=1))
        repo.refresh_catalog([ROW], observed_at=NOW + timedelta(hours=2))
        with psycopg.connect(migrated_database) as c:
            history = c.execute("SELECT revision,metadata_json,payload_hash FROM instrument_revisions WHERE instrument_id=%s ORDER BY revision", (INSTRUMENT,)).fetchall()
        assert [r[0] for r in history] == [1, 2, 3]
        assert [r[1]["order_price_round"] for r in history] == ["0.1", "0.01", "0.1"]
        for _, metadata, digest in history:
            canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            assert hashlib.sha256(canonical.encode()).hexdigest() == digest
            assert "mark_price" not in metadata and "position_size" not in metadata and "funding_rate" not in metadata
            assert metadata["create_time"] == METADATA["create_time"]
        # Listing, delisting, limits and fees must not be treated as quote noise.
        for i, (field, value) in enumerate([
            ("create_time", 1669689000), ("status", "delisting"), ("in_delisting", True),
            ("taker_fee_rate", "0.0006"), ("risk_limit_max", "9000000"), ("funding_interval", 14400),
        ], 4):
            repo.refresh_catalog([{**ROW, "metadata": {**METADATA, field: value}}], observed_at=NOW + timedelta(hours=i))
            with psycopg.connect(migrated_database) as c:
                latest = c.execute("SELECT revision,metadata_json FROM instrument_revisions WHERE instrument_id=%s ORDER BY revision DESC LIMIT 1", (INSTRUMENT,)).fetchone()
                assert latest[0] == i and latest[1][field] == value
        # A normalized state change not present in old provider metadata is also
        # an actual version, and does update the catalog status timestamp.
        disabled_at = NOW + timedelta(days=1)
        assert repo.refresh_catalog([{**ROW, "status": "disabled"}], observed_at=disabled_at) == 1
        with psycopg.connect(migrated_database) as c:
            assert c.execute("SELECT status,updated_at FROM instruments WHERE id=%s", (INSTRUMENT,)).fetchone() == ("DISABLED", disabled_at)
    finally:
        pool.close()


def test_legacy_raw_metadata_is_untouched_and_concurrent_same_rules_only_append_once(migrated_database):
    raw = json.dumps(METADATA, sort_keys=True, separators=(",", ":"))
    with psycopg.connect(migrated_database) as c:
        c.execute("""INSERT INTO instruments(id,venue,market,symbol,base_asset,quote_asset,settle_asset,status,created_at,updated_at)
            VALUES(%s,'GATE','FUTURES_USDT','BTC_USDT','BTC','USDT','USDT','ACTIVE',%s,%s)""", (INSTRUMENT, NOW, NOW))
        c.execute("""INSERT INTO instrument_revisions(id,instrument_id,revision,effective_at,available_at,metadata_json,payload_hash,created_at)
            VALUES(gen_random_uuid(),%s,733,%s,%s,%s,%s,%s)""",
            (INSTRUMENT, NOW, NOW, Jsonb(METADATA), hashlib.sha256(raw.encode()).hexdigest(), NOW))
    pool, repo = open_repo(migrated_database)
    try:
        assert repo.refresh_catalog([{**ROW, "metadata": {**METADATA, "mark_price": "88888"}}], observed_at=NOW + timedelta(minutes=30)) == 0
        with psycopg.connect(migrated_database) as c:
            assert c.execute("SELECT revision,metadata_json FROM instrument_revisions WHERE instrument_id=%s", (INSTRUMENT,)).fetchall() == [(733, METADATA)]
        barrier = Barrier(3)
        new_row = {**ROW, "metadata": {**METADATA, "order_size_max": "500000"}}
        def refresh():
            barrier.wait(timeout=10)
            return repo.refresh_catalog([new_row], observed_at=NOW + timedelta(hours=1))
        with ThreadPoolExecutor(max_workers=2) as executor:
            # The pre-held row lock forces both writers through the concurrent
            # path. Neither may allocate its revision before the lock is free.
            with psycopg.connect(migrated_database) as holder:
                holder.execute("SELECT id FROM instruments WHERE id=%s FOR UPDATE", (INSTRUMENT,))
                futures = [executor.submit(refresh) for _ in range(2)]
                barrier.wait(timeout=10)
                assert not any(f.done() for f in futures)
            assert [f.result(timeout=15) for f in futures] == [0, 0]
        with psycopg.connect(migrated_database) as c:
            history = c.execute("SELECT revision,metadata_json FROM instrument_revisions WHERE instrument_id=%s ORDER BY revision", (INSTRUMENT,)).fetchall()
            assert [r[0] for r in history] == [733, 734]
            assert history[0][1] == METADATA
            assert history[1][1]["order_size_max"] == "500000"
    finally:
        pool.close()


def test_spot_precision_status_and_missing_listing_are_distinct_rules(migrated_database):
    metadata = {"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "precision": 0,
                "amount_precision": 8, "min_base_amount": None, "min_quote_amount": "1",
                "trade_status": "tradable", "buy_start": 1516378650, "sell_start": 1516378650,
                "market_order_max_stock": "0", "market_order_max_money": None}
    row = {**ROW, "id": "spot::BTC_USDT", "market": "spot", "settle": "", "metadata": metadata}
    identifier = legacy_instrument_uuid(row["id"])
    pool, repo = open_repo(migrated_database)
    try:
        assert repo.refresh_catalog([row], observed_at=NOW) == 1
        repo.refresh_catalog([{**row, "metadata": {**metadata, "last": "80000", "volume": "123456"}}],
                             observed_at=NOW + timedelta(minutes=30))
        revision_2 = {**metadata, "precision": 1}
        repo.refresh_catalog([{**row, "metadata": revision_2}], observed_at=NOW + timedelta(hours=1))
        revision_3 = {key: value for key, value in revision_2.items() if key != "buy_start"}
        repo.refresh_catalog([{**row, "metadata": revision_3}], observed_at=NOW + timedelta(hours=2))
        repo.refresh_catalog([{**row, "status": "untradable", "metadata": {**revision_3, "trade_status": "untradable"}}],
                             observed_at=NOW + timedelta(hours=3))
        with psycopg.connect(migrated_database) as c:
            history = c.execute("SELECT revision,metadata_json,tick_size,lot_size,min_notional FROM instrument_revisions WHERE instrument_id=%s ORDER BY revision", (identifier,)).fetchall()
            assert [r[0] for r in history] == [1, 2, 3, 4]
            assert history[0][1]["precision"] == 0 and history[0][1]["min_base_amount"] is None
            assert history[1][1]["precision"] == 1
            assert "buy_start" not in history[2][1]
            assert history[3][1]["trade_status"] == "untradable"
            # This change does not invent a quantity step from min order size,
            # or synthesize incomplete instrument rules for execution consumers.
            assert all(item[2:] == (None, None, None) for item in history)
            assert c.execute("SELECT status FROM instruments WHERE id=%s", (identifier,)).fetchone()[0] == "DISABLED"
    finally:
        pool.close()
