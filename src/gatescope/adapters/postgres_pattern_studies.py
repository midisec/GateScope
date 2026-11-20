"""Bounded, restartable prefix replay in the existing strategy worker.

Inputs are public OHLCV only. Database locks prevent duplicate workers; a budgeted
loop checkpoints each slice and yields to alerts. SQL time is not a hard realtime SLO.
"""
from __future__ import annotations

import time
import copy
import math
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg import Error as DatabaseError

from gatescope.adapters.postgres import PostgresPool
from gatescope.adapters.market_history_bounds import history_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.domain.history import year_bounds
from gatescope.domain.current_patterns import (
    confidence_score, filter_current_records, summarize_current_records, validate_confidence_filter,
)
from gatescope.observability import event
from gatescope.quant.pattern_study import WINDOW, digest
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import observe, append_transition, TERMINAL
from gatescope.adapters.postgres_structure_archive import context_id, write_journal, archive_report, terminal_candidates
from gatescope.adapters import annual_structure_study as annual


def merge_forecast_training(state: dict, replay: dict | None, cutoff: int) -> None:
    """Merge completed historical next legs without overwriting real observations.

    The model still checks resolved_time < issued_at when issuing each forecast.
    Here each bar sees only samples resolved by its own closed-bar boundary; a
    replay checkpoint caught up further in wall time cannot leak future legs.
    """
    if not isinstance(replay, dict):
        return
    training = state.setdefault('forecast_training', {})
    for cell, samples in replay.items():
        if not isinstance(cell, str) or not isinstance(samples, list):
            continue
        merged = {}
        # Keep existing recorded samples on collisions, including their ratios.
        for sample in [*samples[-256:], *training.get(cell, [])]:
            if not isinstance(sample, (list, tuple)) or len(sample) != 3:
                continue
            resolved, ratio, candidate = sample
            if (isinstance(resolved, bool) or not isinstance(resolved, int)
                    or not 0 <= resolved <= cutoff or isinstance(ratio, bool)
                    or not isinstance(ratio, (int, float)) or not math.isfinite(ratio)
                    or not isinstance(candidate, str)):
                continue
            merged[(candidate, resolved)] = list(sample)
        training[cell] = sorted(merged.values(), key=lambda item: (item[0], item[2]))[-256:]


def snapshot(conn, instrument: UUID, timeframe: str, limit: int, as_of: datetime, *, end_time=None, strict=True) -> tuple[list[dict], dict]:
    rows = conn.execute("""SELECT * FROM (SELECT DISTINCT ON (open_time)
      open_time,open,high,low,close,volume,revision,payload_hash,quality_flags,available_at
      FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
        AND available_at<=%s AND close_time<=%s AND created_at<=%s
      ORDER BY open_time DESC,revision DESC LIMIT %s) recent ORDER BY open_time""",
      (instrument, timeframe, as_of, min(as_of, end_time) if end_time else as_of, as_of, limit)).fetchall()
    bars = [{"ts": int(r[0].timestamp()), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
             "close": float(r[4]), "volume": float(r[5])} for r in rows]
    step = TIMEFRAME_SECONDS[timeframe]
    if strict and any(b["ts"] - a["ts"] != step for a, b in zip(bars, bars[1:])):
        raise ValueError("PATTERN_HISTORY_GAP")
    return bars, {"snapshot_at": as_of.isoformat(), "step_seconds": step,
        "input_digest": digest([[r[0].isoformat(), r[6], r[7]] for r in rows]),
        "quality_flags": sorted(set().union(*(set(r[8]) for r in rows))),
        "latest_recorded_at": max((r[9] for r in rows), default=as_of).isoformat()}


class PatternStudyRepository:
    def __init__(self, pool: PostgresPool):
        self.pool = pool
        self._cache: OrderedDict = OrderedDict()
        self._lock = Lock()
        self._scheduled_at = 0.

    def current(self, instrument: UUID, timeframe: str, *, offset=0, limit=96, phase=None, family=None, query="",
                pattern=None, confidence_min=None, confidence_max=None, unscored_only=False) -> dict:
        validate_confidence_filter(confidence_min, confidence_max, unscored_only)
        key = (instrument, timeframe, offset, limit, phase, family, query,
            pattern, confidence_min, confidence_max, unscored_only)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                self._cache.move_to_end(key)
                return cached[1]
            with self.pool.public_transaction() as conn:
                conn.execute("SELECT gatescope_security.request_pattern_tracking(%s,%s,%s)", (instrument, timeframe, VERSION))
                row = conn.execute("SELECT checkpoint_json,safe_error FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s AND version=%s", (instrument,timeframe,VERSION)).fetchone()
                report = conn.execute("""SELECT requested_at,state,cursor,total,(checkpoint_json->>'last_ts')::bigint
                    FROM market_pattern_studies WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (instrument,timeframe,VERSION)).fetchone()
                state = row[0] if row else {}
                as_of = state.get("last_ts", 0) + TIMEFRAME_SECONDS[timeframe] if state.get("last_ts") else None
                records = list(state.get("records", {}).values())
                counts = {p:sum(r["phase"]==p for r in records) for p in ("FORMING","FORMED","CONFIRMED","INVALIDATED","EXPIRED","CENSORED")}
                tracked_total = len(records)
                records = filter_current_records(records, phase=phase, family=family, query=query,
                    pattern=pattern, confidence_min=confidence_min, confidence_max=confidence_max,
                    unscored_only=unscored_only)
                total = len(records)
                summary = summarize_current_records(records)
                records = sorted(records, key=lambda r: (r["phase"] not in TERMINAL, r["observed_time"],r["id"]), reverse=True)[offset:offset+limit]
                history_context=context_id(instrument,timeframe,report[0]) if report else None
                events = [{**r["latest"], "confidence": confidence_score(r["latest"]),
                    "context_id": context_id(instrument,timeframe), "first_observed_time": r["first_observed_time"],
                    "initial_prediction": min(r.get("stages",{}).values(),key=lambda s:s.get("time",0),default={"prediction": None}).get("prediction"),
                    "historical_reference": annual.record_reference(conn,history_context,r,as_of or 0,
                        recorded_context=context_id(instrument,timeframe))} for r in records]
            result = {"items": events, "catalog": annual.reference_catalog(), "version": VERSION, "bars_scanned": state.get("bars_scanned", 0),
                "history_status": {"state": report[1] if report else "NOT_REQUESTED",
                    "cursor": int(report[2] or 0) if report else 0,
                    "total": int(report[3] or 0) if report else 0,
                    "through_time": report[4] + TIMEFRAME_SECONDS[timeframe] if report and report[4] is not None else None,
                    "version": VERSION},
                "as_of": as_of, "metadata": state.get("metadata", {}), "window": WINDOW, "display_limit": limit,
                "total":total,"tracked_total":tracked_total,"phase_counts":counts,"summary":summary,"offset":offset,"limit":limit,
                "has_more":offset+len(events)<total,"count_scope":"RECENT_TRACKED","diagnostics":state.get("detection_diagnostics",{}),
                "tracking_status": row[1] or ("READY" if as_of else "QUEUED") if row else "QUEUED"}
            self._cache[key] = (time.monotonic(), result)
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
            return result

    def request(self, instrument: UUID, timeframe: str) -> dict:
        with self.pool.public_transaction() as conn:
            conn.execute("SELECT gatescope_security.request_pattern_study(%s,%s,%s)", (instrument, timeframe, VERSION))
        return self.get(instrument, timeframe)

    def get(self, instrument: UUID, timeframe: str) -> dict:
        with self.pool.public_transaction() as conn:
            row = conn.execute("""SELECT state,cursor,total,requested_at,updated_at,safe_error,report_json,version
              FROM market_pattern_studies WHERE instrument_id=%s AND timeframe=%s""", (instrument, timeframe)).fetchone()
        if not row:
            return {"state": "NOT_REQUESTED", "cursor": 0, "total": 0, "report": None}
        result = dict(zip(("state", "cursor", "total", "requested_at", "updated_at", "error", "report", "version"), row, strict=True))
        if result["report"] and result["report"].get("version") != result["version"]:
            result["previous_report_version"] = result["report"]["version"]
            result["report"] = None  # New-model progress must not display old-model statistics.
        if result["report"]:
            result["report"].pop("training_samples",None)  # Internal calibration ledger, not a repeated UI payload.
            if result["report"].get("version") == VERSION:
                capabilities = {item["id"]: item["supports_forming"] for item in annual.reference_catalog()}
                result["report"]["catalog"] = [{**item, "supports_forming": capabilities[item["id"]]}
                    if item["id"] in capabilities else item for item in result["report"].get("catalog", [])]
        return result

    def watchlist_progress(self,tenant,user,offset=0,limit=40):
        with self.pool.transaction(tenant,user) as conn:
            query="""WITH selected AS (SELECT DISTINCT wi.instrument_id FROM watchlist_items wi JOIN watchlists w ON w.id=wi.watchlist_id
                WHERE w.deleted_at IS NULL AND w.tenant_id=%s AND w.owner_user_id=%s)
                SELECT i.id,i.symbol,tf.period,s.cached_bars,s.target_bars,s.missing_bars,s.history_status,s.history_error_code,
                p.state,p.cursor,p.total,p.safe_error,s.target_from,s.target_to,p.report_json->>'to_time',
                s.live_error_code,s.last_live_at,s.last_history_at,p.input_json->'metadata'->'automatic_resume_attempts',count(*) OVER()
                FROM selected JOIN instruments i ON i.id=selected.instrument_id
                CROSS JOIN unnest(ARRAY['1m','5m','15m','30m','1h','4h','8h','1d']) WITH ORDINALITY tf(period,position)
                LEFT JOIN market_sync_state s ON s.instrument_id=i.id AND s.timeframe=tf.period
                LEFT JOIN market_pattern_studies p ON p.instrument_id=i.id AND p.timeframe=tf.period AND p.version=%s
                ORDER BY i.symbol,i.id,tf.position LIMIT %s OFFSET %s"""
            rows=conn.execute(query,(tenant,user,VERSION,limit,offset)).fetchall()
            now=datetime.now(timezone.utc)
            names=("instrument_id","symbol","timeframe","cached_bars","target_bars","missing_bars","history_status","history_error_code",
                "state","cursor","total","error","target_from","target_to","report_to","live_error_code","last_live_at","coverage_asof","automatic_resume_attempts")
            items=[]
            for row in rows:
                item=dict(zip(names,row[:-1],strict=True))
                bounds=history_bounds(conn,item["instrument_id"],item["timeframe"],now)
                step=TIMEFRAME_SECONDS[item["timeframe"]]
                if (item["coverage_asof"] is not None and item["target_from"] is not None
                        and item["target_to"] is not None and item["target_bars"]):
                    # Numerator, denominator and range belong to ONE durable
                    # background snapshot. Advancing a minute must not run a
                    # half-million-row COUNT on every user's progress poll.
                    start=int(item["target_from"].timestamp())
                    end=int(item["target_to"].timestamp())-step
                    requested_start=year_bounds(item["target_to"],item["timeframe"])[0]
                    policy="LISTING_CLIPPED" if start>requested_start else "ROLLING_YEAR"
                    expected_start=(max(requested_start,bounds["start"])
                        if bounds["range_policy"]=="LISTING_CLIPPED" else requested_start)
                    # Newly learned listing metadata does not justify mixing
                    # old cached counts with a new smaller denominator. Leave
                    # recalculation to the market worker, not the web request.
                    pending=start!=expected_start
                else:
                    # No initialized history snapshot: availability is unknown,
                    # not a request to synchronously scan all cached candles.
                    start,end=bounds["start"],bounds["end"]
                    requested_start=bounds["requested_start"]
                    policy=bounds["range_policy"]
                    pending=True
                    item.update(target_from=datetime.fromtimestamp(start,timezone.utc),
                        target_to=datetime.fromtimestamp(end+step,timezone.utc),
                        target_bars=bounds["target_bars"],cached_bars=0,coverage_asof=None)
                item.update(instrument_id=str(item["instrument_id"]),listing_time=bounds["listing_time"],
                    range_policy=policy,range_start=start,range_end=end,requested_start=requested_start,
                    replay_error=item["error"],range_refresh_pending=pending)
                item["cached_bars"]=item["cached_bars"] or 0
                item["missing_bars"]=max(0,item["target_bars"]-item["cached_bars"])
                item["coverage_ratio"]=min(1.,item["cached_bars"]/item["target_bars"]) if item["target_bars"] else 0.
                item["state"]=item["state"] or "WAITING_HISTORY"
                attempts=item.pop("automatic_resume_attempts")
                item["replay_retry_exhausted"]=(item["state"]=="FAILED" and isinstance(attempts,int)
                    and not isinstance(attempts,bool) and attempts>=3)
                item["last_live_at"]=item["last_live_at"].isoformat() if item["last_live_at"] else None
                item["coverage_asof"]=item["coverage_asof"].isoformat() if item["coverage_asof"] else None
                items.append(item)
        return {"items":items,"total":rows[0][-1] if rows else 0,"offset":offset,"limit":limit,"period_days":365}

    def run_slice(self, *, seconds: float = 2, max_steps: int = 64) -> bool:
        with self.pool.public_transaction() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(7342282)").fetchone()[0]:
                return False
            row = conn.execute("""SELECT instrument_id,timeframe,requested_at,cursor,input_json,checkpoint_json,version
              FROM market_pattern_studies WHERE state IN ('QUEUED','RUNNING')
              ORDER BY updated_at,requested_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if not row:
                return False
            instrument, timeframe, requested, cursor, saved, checkpoint, version = row
            try:
                # A detector/report failure must not commit half a slice's
                # journal while discarding the checkpoint that owns those facts.
                with conn.transaction():
                    if version != VERSION:
                        raise ValueError("PATTERN_VERSION_CHANGED")
                    if saved is None:
                        saved,total=annual.initialize(conn,instrument,timeframe,requested,context_id(instrument,timeframe,requested))
                        saved["metadata"]["step_seconds"]=TIMEFRAME_SECONDS[timeframe]
                        checkpoint,cursor={"step_seconds":TIMEFRAME_SECONDS[timeframe]},0
                        conn.execute("UPDATE market_pattern_studies SET input_json=%s,total=%s WHERE instrument_id=%s AND timeframe=%s",
                            (Jsonb(saved),total,instrument,timeframe))
                    if not saved.get("stream"):raise ValueError("PATTERN_VERSION_CHANGED")
                    bars=saved["bars"];metadata=saved["metadata"];context=metadata["context_id"]
                    chunk=annual.page(conn,instrument,timeframe,datetime.fromisoformat(metadata["snapshot_at"]),
                        saved["start"],saved["end"],saved["after"],max_steps)
                    started, steps = time.monotonic(), 0
                    for bar in chunk:
                        if steps and time.monotonic()-started>=seconds:break
                        metadata["input_digest"]=digest([metadata["input_digest"],bar["ts"],bar["revision"],bar["payload_hash"]])
                        metadata["quality_flags"]=sorted(set(metadata["quality_flags"])|set(bar["quality_flags"]))
                        bars.append({k:bar[k] for k in ("ts","open","high","low","close","volume")})
                        bars[:]=bars[-WINDOW:]
                        saved.setdefault("first_time",bar["ts"])
                        if cursor>=19:
                            journal=observe(bars,len(bars)-1,checkpoint,snapshot_at=metadata["snapshot_at"],index_offset=cursor-len(bars)+1,
                                prediction_provider=(lambda e,t:annual.reference(conn,context,e,t)) if metadata["mode"]=="INCREMENTAL" else None)
                            write_journal(conn,instrument,timeframe,context,journal)
                            annual.project_journal(conn,context,journal)
                            annual.project_returns(conn,context,annual.advance_returns(checkpoint,bar,cursor))
                            annual.compact(checkpoint)
                        saved["after"]=bar["ts"]
                        cursor += 1
                        steps += 1
                    done=cursor>=saved["count"] or not chunk
                    previous=checkpoint.get("reported_cursor",0)
                    if done or (not previous and cursor>=500) or cursor-previous>=5000:
                        from gatescope.quant.stage_environment import stage_environment_key
                        report=annual.report(conn,context,checkpoint,metadata,min(cursor,metadata["cached_bars"]),saved["first_time"],
                            environment_key=stage_environment_key(bars,checkpoint["step_seconds"]))
                        report["partial"]=not done
                        if done:archive_report(conn,instrument,timeframe,context,report)
                        checkpoint["reported_cursor"]=cursor
                        conn.execute("UPDATE market_pattern_studies SET report_json=%s WHERE instrument_id=%s AND timeframe=%s",(Jsonb(report),instrument,timeframe))
                    conn.execute("""UPDATE market_pattern_studies SET state=%s,cursor=%s,updated_at=now(),
                        input_json=%s,checkpoint_json=%s WHERE instrument_id=%s AND timeframe=%s""",
                        ("READY" if done else "RUNNING",cursor,Jsonb(saved),Jsonb(checkpoint),instrument,timeframe))
            except Exception as exc:
                if isinstance(exc, DatabaseError):
                    raise  # Roll back the whole slice; a DB outage must not lose progress.
                safe = str(exc) if str(exc) in {"PATTERN_HISTORY_GAP", "PATTERN_HISTORY_SHORT", "PATTERN_STUDY_TOO_DENSE", "PATTERN_VERSION_CHANGED"} else "PATTERN_CALCULATION_FAILED"
                event("pattern_study.failed", level=40, error_type=type(exc).__name__, safe_error=safe)
                conn.execute("""UPDATE market_pattern_studies SET state='FAILED',safe_error=%s,updated_at=now()
                  WHERE instrument_id=%s AND timeframe=%s""", (safe, instrument, timeframe))
            return True

    def schedule(self):
        """Automatically cover watched pairs; the eight-slot queue is shared with manual research."""
        if time.monotonic()-self._scheduled_at<30:return
        self._scheduled_at=time.monotonic()
        with self.pool.public_transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7342281)")
            remaining=8-conn.execute("SELECT count(*) FROM market_pattern_studies WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]
            if remaining<=0:return
            pairs=conn.execute("""SELECT s.instrument_id,s.timeframe,p.version,p.state,p.input_json,p.cursor,p.requested_at,p.safe_error
                FROM market_sync_state s LEFT JOIN market_pattern_studies p USING(instrument_id,timeframe)
                WHERE s.active AND s.cached_bars>=60
                AND (p.instrument_id IS NULL OR p.version<>%s OR p.state IN ('READY','FAILED'))
                AND (p.updated_at IS NULL OR p.updated_at<now()-interval '15 minutes' OR p.version<>%s)
                AND (p.state IS DISTINCT FROM 'FAILED' OR p.version IS DISTINCT FROM %s
                  OR p.safe_error IN ('PATTERN_VERSION_CHANGED','PATTERN_HISTORY_SHORT')
                  OR p.input_json->>'stream' IS DISTINCT FROM 'true'
                  OR COALESCE(p.input_json->'metadata'->'automatic_resume_attempts','0'::jsonb)<'3'::jsonb)
                ORDER BY CASE WHEN p.instrument_id IS NULL OR p.version<>%s THEN 0 ELSE 1 END,
                CASE s.timeframe WHEN '1d' THEN 0 WHEN '8h' THEN 1 WHEN '4h' THEN 2 WHEN '1h' THEN 3
                WHEN '30m' THEN 4 WHEN '15m' THEN 5 WHEN '5m' THEN 6 ELSE 7 END,s.instrument_id LIMIT %s""",
                (VERSION,VERSION,VERSION,VERSION,remaining)).fetchall()
            for instrument,timeframe,version,status,saved,cursor,_requested,safe_error in pairs:
                now=datetime.now(timezone.utc)
                if (version==VERSION and status=='FAILED' and saved and saved.get('stream')
                        and safe_error not in ('PATTERN_VERSION_CHANGED','PATTERN_HISTORY_SHORT')):
                    metadata=saved.setdefault('metadata',{})
                    attempts=metadata.get('automatic_resume_attempts',0)
                    if isinstance(attempts,bool) or not isinstance(attempts,int) or not 0<=attempts<3:
                        continue
                    metadata['automatic_resume_attempts']=attempts+1
                    conn.execute("""UPDATE market_pattern_studies SET state='QUEUED',input_json=%s,
                        updated_at=now(),safe_error=NULL WHERE instrument_id=%s AND timeframe=%s""",
                        (Jsonb(saved),instrument,timeframe))
                    continue
                if version==VERSION and status=="READY" and saved and saved.get("stream"):
                    old_snapshot=datetime.fromisoformat(saved["metadata"]["snapshot_at"])
                    bounds=history_bounds(conn,instrument,timeframe,now)
                    start,end,target=bounds["start"],bounds["end"],bounds["target_bars"]
                    step=TIMEFRAME_SECONDS[timeframe]
                    old_metadata=saved["metadata"]
                    range_changed=(old_metadata.get("listing_time")!=bounds["listing_time"]
                        or old_metadata.get("range_policy","ROLLING_YEAR")!=bounds["range_policy"]
                        or start<saved["start"])
                    revised=conn.execute("""SELECT 1 FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                        AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
                        AND (created_at>%s OR available_at>%s OR close_time>%s)
                        AND available_at<=%s AND created_at<=%s AND close_time<=%s
                        AND mod(extract(epoch FROM open_time),%s)=0 LIMIT 1""",
                        (instrument,timeframe,start,saved["after"],old_snapshot,old_snapshot,old_snapshot,now,now,now,step)).fetchone()
                    if not revised and not range_changed:
                        added=conn.execute("""SELECT count(DISTINCT open_time) FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                            AND open_time>to_timestamp(%s) AND open_time<=to_timestamp(%s) AND available_at<=%s AND created_at<=%s
                            AND close_time<=%s AND mod(extract(epoch FROM open_time),%s)=0""",
                            (instrument,timeframe,max(saved["after"],start-1),end,now,now,now,step)).fetchone()[0]
                        if not added:
                            # A quiet daily chart must not occupy the first eight scheduling slots forever.
                            conn.execute("UPDATE market_pattern_studies SET updated_at=now() WHERE instrument_id=%s AND timeframe=%s",(instrument,timeframe))
                            continue
                        covered=conn.execute("""SELECT count(DISTINCT open_time) FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final
                            AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s) AND available_at<=%s AND created_at<=%s
                            AND close_time<=%s AND mod(extract(epoch FROM open_time),%s)=0""",
                            (instrument,timeframe,start,end,now,now,now,step)).fetchone()[0]
                        saved["count"]=cursor+added;saved["end"]=end
                        saved["metadata"].update(snapshot_at=now.isoformat(),mode="INCREMENTAL",requested_from=start,
                            requested_to=end,cached_bars=covered,missing_bars=max(0,target-covered),
                            target_bars=target,coverage_ratio=covered/target if target else 0.,
                            listing_time=bounds["listing_time"],range_policy=bounds["range_policy"],
                            requested_start=bounds["requested_start"],range_start=start,range_end=end)
                        conn.execute("""UPDATE market_pattern_studies SET state='QUEUED',input_json=%s,total=%s,updated_at=now(),safe_error=NULL
                            WHERE instrument_id=%s AND timeframe=%s""",(Jsonb(saved),saved["count"],instrument,timeframe))
                        continue
                conn.execute("""INSERT INTO market_pattern_studies(instrument_id,timeframe,version,state) VALUES(%s,%s,%s,'QUEUED')
                    ON CONFLICT(instrument_id,timeframe) DO UPDATE SET version=EXCLUDED.version,state='QUEUED',requested_at=now(),
                    updated_at=now(),cursor=0,total=0,input_json=NULL,checkpoint_json=NULL,safe_error=NULL""",(instrument,timeframe,VERSION))

    def track_slice(self, *, seconds: float = 1, max_steps: int = 16) -> bool:
        """Follow closed bars without requiring the web page to remain open."""
        with self.pool.public_transaction() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(7342284)").fetchone()[0]:
                return False
            conn.execute("""INSERT INTO market_pattern_tracking(instrument_id,timeframe,version)
              SELECT instrument_id,timeframe,%s FROM market_sync_state WHERE active=true
              ON CONFLICT DO NOTHING""", (VERSION,))
            row = conn.execute("""SELECT t.instrument_id,t.timeframe,t.checkpoint_json,t.version FROM market_pattern_tracking t
              WHERE (t.version=%s OR jsonb_array_length(COALESCE(t.checkpoint_json->'active','[]'))>0) AND t.updated_at<now()-interval '15 seconds' AND
              (t.requested_at>now()-interval '24 hours' OR jsonb_array_length(COALESCE(t.checkpoint_json->'active','[]'))>0
               OR EXISTS(SELECT 1 FROM market_sync_state s WHERE s.instrument_id=t.instrument_id AND s.timeframe=t.timeframe AND s.active))
              ORDER BY t.updated_at FOR UPDATE OF t SKIP LOCKED LIMIT 1""", (VERSION,)).fetchone()
            if not row:
                return False
            instrument,timeframe,state,tracked_version = row
            now = datetime.now(timezone.utc)
            if tracked_version != VERSION:
                journal: list[dict]=[]
                for identifier in state.get("active",[]):
                    append_transition(state["records"][identifier],"CENSORED",state["last_index"],state["last_ts"]+state["step_seconds"],"MODEL_CHANGED",journal,now.isoformat())
                write_journal(conn,instrument,timeframe,context_id(instrument,timeframe,version=tracked_version),journal,"RECORDED",version=tracked_version)
                state["active"]=[]
                conn.execute("UPDATE market_pattern_tracking SET checkpoint_json=%s,updated_at=now() WHERE instrument_id=%s AND timeframe=%s AND version=%s",(Jsonb(state),instrument,timeframe,tracked_version))
                return True
            last = state.get("last_ts")
            if last is not None:
                next_bar = conn.execute("""SELECT 1 FROM bars WHERE instrument_id=%s AND timeframe=%s
                    AND open_time>to_timestamp(%s) AND is_final=true
                    AND available_at<=%s AND close_time<=%s AND created_at<=%s
                    ORDER BY open_time DESC LIMIT 1""", (instrument,timeframe,last,now,now,now)).fetchone()
                if next_bar is None:
                    # A scheduler touch must not replace the original snapshot
                    # clock, rewrite its TOAST payload or erase a prior error.
                    conn.execute("""UPDATE market_pattern_tracking SET updated_at=now()
                        WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (instrument,timeframe,VERSION))
                    return True
            bars, metadata = snapshot(conn,instrument,timeframe,WINDOW+160,now,strict=False)
            pending = ([i for i,b in enumerate(bars) if b["ts"]>last] if last is not None
                       else ([len(bars)-1] if len(bars)>=20 else []))
            if not pending:
                conn.execute("""UPDATE market_pattern_tracking SET updated_at=now()
                    WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (instrument,timeframe,VERSION))
                return True
            # Bootstrap new live forecasts from the same model's causal replay prefix.
            historical=conn.execute("""SELECT checkpoint_json->'training',checkpoint_json->'forecast_training',
                (checkpoint_json->>'last_ts')::bigint,requested_at FROM market_pattern_studies
                WHERE instrument_id=%s AND timeframe=%s AND version=%s""",(instrument,timeframe,VERSION)).fetchone()
            started, error, processed = time.monotonic(), None, 0
            for i in pending[:max_steps]:
                if time.monotonic()-started>=seconds:
                    break
                offset = state.get("last_index",i-1)+1-i
                before = copy.deepcopy(state)
                try:
                    state.setdefault("step_seconds",TIMEFRAME_SECONDS[timeframe])
                    if (historical and historical[2] is not None
                            and historical[2]<bars[pending[0]]["ts"] and not state.get("history_seeded")):
                        # Seed only as part of an attempted new close. Failed or
                        # budget-skipped bars must not persist replay learning.
                        for key,value in (historical[0] or {}).items():state.setdefault("training",{}).setdefault(key,value)
                        state["history_seeded"]=True
                    # Replay can finish after a live pair was first initialized.
                    # Merge its completed legs before EACH new close, not only
                    # on the once-only classification-count bootstrap above.
                    if historical:
                        merge_forecast_training(state,historical[1],bars[i]['ts']+TIMEFRAME_SECONDS[timeframe])
                    journal = observe(bars,i,state,snapshot_at=now.isoformat(),source_mode="RECORDED",index_offset=offset,
                        prediction_provider=lambda e,t:annual.reference(conn,context_id(instrument,timeframe,historical[3]) if historical else None,e,t,
                            recorded_context=context_id(instrument,timeframe)),
                        terminal_guard=lambda ids:terminal_candidates(conn,context_id(instrument,timeframe),ids))
                except ValueError as exc:
                    state = before
                    error = str(exc) if str(exc).startswith("PATTERN_") else "PATTERN_CALCULATION_FAILED"
                    break
                write_journal(conn,instrument,timeframe,context_id(instrument,timeframe),journal,"RECORDED")
                annual.project_journal(conn,context_id(instrument,timeframe),journal)
                # Only hot projections are pruned; immutable journal keeps EVERY outcome.
                cutoff = state["last_index"]-WINDOW
                state["records"] = {k:r for k,r in state["records"].items() if r["phase"] not in TERMINAL or r["last_index"]>=cutoff}
                state["facts"] = []
                processed += 1
            if processed:
                state["bars_scanned"],state["metadata"] = min(len(bars),WINDOW),metadata
                conn.execute("""UPDATE market_pattern_tracking SET checkpoint_json=%s,updated_at=now(),safe_error=%s
                  WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (Jsonb(state),error,instrument,timeframe,VERSION))
            elif error is not None:
                conn.execute("""UPDATE market_pattern_tracking SET updated_at=now(),safe_error=%s
                    WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (error,instrument,timeframe,VERSION))
            else:
                conn.execute("""UPDATE market_pattern_tracking SET updated_at=now()
                    WHERE instrument_id=%s AND timeframe=%s AND version=%s""", (instrument,timeframe,VERSION))
            return True
