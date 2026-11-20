from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_telegram_market import TelegramMarketRepository
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.adapters.postgres_market_ingest import PostgresMarketIngestRepository, MarketSubscription
from gatescope.quant.structure_detection import VERSION
from gatescope.application.telegram_market_runtime import TelegramMarketRuntime
from gatescope.application.notifications import DeliveryOutcome
from tests.integration.test_telegram_market import setup_data
from tests.integration.test_postgres_rls import TENANT_A, TENANT_B, USER_A, USER_B, INSTRUMENT


def test_screen_real_sql_is_causal_stage_specific_and_user_owned(migrated_database):
    setup_data(migrated_database)
    now = datetime.now(timezone.utc)
    closed = int(now.timestamp()) // 3600 * 3600
    step = 3600
    requested = now - timedelta(minutes=1)
    context = context_id(INSTRUMENT, "1h", requested)
    record = {
        "id": "a" * 64,
        "pattern": "double_bottom",
        "phase": "FORMED",
        "observed_time": closed - step,
        "first_observed_time": closed - step,
        "deadline_time": closed + 100 * step,
        "first": {"start_time": closed - 100 * step},
        "latest": {
            "name": "双底",
            "confidence": 0.8,
            "signal_direction": "bullish",
            "levels": {"invalid_side": -1, "invalid_price": 90.0, "buffer": 1.0},
        },
    }
    state = {"last_ts": closed - step, "records": {record["id"]: record}}
    with psycopg.connect(migrated_database) as c:
        c.execute(
            "INSERT INTO market_sync_state(instrument_id,timeframe,last_live_at) VALUES(%s,'1h',%s)",
            (INSTRUMENT, now),
        )
        c.execute(
            "INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json) VALUES(%s,'1h',%s,%s)",
            (INSTRUMENT, VERSION, Jsonb(state)),
        )
        stored_requested = c.execute(
            "INSERT INTO market_pattern_studies(instrument_id,timeframe,version,state,requested_at,cursor,total) VALUES(%s,'1h',%s,'RUNNING',%s,5000,8760) RETURNING requested_at",
            (INSTRUMENT, VERSION, requested),
        ).fetchone()[0]
        # The existing context key uses the database timestamp representation;
        # exercise the same timezone round-trip as the replay worker.
        context = context_id(INSTRUMENT, "1h", stored_requested)
        cases = []
        for i in range(200):
            observed = closed - (5000 - i * 21) * step
            cases.append(
                (
                    context,
                    str(i),
                    "double_bottom",
                    "FORMED",
                    20,
                    observed,
                    "COMPLETE",
                    observed,
                    observed + 20 * step,
                    1,
                    0.01 if i < 160 else -0.02,
                )
            )
        base = cases[0]
        # A different stage/side/horizon/model, an overlapping sample, and
        # outcomes after the current pattern's start must NEVER raise its rate.
        cases.extend(
            [
                (base[0], "wrong-stage", base[2], "CONFIRMED", *base[4:]),
                (*base[:1], "wrong-side", *base[2:9], -1, 0.9),
                (*base[:1], "wrong-horizon", *base[2:4], 5, *base[5:]),
                ("old-model", "wrong-context", *base[2:]),
                (*base[:1], "overlap", *base[2:6], "OVERLAP", *base[7:]),
                (
                    context,
                    "same-structure",
                    "double_bottom",
                    "FORMED",
                    20,
                    closed - 30 * step,
                    "COMPLETE",
                    closed - 30 * step,
                    closed - 10 * step,
                    1,
                    0.9,
                ),
                (
                    context,
                    "future",
                    "double_bottom",
                    "FORMED",
                    20,
                    closed - step,
                    "COMPLETE",
                    closed - step,
                    closed + 19 * step,
                    1,
                    0.9,
                ),
            ]
        )
        with c.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO market_structure_returns(context_id,evidence_id,pattern,phase,horizon,observed_time,status,entry_time,exit_time,side,net_return)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                cases,
            )
    admin = PostgresPool(migrated_database)
    notification = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    admin.open()
    notification.open()
    try:
        rows = [(closed - (4 - i) * step, 100, 104, 98, 101, 10) for i in range(4)]
        PostgresMarketIngestRepository(admin).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            rows,
            observed_at=now,
        )
        repo = TelegramMarketRepository(notification)
        result = repo.opportunities(TENANT_A, USER_A)
        assert result["fresh_pairs"] == 1 and result["pending_pairs"] == 7
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["statistics"]["sample_count"] == 200, item["statistics"]
        assert item["statistics"]["win_rate"] == 0.8
        assert item["qualified"], item["reasons"]
        assert item["statistics"]["mean_net_return"] == pytest.approx(0.004)
        assert repo.opportunities(TENANT_B, USER_B)["items"] == []
        # Another owner in the SAME workspace sees no private watchlist entries.
        assert repo.opportunities(TENANT_A, USER_B)["items"] == []
        with notification.public_transaction() as c:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE market_structure_returns SET status=status")
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_sync_state SET last_live_at=now()-interval '2 minutes'")
        result = repo.opportunities(TENANT_A, USER_A)
        assert not result["items"] and result["pending_pairs"] == 8
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_sync_state SET last_live_at=now()")
            c.execute(
                "INSERT INTO market_live_candles VALUES(%s,'1h',to_timestamp(%s),to_timestamp(%s),100,110,80,85,10,now())",
                (INSTRUMENT, closed, closed + step),
            )
        result = repo.opportunities(TENANT_A, USER_A)
        assert not result["items"] and result["unusable"] == 1
        # Recovery after a post-confirmation invalidating close must not revive
        # the candidate in a fresh screen (the original journal stays intact).
        record["phase"] = "CONFIRMED"
        record["observed_time"] = closed - 2 * step
        with psycopg.connect(migrated_database) as c:
            c.execute("UPDATE market_live_candles SET close=101,low=98")
            c.execute("UPDATE market_pattern_tracking SET checkpoint_json=%s", (Jsonb(state),))
        PostgresMarketIngestRepository(admin).upsert_final_bars(
            MarketSubscription(INSTRUMENT, "GATE", "FUTURES_USDT", "BTC_USDT", "USDT", "1h"),
            [(closed - 2 * step, 100, 104, 80, 88, 10)],
            observed_at=datetime.now(timezone.utc),
        )
        result = repo.opportunities(TENANT_A, USER_A)
        assert not result["items"] and result["unusable"] == 1
    finally:
        admin.close()
        notification.close()


def test_push_entry_queues_only_current_private_owner_and_runtime_delivers(migrated_database):
    setup_data(migrated_database)
    api = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_api"))
    notification = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_notification"))
    api.open()
    notification.open()
    try:
        repo = TelegramMarketRepository(api)
        a = SimpleNamespace(tenant_id=TENANT_A, user_id=USER_A)
        b = SimpleNamespace(tenant_id=TENANT_B, user_id=USER_B)
        assert repo.enqueue(b, 1, 42, callback="gm:opportunities") == "UNBOUND"
        assert repo.enqueue(a, 1, 42, callback="gm:opportunities") == "QUEUED"
        assert repo.enqueue(a, 1, 42, callback="gm:opportunities") == "DUPLICATE"
        with psycopg.connect(migrated_database) as c:
            c.execute(
                "UPDATE telegram_market_requests SET response_json=%s WHERE update_id=1",
                (Jsonb({"acceptance_test": True, "text": "stale snapshot", "actions": [], "chart": None}),),
            )
        sent = []

        class Sender:
            def send_market(self, chat, text, markup, photo):
                sent.append(text)
                assert photo is None
                return DeliveryOutcome(True, None, None, 1)

        runtime = TelegramMarketRuntime(
            TelegramMarketRepository(notification), lambda t: Sender(), "screening-test", uuid4()
        )
        assert runtime.run_once() == 1 and len(sent) == 1 and "没有达标形态" in sent[0]
        assert sent[0].startswith("部署验收") and "stale snapshot" not in sent[0]
    finally:
        api.close()
        notification.close()
