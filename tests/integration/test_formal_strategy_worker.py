from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.postgres_market_ingest import MarketSubscription, PostgresMarketIngestRepository
from gatescope.adapters.postgres_strategy import PostgresStrategyRepository
from gatescope.application.alerts import AlertDecision, PostgresAlertWriter
from gatescope.application.strategy_runtime import FormalStrategyRunner
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.quant.structure_detection import VERSION


TENANT = UUID("10000000-0000-0000-0000-000000000001")
USER = UUID("10000000-0000-0000-0000-000000000011")
INSTRUMENT = UUID("30000000-0000-0000-0000-000000000033")


def test_formal_strategy_scans_once_persists_facts_and_enqueues_alert(
    migrated_database: str,
) -> None:
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            """INSERT INTO tenants (id,slug,name,status,created_at,updated_at)
               VALUES (%s,'strategy-test','Strategy Test','ACTIVE',now(),now())""",
            (TENANT,),
        )
        connection.execute(
            """INSERT INTO users
                 (id,normalized_username,display_username,password_hash,status,
                  password_changed_at,created_at,updated_at)
               VALUES (%s,'strategy-owner','Strategy Owner','fixture','ACTIVE',now(),now(),now())""",
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
        rule_id, version_id = uuid4(), uuid4()
        connection.execute(
            """INSERT INTO alert_rules
                 (id,tenant_id,owner_user_id,name,current_version,status,created_at,updated_at)
               VALUES (%s,%s,%s,'Causal reversal',1,'ACTIVE',now(),now())""",
            (rule_id, TENANT, USER),
        )
        connection.execute(
            """INSERT INTO alert_rule_versions
                 (id,tenant_id,alert_rule_id,version,condition_json,routing_json,created_by,created_at)
               VALUES (%s,%s,%s,1,%s::jsonb,'{"schema_version":1,"channel_ids":[]}',%s,now())""",
            (
                version_id,
                TENANT,
                rule_id,
                '{"schema_version":1,"instrument_id":"30000000-0000-0000-0000-000000000033",'
                '"timeframe":"1m","pattern_ids":["double_bottom"],"match_mode":"ANY",'
                '"minimum_confidence":0.7,"confirmed_only":true,"coincidence_bars":3,'
                '"cooldown_seconds":900}',
                USER,
            ),
        )

    pool = PostgresPool(migrated_database)
    pool.open()
    worker = PostgresPool(make_conninfo(migrated_database, options="-c role=gatescope_strategy"))
    worker.open()
    try:
        subscription = MarketSubscription(
            INSTRUMENT,
            "GATE",
            "FUTURES_USDT",
            "BTC_USDT",
            "USDT",
            "1m",
        )
        observed = datetime.now(timezone.utc)
        closed = observed.replace(second=0, microsecond=0)
        start = closed - timedelta(minutes=30)
        rows = [
            (int((start + timedelta(minutes=index)).timestamp()), 100.0, 102.0, 99.0, 101.0, 10.0)
            for index in range(30)
        ]
        PostgresMarketIngestRepository(pool).upsert_final_bars(
            subscription,
            rows,
            observed_at=observed,
        )
        latest_timestamp = rows[-1][0]

        event = {
            "id": "fixture-event",
            "pattern": "double_bottom",
            "name": "双底",
            "group": "反转形态",
            "signal_direction": "bullish",
            "experimental": False,
            "confidence": 0.82,
            "confirmed": True,
            "start_time": rows[-5][0],
            "end_time": latest_timestamp,
            "points": [],
            "lines": [],
            "region": None,
            "note": "fixture",
            "observed_time": int(closed.timestamp()),
            "phase": "CONFIRMED",
            "evidence_id": "e" * 64,
            "levels": {"invalid_side": -1, "invalid_price": 90, "buffer": 1},
        }
        record = {
            "id": event["id"],
            "phase": "CONFIRMED",
            "observed_time": event["observed_time"],
            "deadline_time": int((closed + timedelta(minutes=10)).timestamp()),
            "latest": event,
            "reason": "BREAKOUT",
            "snapshot_at": observed.isoformat(),
        }
        state = {"last_ts": latest_timestamp, "records": {event["id"]: record}}
        with psycopg.connect(migrated_database) as connection:
            connection.execute(
                "INSERT INTO market_sync_state(instrument_id,timeframe,last_live_at) VALUES(%s,'1m',%s)",
                (INSTRUMENT, observed),
            )
            connection.execute(
                "INSERT INTO market_pattern_tracking(instrument_id,timeframe,version,checkpoint_json) VALUES(%s,'1m',%s,%s)",
                (INSTRUMENT, VERSION, Jsonb(state)),
            )
            connection.execute(
                """INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,
                timeframe,version,source_mode,phase,observed_time,recorded_at,event_json)
                VALUES(%s,%s,1,%s,'1m',%s,'RECORDED','CONFIRMED',%s,%s,%s)""",
                (
                    context_id(INSTRUMENT, "1m"),
                    event["id"],
                    INSTRUMENT,
                    VERSION,
                    closed,
                    observed - timedelta(milliseconds=5),
                    Jsonb(record),
                ),
            )
        subject = uuid5(NAMESPACE_URL, "gatescope:test:strategy")
        runner = FormalStrategyRunner(
            PostgresStrategyRepository(worker),
            PostgresAlertWriter(worker),
            subject_id=subject,
            clock=lambda: observed,
        )
        first = runner.run_once()
        second = runner.run_once()
        assert (first.rules, first.scanned, first.emitted, first.failed) == (1, 1, 1, 0)
        assert (second.scanned, second.emitted, second.failed) == (0, 0, 0)
        # A presentation-only schema upgrade changes the fact hash, not the
        # original structure evidence. It must not re-notify this lifecycle.
        with psycopg.connect(migrated_database) as connection:
            old_payload = connection.execute("SELECT payload_json FROM alert_event_facts").fetchone()[0]
        old_payload["matched"][0]["fact_id"] = "new-metadata-revision"
        old_payload["event_key"] = "d" * 64
        revised = json.dumps(old_payload, sort_keys=True, separators=(",", ":"))
        duplicate = AlertDecision("d" * 64, rule_id, version_id, INSTRUMENT, "1m", observed,
            ("new-metadata-revision",), hashlib.sha256(revised.encode()).hexdigest(), revised)
        assert PostgresAlertWriter(worker).record(TENANT, subject, duplicate) is False
        restarted = FormalStrategyRunner(
            PostgresStrategyRepository(worker),
            PostgresAlertWriter(worker),
            subject_id=subject,
            clock=lambda: observed,
        )
        assert restarted.run_once().emitted == 0
        with psycopg.connect(migrated_database) as connection:
            connection.execute(
                "UPDATE market_sync_state SET last_live_at=%s", (observed - timedelta(seconds=46),)
            )
        assert (
            PostgresStrategyRepository(worker).structure_snapshot(
                TENANT, subject, INSTRUMENT, "1m", now=observed
            )
            is None
        )
    finally:
        worker.close()
        pool.close()

    with psycopg.connect(migrated_database) as connection:
        assert connection.execute("SELECT count(*) FROM pattern_event_facts").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM alert_event_facts").fetchone()[0] == 1
        version, payload = connection.execute(
            "SELECT detector_version,payload_json FROM pattern_event_facts"
        ).fetchone()
        assert version == VERSION and payload["evidence_id"] == "e" * 64
        alert = connection.execute("SELECT payload_json FROM alert_event_facts").fetchone()[0]
        assert alert["matched"][0]["context_id"] == context_id(INSTRUMENT, "1m")
        assert alert["matched"][0]["phase"] == "CONFIRMED"
        assert alert["matched"][0]["trade_notice"]["entry"]["price"] == 101.0
        assert alert["matched"][0]["trade_notice"]["auto_order"] is False
        assert alert["matched"][0]["source_event"]["source_snapshot_at"] == observed.isoformat()
        assert alert["observed_at"] == closed.isoformat()
        assert alert["expires_at"] == (closed + timedelta(minutes=2)).isoformat()
        queued = connection.execute(
            "SELECT event_type FROM outbox_events WHERE event_type='notification.requested.v1'"
        ).fetchone()
        assert queued == ("notification.requested.v1",)
