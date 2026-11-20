"""Bounded, strictly read-only exports of existing public structure evidence.

No detector, queue request, replay, provider client or credentials are involved.
Geometric fit, historical confirmation, and profitable returns remain distinct.
"""

from __future__ import annotations

import copy
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from uuid import UUID

from psycopg.errors import LockNotAvailable, QueryCanceled

from gatescope.adapters import annual_structure_study as annual
from gatescope.adapters.postgres_structure_archive import context_id
from gatescope.domain.current_patterns import (
    PHASES,
    confidence_score,
    filter_current_records,
    summarize_current_records,
    validate_confidence_filter,
)
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.quant.structure_detection import VERSION
from gatescope.quant.structure_lifecycle import MAX_RECORDS
from gatescope.quant.structure_archive_codec import unpack_structure_record

STATISTICAL_POLICIES = {
    "geometry_confidence": "DETECTOR_FIT_SCORE_NOT_A_PROBABILITY",
    "confirmation_denominator": "CONFIRMED_PLUS_INVALIDATED_PLUS_EXPIRED_RESOLVED_NONOVERLAPPING_CASES",
    "formation_denominator": "SAME_RESOLVED_COHORT_ENTERING_FORMING_INCLUDING_FAILURES",
    "excluded_outcomes": ["FORMING", "FORMED", "CENSORED"],
    "historical_reference": "SAME_MODEL_PATTERN_ENTRY_STAGE_SCORE_BUCKET_PRIOR_RESOLVED_CASES_ONLY",
    "source_precedence": "RECORDED_PRECEDES_REPLAY_FOR_SAME_CANDIDATE",
    "original_prediction": "IMMUTABLE_NOT_REPLACED_BY_RETROSPECTIVE_REFERENCE",
    "profit_probability": "NOT_INFERRED_FROM_GEOMETRY_OR_CONFIRMATION_RATE",
    "missing_samples": "UNKNOWN_IS_NOT_ZERO",
    "historical_replay": "RECONSTRUCTED_HISTORY_NOT_PROOF_OF_LIVE_AVAILABILITY",
}
EVENT_FIELDS = (
    "id",
    "evidence_id",
    "pattern",
    "name",
    "group",
    "phase",
    "experimental",
    "confidence",
    "direction",
    "signal_direction",
    "start_time",
    "end_time",
    "points",
    "lines",
    "recognition",
    "forecast",
    "initial_forecast",
    "forecast_outcome",
    "pre_formation_forecast",
    "structure_formed_at",
    "formation_observation",
    "levels",
    "stage",
    "note",
    "quality",
    "observed_time",
    "deadline_time",
)
RECORD_FIELDS = (
    "id",
    "pattern",
    "phase",
    "sequence",
    "reason",
    "first_index",
    "last_index",
    "first_observed_time",
    "observed_time",
    "first_snapshot_at",
    "snapshot_at",
    "source_mode",
    "deadline_time",
    "deadline_index",
    "resolved_time",
    "resolved_index",
    "stages",
    "pre_formation_forecast",
    "forecast_outcome",
)
REPORT_FIELDS = (
    "version",
    "bar_count",
    "from_time",
    "to_time",
    "window",
    "pivot_order",
    "minimum_score",
    "quality_reference_score",
    "round_trip_cost",
    "hypotheses",
    "availability_mode",
    "candidate_count",
    "lifecycle",
    "forecast_outcomes",
    "forecast_basis",
    "partial",
)
METADATA_FIELDS = (
    "snapshot_at",
    "quality_flags",
    "input_digest",
    "period_days",
    "target_bars",
    "missing_bars",
    "coverage_ratio",
    "mode",
    "range_policy",
    "listing_time",
    "cached_bars",
    "requested_from",
    "requested_to",
    "context_id",
    "step_seconds",
    "latest_recorded_at",
)


def _json(value):
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _base(instrument, timeframe, **payload):
    return _json(
        {
            "instrument_id": str(instrument),
            "timeframe": timeframe,
            "export_version": "mcp-structures-1",
            "model_version": VERSION,
            "read_at": datetime.now(timezone.utc),
            "research_only": True,
            "read_policy": "EXISTING_DATA_ONLY_NO_QUEUE_NO_REPLAY_NO_UPSTREAM",
            "statistical_policies": STATISTICAL_POLICIES,
            **payload,
        }
    )


def _bounded(method):
    @wraps(method)
    def run(self, instrument, timeframe, *args, **kwargs):
        try:
            return method(self, instrument, timeframe, *args, **kwargs)
        except (QueryCanceled, LockNotAvailable):
            return _base(
                instrument,
                timeframe,
                status="UNAVAILABLE",
                items=[],
                reason="READ_BUDGET_EXCEEDED",
                total=None,
                data_watermark=None,
            )

    return run


def _validate(instrument, timeframe, *, limit=None, offset=0, phase=None, family=None, pattern=None):
    if not isinstance(instrument, UUID) or timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("INVALID_STRUCTURE_IDENTITY")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50):
        raise ValueError("STRUCTURE_LIMIT_MUST_BE_1_TO_50")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
        raise ValueError("INVALID_STRUCTURE_OFFSET")
    if phase is not None and phase not in PHASES:
        raise ValueError("INVALID_STRUCTURE_PHASE")
    if family is not None and family not in {"chart", "wave"}:
        raise ValueError("INVALID_STRUCTURE_FAMILY")
    if pattern is not None and (not isinstance(pattern, str) or not re.fullmatch(r"[a-z_]{1,80}", pattern)):
        raise ValueError("INVALID_STRUCTURE_PATTERN")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("INVALID_STRUCTURE_EVIDENCE_ID")


def _event(value):
    return (
        copy.deepcopy({key: value[key] for key in EVENT_FIELDS if key in value})
        if isinstance(value, dict)
        else {}
    )


def _record(value):
    value = unpack_structure_record(value)
    result = copy.deepcopy({key: value[key] for key in RECORD_FIELDS if key in value})
    result.update(first=_event(value.get("first")), latest=_event(value.get("latest")))
    return result


def _initial_prediction(record):
    stages = record.get("stages", {})
    stages = stages if isinstance(stages, dict) else {}
    eligible = [s for s in stages.values() if isinstance(s, dict) and isinstance(s.get("time"), (int, float))]
    first = min(eligible, key=lambda s: s["time"], default={})
    return copy.deepcopy(first.get("prediction"))


def _history_unavailable(reason):
    return {
        "reference_state": "UNAVAILABLE",
        "reference_reason": reason,
        "sample_count": None,
        "successes": None,
        "rate": None,
        "probability": None,
        "interval": None,
        "formation": None,
        "failure_examples": [],
        "reference_is_live_prediction": False,
    }


def _projection(column, fields):
    # Field names and columns are constants, never caller-controlled SQL.
    return "jsonb_build_object(" + ",".join(f"'{key}',{column}->'{key}'" for key in fields) + ")"


def _shape_summary(event, timeframe, record):
    """Name exact persisted conditions; never invent a single-sided trigger.

    FORMED describes geometric completion. Only CONFIRMED means the detector's
    breakout/reversal confirmation conditions were met; neither means a fill.
    """
    event = event if isinstance(event, dict) else {}
    levels = event.get("levels") if isinstance(event.get("levels"), dict) else {}
    forecast = event.get("forecast") if isinstance(event.get("forecast"), dict) else {}
    conditions = forecast.get("confirmation")
    conditions = conditions if isinstance(conditions, list) else []
    notes = {}

    def positive(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    trigger = None
    if len(conditions) == 1 and isinstance(conditions[0], dict) and positive(conditions[0].get("price")):
        trigger = conditions[0]["price"]
        notes["trigger_price"] = (
            "冻结预测发布时的条件确认价；仍须先几何成形并满足确认收盘次数，不代表已触发或成交"
        )
    elif len(conditions) > 1:
        notes["trigger_price"] = "存在多个方向的确认条件，不能选择一个作为唯一触发价"
    elif "trigger" in levels or "upper" in levels or "lower" in levels:
        notes["trigger_price"] = "仅存时间相关的确认原理线；保留原始线和缓冲，不补算或猜测单一价格"
    else:
        notes["trigger_price"] = "原始证据未记录可用的单一触发价"
    invalidation = None
    if "invalid_edge" in levels:
        notes["invalidation_price"] = "失效边界是时间相关原理线，不将其中任一端点冒充固定失效价"
    elif positive(levels.get("invalid_price")):
        invalidation = levels["invalid_price"]
        notes["invalidation_price"] = "原始结构失效基准；还须结合方向、缓冲和收盘/触价规则，不等于实际止损单"
    else:
        notes["invalidation_price"] = "原始证据未记录固定失效价"
    phase = event.get("phase")
    meaning = {
        "FORMING": "形成中，结构尚未几何成形",
        "FORMED": "几何结构已形成，尚未确认突破或反转",
        "CONFIRMED": "已满足该模型的突破或反转确认规则，不代表交易已成交",
        "INVALIDATED": "结构已失效",
        "EXPIRED": "结构已到期而未完成",
        "CENSORED": "观测中断，结果未知",
    }
    return {
        "name": event.get("name") if isinstance(event.get("name"), str) else None,
        "pattern": event.get("pattern"),
        "timeframe": timeframe,
        "phase": phase,
        "phase_meaning": meaning.get(phase, "未记录有效的形态阶段"),
        "is_confirmed": phase == "CONFIRMED" if phase in PHASES else None,
        "first_seen": record.get("first_observed_time"),
        "observed_at": event.get("observed_time", record.get("observed_time")),
        "forecast_issued_at": forecast.get("issued_at"),
        "key_levels": copy.deepcopy(
            {
                "anchors": event.get("points", []),
                "lines": event.get("lines", []),
                "structure_levels": levels,
                "confirmation_conditions": conditions,
                "confirmation_closes": forecast.get("confirmation_closes", levels.get("closes")),
                "target_zone": forecast.get("target"),
                "target_kind": forecast.get("kind"),
            }
        ),
        "trigger_price": trigger,
        "invalidation_price": invalidation,
        "field_notes": notes,
    }


class McpStructureRepository:
    def __init__(self, pool):
        self.pool = pool

    @contextmanager
    def _read(self):
        with self.pool.public_transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '1500ms'")
            conn.execute("SET LOCAL lock_timeout = '500ms'")
            yield conn

    @_bounded
    def current(
        self,
        instrument,
        timeframe,
        *,
        limit=20,
        offset=0,
        phase=None,
        family=None,
        pattern=None,
        confidence_min=None,
        confidence_max=None,
        unscored_only=False,
    ):
        _validate(
            instrument, timeframe, limit=limit, offset=offset, phase=phase, family=family, pattern=pattern
        )
        validate_confidence_filter(confidence_min, confidence_max, unscored_only)
        with self._read() as conn:
            tracking = conn.execute(
                """SELECT (checkpoint_json->>'last_ts')::bigint,
                (checkpoint_json->>'bars_scanned')::bigint,updated_at,safe_error
                FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s AND version=%s""",
                (instrument, timeframe, VERSION),
            ).fetchone()
            if tracking is None:
                return _base(
                    instrument,
                    timeframe,
                    status="NOT_REQUESTED",
                    items=[],
                    total=None,
                    data_watermark=None,
                    reason="NO_EXISTING_TRACKING_CHECKPOINT",
                )
            # Read only tiny summaries for filtering; never transfer the entire
            # checkpoint's candles, calibration state and up to 16000 full records.
            rows = conn.execute(
                """SELECT e.key,jsonb_build_object('id',e.key,
                'phase',e.value->'phase','pattern',e.value->'pattern','observed_time',e.value->'observed_time',
                'latest',jsonb_build_object('name',e.value->'latest'->'name','group',e.value->'latest'->'group',
                    'confidence',e.value->'latest'->'confidence','experimental',e.value->'latest'->'experimental',
                    'signal_direction',e.value->'latest'->'signal_direction'))
                FROM market_pattern_tracking t CROSS JOIN LATERAL jsonb_each(
                    CASE WHEN jsonb_typeof(t.checkpoint_json->'records')='object'
                    THEN t.checkpoint_json->'records' ELSE '{}'::jsonb END) e
                WHERE t.instrument_id=%s AND t.timeframe=%s AND t.version=%s
                ORDER BY e.key LIMIT %s""",
                (instrument, timeframe, VERSION, MAX_RECORDS + 1),
            ).fetchall()
            source_truncated = len(rows) > MAX_RECORDS
            records = [
                value
                for _, value in rows[:MAX_RECORDS]
                if isinstance(value, dict) and value.get("phase") in PHASES
            ]
            filtered = filter_current_records(
                records,
                phase=phase,
                family=family,
                pattern=pattern,
                confidence_min=confidence_min,
                confidence_max=confidence_max,
                unscored_only=unscored_only,
            )
            filtered.sort(
                key=lambda r: (r["phase"] in {"FORMING", "FORMED"}, r.get("observed_time") or 0, r["id"]),
                reverse=True,
            )
            selected = filtered[offset : offset + limit]
            full = {}
            if selected:
                full = dict(
                    conn.execute(
                        """SELECT e.key,e.value FROM market_pattern_tracking t
                    CROSS JOIN LATERAL jsonb_each(t.checkpoint_json->'records') e
                    WHERE t.instrument_id=%s AND t.timeframe=%s AND t.version=%s AND e.key=ANY(%s)""",
                        (instrument, timeframe, VERSION, [r["id"] for r in selected]),
                    ).fetchall()
                )
            study = conn.execute(
                """SELECT requested_at,state,cursor,total,version FROM market_pattern_studies
                WHERE instrument_id=%s AND timeframe=%s AND version=%s""",
                (instrument, timeframe, VERSION),
            ).fetchone()
            history_context = context_id(instrument, timeframe, study[0], version=study[4]) if study else None
            current_context = context_id(instrument, timeframe)
            watermark = tracking[0] + TIMEFRAME_SECONDS[timeframe] if tracking[0] is not None else None
            history_deadline = time.monotonic() + 1.5
            age = max(0, int(datetime.now(timezone.utc).timestamp()) - watermark) if watermark else None
            items = []
            for summary in selected:
                record = full.get(summary["id"])
                if not isinstance(record, dict):
                    continue  # A newer checkpoint can legitimately retire a row.
                event = _event(record.get("latest"))
                history = _history_unavailable("HISTORICAL_REFERENCE_READ_BUDGET")
                if watermark is not None and time.monotonic() < history_deadline:
                    try:
                        with conn.transaction():
                            conn.execute("SET LOCAL statement_timeout = '500ms'")
                            history = annual.record_reference(
                                conn, history_context, record, watermark, recorded_context=current_context
                            )
                    except QueryCanceled:
                        pass  # Savepoint rollback keeps the outer read usable.
                items.append(
                    {
                        **event,
                        "context_id": current_context,
                        "model_version": VERSION,
                        "source_mode": "RECORDED",
                        "confidence": confidence_score(event),
                        "first_observed_time": record.get("first_observed_time"),
                        "initial_prediction": _initial_prediction(record),
                        "historical_reference": history,
                        "shape_summary": _shape_summary(event, timeframe, record),
                    }
                )
            history_missing = sum(
                item["historical_reference"].get("reference_state")
                in {"UNAVAILABLE", "HISTORY_NOT_READY", "STAGE_UNAVAILABLE"}
                for item in items
            )
            stale = age is None or age > 2 * TIMEFRAME_SECONDS[timeframe]
            flags = ["STRUCTURE_CHECKPOINT_STALE"] if stale else []
            if history_missing:
                flags.append("HISTORICAL_REFERENCE_UNAVAILABLE")
            if source_truncated:
                flags.append("CHECKPOINT_SUMMARY_TRUNCATED")
            if tracking[3]:
                flags.append("STRUCTURE_TRACKING_DEGRADED")
            status = (
                "NOT_READY"
                if watermark is None
                else "STALE"
                if stale
                else "PARTIAL"
                if flags or len(items) < len(selected)
                else "READY"
            )
            return _base(
                instrument,
                timeframe,
                status=status,
                quality_flags=flags,
                historical_references_unavailable=history_missing,
                items=items,
                total=len(filtered) if not source_truncated else None,
                matched_loaded=len(filtered),
                tracked_loaded=len(records),
                source_truncated=source_truncated,
                limit=limit,
                offset=offset,
                has_more=offset + limit < len(filtered) or source_truncated,
                next_offset=offset + limit if offset + limit < len(filtered) else None,
                count_scope="RECENT_TRACKED_CHECKPOINT"
                if not source_truncated
                else "BOUNDED_CHECKPOINT_PREFIX",
                summary=summarize_current_records(filtered),
                data_watermark=watermark,
                as_of=watermark,
                tracking_updated_at=tracking[2],
                tracking_status=tracking[3] or ("READY" if watermark else "QUEUED"),
                checkpoint_age_seconds=age,
                checkpoint_stale=stale,
                bars_scanned=tracking[1],
                history_status={"state": study[1], "cursor": study[2], "total": study[3]}
                if study
                else {"state": "NOT_REQUESTED"},
            )

    @_bounded
    def study(self, instrument, timeframe, *, pattern=None):
        _validate(instrument, timeframe, pattern=pattern)
        summary = _projection("report_json", REPORT_FIELDS)
        metadata = _projection("report_json->'metadata'", METADATA_FIELDS)
        arrays = []
        for key in ("rows", "completion_rows", "catalog"):
            field = "id" if key == "catalog" else "pattern"
            arrays.append(f"""(SELECT coalesce(jsonb_agg(value),'[]'::jsonb) FROM (
                SELECT value FROM jsonb_array_elements(CASE WHEN jsonb_typeof(report_json->'{key}')='array'
                  THEN report_json->'{key}' ELSE '[]'::jsonb END) entry(value)
                WHERE (%s::text IS NULL OR value->>'{field}'=%s) LIMIT 501) bounded)""")
        with self._read() as conn:
            row = conn.execute(
                f"""SELECT state,cursor,total,requested_at,updated_at,safe_error,version,
                report_json IS NOT NULL,{summary},{metadata},{",".join(arrays)}
                FROM market_pattern_studies WHERE instrument_id=%s AND timeframe=%s""",
                (pattern, pattern, pattern, pattern, pattern, pattern, instrument, timeframe),
            ).fetchone()
        if row is None:
            return _base(
                instrument,
                timeframe,
                status="NOT_REQUESTED",
                state="NOT_REQUESTED",
                source_mode="REPLAY",
                report=None,
            )
        report = None
        if row[7] and isinstance(row[8], dict) and row[8].get("version") == row[6]:
            report = {**row[8], "metadata": row[9]}
            for key, values in zip(("rows", "completion_rows", "catalog"), row[10:13], strict=True):
                report[key] = values[:500]
                report[key + "_truncated"] = len(values) > 500
        return _base(
            instrument,
            timeframe,
            status=row[0],
            state=row[0],
            cursor=row[1],
            total=row[2],
            requested_at=row[3],
            updated_at=row[4],
            safe_error=row[5],
            model_version=row[6],
            source_mode="REPLAY",
            report=report,
            report_status="READY" if report else "MODEL_MISMATCH" if row[7] else "NOT_READY",
            data_watermark=report.get("to_time") if report else None,
            pattern_filter=pattern,
            report_policy="STORED_SUMMARY_ONLY_NO_RECOMPUTATION_TRAINING_ARRAYS_EXCLUDED",
        )

    @_bounded
    def events(
        self,
        instrument,
        timeframe,
        *,
        source="RECORDED",
        context=None,
        phase=None,
        pattern=None,
        limit=20,
        offset=0,
    ):
        _validate(instrument, timeframe, limit=limit, offset=offset, phase=phase, pattern=pattern)
        if source not in {"RECORDED", "REPLAY"}:
            raise ValueError("INVALID_STRUCTURE_SOURCE")
        if context is not None:
            _digest(context)
        with self._read() as conn:
            if context is None and source == "REPLAY":
                study = conn.execute(
                    "SELECT requested_at,version FROM market_pattern_studies WHERE instrument_id=%s AND timeframe=%s",
                    (instrument, timeframe),
                ).fetchone()
                if study is None:
                    return _base(
                        instrument,
                        timeframe,
                        status="NOT_REQUESTED",
                        source_mode=source,
                        items=[],
                        total=None,
                    )
                context = context_id(instrument, timeframe, study[0], version=study[1])
            context = context or context_id(instrument, timeframe)
            binding = conn.execute(
                """SELECT version FROM market_pattern_events WHERE context_id=%s
                AND instrument_id=%s AND timeframe=%s AND source_mode=%s LIMIT 1""",
                (context, instrument, timeframe, source),
            ).fetchone()
            if binding is None:
                return _base(
                    instrument,
                    timeframe,
                    status="NOT_FOUND",
                    context_id=context,
                    source_mode=source,
                    items=[],
                    total=None,
                    reason="NO_MATCHING_ARCHIVE_CONTEXT",
                )
            rows = conn.execute(
                """WITH latest AS (
                SELECT DISTINCT ON(candidate_id) candidate_id,sequence,phase,observed_time,recorded_at
                FROM market_pattern_events WHERE context_id=%s AND instrument_id=%s AND timeframe=%s
                  AND source_mode=%s AND version=%s ORDER BY candidate_id,sequence DESC)
                SELECT l.candidate_id,l.sequence,l.phase,l.observed_time,l.recorded_at,
                    e.event_json->>'pattern',e.event_json->'latest'->>'name',e.event_json->>'first_observed_time'
                FROM latest l JOIN market_pattern_events e
                  ON e.context_id=%s AND e.candidate_id=l.candidate_id AND e.sequence=l.sequence
                WHERE (%s::text IS NULL OR l.phase=%s) AND (%s::text IS NULL OR e.event_json->>'pattern'=%s)
                ORDER BY l.observed_time DESC,l.candidate_id LIMIT %s OFFSET %s""",
                (
                    context,
                    instrument,
                    timeframe,
                    source,
                    binding[0],
                    context,
                    phase,
                    phase,
                    pattern,
                    pattern,
                    limit + 1,
                    offset,
                ),
            ).fetchall()
        items = [
            dict(
                zip(
                    (
                        "id",
                        "sequence",
                        "phase",
                        "observed_time",
                        "recorded_at",
                        "pattern",
                        "name",
                        "first_observed_time",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows[:limit]
        ]
        for item in items:
            item.update(context_id=context, model_version=binding[0], source_mode=source)
        return _base(
            instrument,
            timeframe,
            status="READY",
            items=items,
            model_version=binding[0],
            context_id=context,
            source_mode=source,
            total=None,
            total_policy="NOT_COUNTED_TO_BOUND_ARCHIVE_SCAN",
            offset=offset,
            limit=limit,
            has_more=len(rows) > limit,
            next_offset=offset + limit if len(rows) > limit else None,
            context_policy="ONE_BOUND_CONTEXT_NOT_ALL_MODELS_OR_REPLAY_RUNS",
            data_watermark=max((r[3] for r in rows[:limit]), default=None),
        )

    @_bounded
    def detail(
        self,
        instrument,
        timeframe,
        context,
        candidate,
        *,
        transition_limit=20,
        transition_offset=0,
        bar_limit=300,
    ):
        _validate(instrument, timeframe, limit=transition_limit, offset=transition_offset)
        _digest(context)
        _digest(candidate)
        if isinstance(bar_limit, bool) or not isinstance(bar_limit, int) or not 0 <= bar_limit <= 500:
            raise ValueError("STRUCTURE_BAR_LIMIT_MUST_BE_0_TO_500")
        where = "context_id=%s AND candidate_id=%s AND instrument_id=%s AND timeframe=%s"
        args = (context, candidate, instrument, timeframe)
        columns = "sequence,phase,from_phase,observed_time,recorded_at,version,source_mode,gatescope_security.structure_event_record(event_json,event_blob)"
        with self._read() as conn:
            first = conn.execute(
                f"SELECT {columns} FROM market_pattern_events WHERE {where} ORDER BY sequence LIMIT 1", args
            ).fetchone()
            if first is None:
                return _base(
                    instrument,
                    timeframe,
                    status="NOT_FOUND",
                    context_id=context,
                    candidate_id=candidate,
                    reason="CANDIDATE_NOT_BOUND_TO_INSTRUMENT_TIMEFRAME",
                    items=[],
                )
            last = conn.execute(
                f"SELECT {columns} FROM market_pattern_events WHERE {where} ORDER BY sequence DESC LIMIT 1",
                args,
            ).fetchone()
            if last[5:7] != first[5:7]:
                raise ValueError("STRUCTURE_ARCHIVE_IDENTITY_CONFLICT")
            rows = conn.execute(
                f"SELECT {columns} FROM market_pattern_events WHERE {where} AND version=%s AND source_mode=%s ORDER BY sequence LIMIT %s OFFSET %s",
                (*args, first[5], first[6], transition_limit + 1, transition_offset),
            ).fetchall()
            original = _record(first[7])
            latest = _record(last[7])
            chart = self._original_chart(conn, instrument, timeframe, original, bar_limit)
        transitions = [
            {
                **dict(
                    zip(
                        (
                            "sequence",
                            "phase",
                            "from_phase",
                            "observed_time",
                            "recorded_at",
                            "model_version",
                            "source_mode",
                        ),
                        row[:7],
                        strict=True,
                    )
                ),
                "record": _record(row[7]),
                "shape_summary": _shape_summary(unpack_structure_record(row[7]).get("latest"), timeframe, unpack_structure_record(row[7])),
            }
            for row in rows[:transition_limit]
        ]
        return _base(
            instrument,
            timeframe,
            status="READY",
            context_id=context,
            candidate_id=candidate,
            model_version=first[5],
            source_mode=first[6],
            first_record=original,
            latest_record=latest,
            shape_summary=_shape_summary(latest.get("latest"), timeframe, latest),
            first_shape_summary=_shape_summary(original.get("first"), timeframe, original),
            initial_prediction=_initial_prediction(original),
            transitions=transitions,
            transition_offset=transition_offset,
            transition_limit=transition_limit,
            last_sequence=last[0],
            transitions_has_more=len(rows) > transition_limit,
            next_transition_offset=transition_offset + transition_limit
            if len(rows) > transition_limit
            else None,
            transitions_total=None,
            chart=chart,
            latest_geometry=_event(latest.get("latest")),
            latest_geometry_policy="LATER_REVISION_NOT_AN_OVERLAY_FOR_THE_ORIGINAL_CHART",
            data_watermark=last[3],
            first_recorded_at=first[4],
            last_recorded_at=last[4],
            snapshot_policy="IMMUTABLE_FIRST_AND_LATEST_RECORDS_WITH_PAGED_TRANSITIONS_NO_REDETECTION",
        )

    @staticmethod
    def _original_chart(conn, instrument, timeframe, record, limit):
        event = _event(record.get("first"))
        value = {
            "view": "ORIGINAL",
            "geometry": event,
            "bars": [],
            "bar_limit": limit,
            "price_basis": "ORIGINAL_AVAILABLE_REVISION",
            "followup_included": False,
            "geometry_policy": "ORIGINAL_COORDINATES_NEVER_SNAPPED_OR_REDRAWN",
        }
        if not limit:
            return {**value, "status": "NOT_REQUESTED"}
        try:
            snapshot = datetime.fromisoformat(record["first_snapshot_at"].replace("Z", "+00:00"))
            boundary = record["first_observed_time"]
            if snapshot.tzinfo is None or isinstance(boundary, bool) or not isinstance(boundary, int):
                raise ValueError
            end = datetime.fromtimestamp(boundary, timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError):
            return {**value, "status": "UNAVAILABLE", "reason": "ORIGINAL_OBSERVATION_TIME_MISSING"}
        step = TIMEFRAME_SECONDS[timeframe]
        rows = conn.execute(
            """SELECT DISTINCT ON(open_time) open_time,close_time,open,high,low,close,volume,
            available_at,created_at,revision,payload_hash,quality_flags FROM bars
            WHERE instrument_id=%s AND timeframe=%s AND is_final AND open_time>=to_timestamp(%s)
              AND open_time<%s AND close_time<=%s AND available_at<=%s AND created_at<=%s
            ORDER BY open_time DESC,revision DESC LIMIT %s""",
            (
                instrument,
                timeframe,
                max(0, boundary - limit * step),
                end,
                min(snapshot, end),
                snapshot,
                snapshot,
                limit,
            ),
        ).fetchall()
        bars = [
            dict(
                zip(
                    (
                        "open_time",
                        "close_time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "available_at",
                        "created_at",
                        "revision",
                        "payload_hash",
                        "quality_flags",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in reversed(rows)
        ]
        for bar in bars:
            bar["ts"] = int(bar["open_time"].timestamp())
        times = {bar["ts"] for bar in bars}
        anchors = [p for p in event.get("points", []) if isinstance(p, dict)]
        guides = [
            p
            for line in event.get("lines", [])
            if isinstance(line, dict)
            for p in (
                line.get("from"),
                line.get("to"),
                line.get("curve", {}).get("vertex") if isinstance(line.get("curve"), dict) else None,
            )
            if isinstance(p, dict)
        ]
        missing = [p for p in anchors if p.get("time") not in times]

        def valid(point):
            return (
                isinstance(point.get("time"), (int, float))
                and not isinstance(point.get("time"), bool)
                and math.isfinite(point["time"])
                and isinstance(point.get("price"), (int, float))
                and not isinstance(point.get("price"), bool)
                and math.isfinite(point["price"])
                and point["price"] > 0
            )

        invalid_geometry = any(not valid(p) for p in anchors + guides)
        missing_guides = [p for p in guides if p.get("time") not in times]
        internal = [
            p
            for p in missing + missing_guides
            if valid(p) and times and min(times) <= p["time"] <= max(times)
        ]
        by_time = {bar["ts"]: bar for bar in bars}
        revised = any(
            valid(p)
            and p.get("time") in by_time
            and p.get("basis") in {"HIGH", "LOW", "CLOSE"}
            and abs(float(by_time[p["time"]][p["basis"].lower()]) - p["price"])
            > max(1e-12, abs(p["price"]) * 1e-10)
            for p in anchors
        )
        gaps = sum(right["ts"] - left["ts"] != step for left, right in zip(bars, bars[1:], strict=False))
        return {
            **value,
            "bars": bars,
            "bar_count": len(bars),
            "snapshot_at": snapshot,
            "observation_boundary": boundary,
            "missing_anchor_count": len(missing),
            "clipped_guide_endpoint_count": len(missing_guides),
            "gap_count": gaps,
            "window_truncated": bool(missing or missing_guides),
            "status": "INVALID_GEOMETRY"
            if invalid_geometry
            else "REVISED_CANDLE"
            if revised
            else "UNAVAILABLE"
            if not bars
            else "GAPPED"
            if internal or gaps
            else "CLIPPED"
            if missing or missing_guides
            else "READY",
        }
