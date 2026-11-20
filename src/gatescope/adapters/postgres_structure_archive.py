"""Public-market evidence only. Journals and reports cannot be updated or deleted."""

from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.pattern_study import digest
from gatescope.quant.structure_archive_codec import pack_structure_record, unpack_structure_record


def context_id(instrument, timeframe, requested=None, *, version=VERSION):
    return digest([str(instrument), timeframe, version, requested.isoformat() if requested else "RECORDED"])


def terminal_candidates(conn, context, identifiers):
    if not identifiers:
        return set()
    rows = conn.execute(
        """SELECT DISTINCT candidate_id FROM market_pattern_events WHERE context_id=%s
        AND candidate_id=ANY(%s) AND phase IN ('CONFIRMED','INVALIDATED','EXPIRED','CENSORED')""",
        (context, identifiers),
    ).fetchall()
    return {r[0] for r in rows}


def write_journal(conn, instrument, timeframe, context, journal, source="REPLAY", *, version=VERSION):
    for transition in journal:
        header, body = pack_structure_record(transition["record"])
        conn.execute(
            """INSERT INTO market_pattern_events(context_id,candidate_id,sequence,instrument_id,timeframe,version,
          source_mode,phase,from_phase,observed_time,event_json,event_blob) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT DO NOTHING""",
            (
                context,
                transition["candidate_id"],
                transition["sequence"],
                instrument,
                timeframe,
                version,
                source,
                transition["phase"],
                transition["from_phase"],
                datetime.fromtimestamp(transition["observed_time"], timezone.utc),
                Jsonb(header),
                body,
            ),
        )


def archive_report(conn, instrument, timeframe, context, report):
    conn.execute(
        """INSERT INTO market_pattern_reports(context_id,instrument_id,timeframe,version,report_json)
      VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (context, instrument, timeframe, VERSION, Jsonb(report)),
    )
    conn.execute(
        """INSERT INTO market_pattern_report_versions(context_id,snapshot_at,report_json)
      VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
        (context, report["metadata"]["snapshot_at"], Jsonb(report)),
    )


class StructureArchive:
    def __init__(self, pool):
        self.pool = pool

    def list(
        self,
        instrument,
        timeframe,
        *,
        source="RECORDED",
        phase=None,
        pattern=None,
        offset=0,
        limit=12,
        requested_context=None,
    ):
        with self.pool.public_transaction() as conn:
            context = context_id(instrument, timeframe)
            contexts = []
            if source == "REPLAY":
                study = conn.execute(
                    "SELECT requested_at FROM market_pattern_studies WHERE instrument_id=%s AND timeframe=%s AND version=%s",
                    (instrument, timeframe, VERSION),
                ).fetchone()
                if study:
                    context = context_id(instrument, timeframe, study[0])
                archived = conn.execute(
                    """SELECT context_id,created_at,report_json->>'bar_count' FROM market_pattern_reports
                  WHERE instrument_id=%s AND timeframe=%s AND context_id ~ '^[a-f0-9]{64}$' ORDER BY created_at DESC LIMIT 100""",
                    (instrument, timeframe),
                ).fetchall()
                contexts = (
                    [{"id": context, "created_at": study[0], "bar_count": None, "version": VERSION}]
                    if study
                    else []
                )
                contexts.extend(
                    {"id": r[0], "created_at": r[1], "bar_count": r[2]} for r in archived if r[0] != context
                )
                # Failed/interrupted replay prefixes also have immutable evidence,
                # even if they never reached the first report checkpoint.
                partials = conn.execute(
                    """SELECT context_id,min(recorded_at),version FROM market_pattern_events
                    WHERE instrument_id=%s AND timeframe=%s AND source_mode='REPLAY'
                    GROUP BY context_id,version ORDER BY min(recorded_at) DESC LIMIT 100""",
                    (instrument, timeframe),
                ).fetchall()
                known = {c["id"] for c in contexts}
                contexts.extend(
                    {"id": r[0], "created_at": r[1], "bar_count": None, "version": r[2]}
                    for r in partials
                    if r[0] not in known
                )
                if not study and contexts:
                    context = contexts[0]["id"]
            else:
                tracked = conn.execute(
                    "SELECT version,requested_at FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s ORDER BY requested_at DESC",
                    (instrument, timeframe),
                ).fetchall()
                contexts = [{"id": context, "created_at": None, "bar_count": None, "version": VERSION}]
                contexts.extend(
                    {
                        "id": context_id(instrument, timeframe, version=r[0]),
                        "created_at": r[1],
                        "bar_count": None,
                        "version": r[0],
                    }
                    for r in tracked
                    if r[0] != VERSION
                )
            if requested_context:
                # Older model archives remain accessible, but cannot cross pair/source boundaries.
                exists = conn.execute(
                    "SELECT 1 FROM market_pattern_events WHERE context_id=%s AND instrument_id=%s AND timeframe=%s AND source_mode=%s LIMIT 1",
                    (requested_context, instrument, timeframe, source),
                ).fetchone()
                if not exists:
                    return {"items": [], "total": 0, "offset": offset, "limit": limit, "contexts": contexts}
                context = requested_context
            query = """WITH latest AS (SELECT DISTINCT ON(candidate_id) candidate_id,sequence,phase,event_json,recorded_at
              FROM market_pattern_events WHERE context_id=%s ORDER BY candidate_id,sequence DESC)
              SELECT candidate_id,sequence,phase,event_json,recorded_at FROM latest
              WHERE (%s::text IS NULL OR phase=%s) AND (%s::text IS NULL OR event_json->>'pattern'=%s)"""
            args = (context, phase, phase, pattern, pattern)
            total = conn.execute("SELECT count(*) FROM (" + query + ") filtered", args).fetchone()[0]
            rows = conn.execute(
                query
                + " ORDER BY (event_json->>'observed_time')::bigint DESC,candidate_id LIMIT %s OFFSET %s",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "context_id": context,
                    "id": r[0],
                    "phase": r[2],
                    "pattern": r[3]["pattern"],
                    "name": r[3]["latest"]["name"],
                    "first_observed_time": r[3]["first_observed_time"],
                    "observed_time": r[3]["observed_time"],
                    "deadline_time": r[3]["deadline_time"],
                    "reason": r[3]["latest"]["stage"],
                    "source_mode": source,
                    "recorded_at": r[4],
                    "sequence": r[1],
                }
                for r in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
            "contexts": contexts,
        }

    def detail(self, context, candidate):
        from gatescope.adapters.postgres_pattern_studies import snapshot

        with self.pool.public_transaction() as conn:
            rows = conn.execute(
                """SELECT instrument_id,timeframe,
              gatescope_security.structure_event_record(event_json,event_blob),recorded_at FROM market_pattern_events
              WHERE context_id=%s AND candidate_id=%s ORDER BY sequence""",
                (context, candidate),
            ).fetchall()
            if not rows:
                return None
            rows = [(r[0], r[1], unpack_structure_record(r[2]), r[3]) for r in rows]
            instrument, timeframe, first, _ = rows[0]
            last = rows[-1][2]
            boundary = datetime.fromtimestamp(first["first_observed_time"], timezone.utc)
            original, metadata = snapshot(
                conn,
                instrument,
                timeframe,
                480,
                datetime.fromisoformat(first["first_snapshot_at"]),
                end_time=boundary,
                strict=False,
            )
            terminal = last["phase"] not in {"FORMING", "FORMED"}
            end = (
                datetime.fromtimestamp(last["observed_time"], timezone.utc)
                if terminal
                else datetime.now(timezone.utc)
            )
            after, _ = snapshot(
                conn,
                instrument,
                timeframe,
                640,
                datetime.fromisoformat(last["snapshot_at"]) if terminal else datetime.now(timezone.utc),
                end_time=end,
                strict=False,
            )
            bars = original + [b for b in after if b["ts"] >= first["first_observed_time"]]
        return {
            "context_id": context,
            "id": candidate,
            "timeframe": timeframe,
            "source_mode": first["source_mode"],
            "transitions": [{"record": row[2], "recorded_at": row[3]} for row in rows],
            "bars": bars,
            "original_metadata": metadata,
            "original_through": first["first_observed_time"],
            "price_basis": "ORIGINAL_REVISION_THEN_FOLLOWUP",
        }
