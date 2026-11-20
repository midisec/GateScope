from datetime import datetime, timedelta, timezone
import hashlib

import psycopg

from gatescope.adapters.annual_structure_study import page
from tests.integration.test_postgres_rls import INSTRUMENT, seed_two_tenants


def test_bounded_pages_match_original_sequence_across_gaps_revisions_and_time_chunks(migrated_database):
    seed_two_tenants(migrated_database)
    origin = datetime(2025, 1, 1, tzinfo=timezone.utc)
    as_of = datetime(2025, 2, 1, tzinfo=timezone.utc)
    start = int(origin.timestamp())
    end = start + 20162 * 60
    with psycopg.connect(migrated_database) as connection:
        def add(minute, revision=1, *, future_available=False, future_created=False, final=True, future_close=False):
            opened = origin + timedelta(minutes=minute)
            closed = as_of + timedelta(seconds=1) if future_close else opened + timedelta(minutes=1)
            available = as_of + timedelta(seconds=1) if future_available else as_of - timedelta(seconds=10)
            created = as_of + timedelta(seconds=1) if future_created else as_of - timedelta(seconds=10)
            digest = hashlib.sha256(f"{minute}:{revision}".encode()).hexdigest()
            connection.execute("""INSERT INTO bars
                (venue,market,instrument_id,timeframe,open_time,close_time,open,high,low,close,volume,is_final,
                 revision,available_at,payload_hash,quality_flags,created_at)
                VALUES('GATE','FUTURES_USDT',%s,'1m',%s,%s,100,500,1,%s,10,%s,%s,%s,%s,'{}',%s)""",
                (INSTRUMENT, opened, closed, 100 + revision, final, revision, available, digest, created))

        for minute in (-1, 0, 1, 2, 10, 11, 12, 20160, 20161, 20162, 20163):
            add(minute)
        add(1, 2)
        add(1, 3, future_available=True)
        add(2, 2, future_created=True)
        add(3, future_available=True)
        add(4, future_created=True)
        add(5, final=False)
        add(6, future_close=True)

        def original(after):
            rows = connection.execute("""SELECT DISTINCT ON(open_time)
                open_time,revision,payload_hash,close FROM bars
                WHERE instrument_id=%s AND timeframe='1m' AND is_final=true
                  AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s) AND open_time>to_timestamp(%s)
                  AND available_at<=%s AND created_at<=%s AND close_time<=%s
                ORDER BY open_time,revision DESC""",
                (INSTRUMENT, start, end, after, as_of, as_of, as_of)).fetchall()
            return [(int(row[0].timestamp()), row[1], row[2], float(row[3])) for row in rows]

        for limit in (1, 2, 3, 7):
            for initial_after in (start - 1, start, start + 2 * 60, start + 20162 * 60):
                after, collected = initial_after, []
                for _ in range(30):
                    batch = page(connection, INSTRUMENT, "1m", as_of, start, end, after, limit=limit)
                    if not batch:
                        break
                    assert len(batch) <= limit
                    assert all(after < row["ts"] <= end for row in batch)
                    assert batch[-1]["ts"] < batch[0]["ts"] + limit * 60
                    collected.extend((row["ts"], row["revision"], row["payload_hash"], row["close"]) for row in batch)
                    after = batch[-1]["ts"]
                else:
                    raise AssertionError("page cursor failed to terminate")
                assert collected == original(initial_after)
        # An exclusive window edge is returned on the following page, not lost.
        first = page(connection, INSTRUMENT, "1m", as_of, start, end, start - 1, limit=2)
        second = page(connection, INSTRUMENT, "1m", as_of, start, end, first[-1]["ts"], limit=2)
        assert [row["ts"] for row in first] == [start, start + 60]
        assert [row["ts"] for row in second] == [start + 120]
        assert first[-1]["revision"] == 2
        assert second[-1]["revision"] == 1
