"""Streaming replay primitives: bounded OHLC window, durable SQL projections.

The immutable event journal remains the evidence. These replace replay-wide
Python arrays with indexed, rebuildable public-data stage/return projections.
"""

import math
from statistics import NormalDist
from collections.abc import Mapping

from psycopg.types.json import Jsonb

from gatescope.adapters.market_history_bounds import history_bounds
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.quant.pattern_study import WINDOW, PIVOT_ORDER, MINIMUM, HORIZONS, ROUND_TRIP_COST, catalog
from gatescope.quant.structure_detection import VERSION, bucket
from gatescope.quant.structure_lifecycle import TERMINAL, PHASE_LABELS, probability
from gatescope.quant.structure_archive_codec import forecast_comparison
from gatescope.quant.probability_reliability import reliability_comparison


def reference_catalog():
    """Display-only stage capabilities; never add fields to detector metadata."""
    forming = {
        "double_top",
        "double_bottom",
        "elliott_impulse_bull",
        "elliott_impulse_bear",
        "elliott_correction_bull",
        "elliott_correction_bear",
        "elliott_cycle_bull",
        "elliott_cycle_bear",
    }
    return [{**item, "supports_forming": item["id"] in forming} for item in catalog()]


def page(conn, instrument, timeframe, as_of, start, end, after, limit=96):
    """Read a bounded time window without sorting the remaining replay year.

    Finding only the next eligible open time can walk the existing descending
    open/revision index backwards; it does not require opposite revision order.
    The highest-revision sort then covers at most ``limit`` candle intervals.
    Gaps may produce a short page, never skip ahead past an unreturned candle.
    Both phases share one PostgreSQL statement snapshot and the same as-of rules.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("PATTERN_PAGE_LIMIT")
    if limit == 0:
        return []
    step = TIMEFRAME_SECONDS[timeframe]
    seconds = limit * step
    rows = conn.execute(
        """WITH next_bar AS MATERIALIZED (
        SELECT open_time FROM bars
        WHERE instrument_id=%s AND timeframe=%s AND is_final=true
          AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s) AND open_time>to_timestamp(%s)
          AND available_at<=%s AND created_at<=%s AND close_time<=%s
          AND mod(extract(epoch FROM open_time),%s)=0
        ORDER BY open_time ASC LIMIT 1
      )
      SELECT recent.* FROM next_bar n CROSS JOIN LATERAL (
        SELECT DISTINCT ON(open_time) open_time,open,high,low,close,volume,revision,payload_hash,quality_flags
        FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
          AND open_time>=n.open_time AND open_time<n.open_time+make_interval(secs=>%s)
          AND open_time>=to_timestamp(%s) AND open_time>to_timestamp(%s)
          AND open_time<=to_timestamp(%s)
          AND available_at<=%s AND created_at<=%s AND close_time<=%s
          AND mod(extract(epoch FROM open_time),%s)=0
        ORDER BY open_time ASC,revision DESC LIMIT %s
      ) recent ORDER BY recent.open_time ASC""",
        (
            instrument,
            timeframe,
            start,
            end,
            after,
            as_of,
            as_of,
            as_of,
            step,
            instrument,
            timeframe,
            seconds,
            start,
            after,
            end,
            as_of,
            as_of,
            as_of,
            step,
            limit,
        ),
    ).fetchall()
    return [
        {
            "ts": int(r[0].timestamp()),
            **dict(zip(("open", "high", "low", "close", "volume"), map(float, r[1:6]), strict=True)),
            "revision": r[6],
            "payload_hash": r[7],
            "quality_flags": r[8],
        }
        for r in rows
    ]


def initialize(conn, instrument, timeframe, requested, context):
    bounds = history_bounds(conn, instrument, timeframe, requested)
    start, end, target = bounds["start"], bounds["end"], bounds["target_bars"]
    count = conn.execute(
        """SELECT count(DISTINCT open_time) FROM bars WHERE instrument_id=%s AND timeframe=%s
        AND is_final=true AND open_time>=to_timestamp(%s) AND open_time<=to_timestamp(%s)
        AND available_at<=%s AND created_at<=%s AND close_time<=%s
        AND mod(extract(epoch FROM open_time),%s)=0""",
        (instrument, timeframe, start, end, requested, requested, requested, TIMEFRAME_SECONDS[timeframe]),
    ).fetchone()[0]
    if count < 60:
        raise ValueError("PATTERN_HISTORY_SHORT")
    return {
        "stream": True,
        "bars": [],
        "start": start,
        "end": end,
        "after": start - 1,
        "count": count,
        "metadata": {
            "snapshot_at": requested.isoformat(),
            "context_id": context,
            "period_days": 365,
            "target_bars": target,
            "requested_from": start,
            "requested_to": end,
            "cached_bars": count,
            "missing_bars": target - count,
            "coverage_ratio": count / target if target else 0.0,
            "quality_flags": [],
            "input_digest": "",
            "mode": "FULL",
            "listing_time": bounds["listing_time"],
            "range_policy": bounds["range_policy"],
            "requested_start": bounds["requested_start"],
            "range_start": start,
            "range_end": end,
        },
    }, count


def project_journal(conn, context, journal):
    for transition in journal:
        r = transition["record"]
        for phase, stage in r["stages"].items():
            conn.execute(
                """INSERT INTO market_structure_stages(context_id,candidate_id,pattern,entry_phase,score_bucket,
                stage_time,phase,resolved_time,sampled,formed,prediction,forecast_json,outcome_json,environment_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(context_id,candidate_id,entry_phase) DO UPDATE SET phase=EXCLUDED.phase,
                resolved_time=EXCLUDED.resolved_time,formed=EXCLUDED.formed,outcome_json=EXCLUDED.outcome_json
                WHERE (market_structure_stages.phase,market_structure_stages.resolved_time,
                       market_structure_stages.formed,market_structure_stages.outcome_json)
                  IS DISTINCT FROM (EXCLUDED.phase,EXCLUDED.resolved_time,EXCLUDED.formed,EXCLUDED.outcome_json)""",
                (
                    context,
                    r["id"],
                    r["pattern"],
                    phase,
                    stage["score_bucket"],
                    stage["time"],
                    r["phase"],
                    r.get("resolved_time"),
                    stage["sampled"],
                    "FORMED" in r["stages"],
                    stage["prediction"]["probability"],
                    Jsonb(forecast_comparison(r["first"].get("forecast"), transition["sequence"])),
                    Jsonb(r.get("forecast_outcome")),
                    stage.get("environment_key"),
                ),
            )


def advance_returns(state, bar, index):
    """Fixed-horizon samples selected at entry, not after seeing a profitable exit."""
    step = state["step_seconds"]
    updated = []
    pending = []
    for r in state.pop("pending_returns", []):
        if bar["ts"] != r["next_time"]:
            r["status"] = "CENSORED"
        else:
            if r.get("entry_time") is None:
                r.update(entry_time=bar["ts"], entry_price=bar["open"], high=bar["high"], low=bar["low"])
            r["high"] = max(r["high"], bar["high"])
            r["low"] = min(r["low"], bar["low"])
            r["next_time"] += step
            if index >= r["exit_index"]:
                entry = r["entry_price"]
                side = r["side"]
                r.update(
                    status="COMPLETE",
                    exit_time=bar["ts"] + step,
                    net_return=side * (bar["close"] - entry) / entry - ROUND_TRIP_COST,
                    mfe=max(0.0, (r["high"] - entry if side > 0 else entry - r["low"]) / entry),
                    mae=min(0.0, (r["low"] - entry if side > 0 else entry - r["high"]) / entry),
                )
        if r["status"] == "PENDING":
            pending.append(r)
        else:
            updated.append(r)
    for pattern, phase, i, side, evidence in state.pop("facts", []):
        for horizon in HORIZONS:
            key = f"{pattern}:{phase}:{horizon}"
            previous = state.setdefault("return_sampling", {}).get(key, -1)
            status = "NEUTRAL" if not side else "OVERLAP" if i + 1 <= previous else "PENDING"
            r = {
                "pattern": pattern,
                "phase": phase,
                "horizon": horizon,
                "observed_time": bar["ts"] + step,
                "status": status,
                "side": side,
                "evidence_id": evidence,
                "exit_index": i + horizon,
                "next_time": bar["ts"] + step,
            }
            updated.append(r.copy())
            if status == "PENDING":
                state["return_sampling"][key] = i + horizon
                pending.append(r)
    state["pending_returns"] = pending
    return updated


def project_returns(conn, context, rows):
    if not rows:
        return
    with conn.cursor() as cursor:
        cursor.executemany(
            """INSERT INTO market_structure_returns(context_id,evidence_id,pattern,phase,horizon,observed_time,
            status,entry_time,exit_time,side,net_return,mfe,mae) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(context_id,evidence_id,horizon) DO UPDATE SET status=EXCLUDED.status,entry_time=EXCLUDED.entry_time,
            exit_time=EXCLUDED.exit_time,net_return=EXCLUDED.net_return,mfe=EXCLUDED.mfe,mae=EXCLUDED.mae
            WHERE (market_structure_returns.status,market_structure_returns.entry_time,
                   market_structure_returns.exit_time,market_structure_returns.net_return,
                   market_structure_returns.mfe,market_structure_returns.mae)
              IS DISTINCT FROM (EXCLUDED.status,EXCLUDED.entry_time,EXCLUDED.exit_time,
                                EXCLUDED.net_return,EXCLUDED.mfe,EXCLUDED.mae)""",
            [
                (
                    context,
                    r["evidence_id"],
                    r["pattern"],
                    r["phase"],
                    r["horizon"],
                    r["observed_time"],
                    r["status"],
                    r.get("entry_time"),
                    r.get("exit_time"),
                    r["side"],
                    r.get("net_return"),
                    r.get("mfe"),
                    r.get("mae"),
                )
                for r in rows
            ],
        )


def reference(conn, context, event, as_of, *, recorded_context=None):
    phase = event["phase"]
    if recorded_context and phase in {"FORMING", "FORMED"}:
        return observed_reference(conn, context, recorded_context, event, as_of)
    if not context or phase not in {"FORMING", "FORMED"}:
        return {**probability(0, 0), "basis": "NO_MATCHING_HISTORY"}
    n, w, f = conn.execute(
        """SELECT count(*),count(*) FILTER(WHERE phase='CONFIRMED'),count(*) FILTER(WHERE formed) FROM market_structure_stages
        WHERE context_id=%s AND pattern=%s AND entry_phase=%s AND score_bucket=%s AND sampled
        AND phase IN ('CONFIRMED','INVALIDATED','EXPIRED') AND resolved_time<%s AND stage_time>=%s AND candidate_id<>%s""",
        (context, event["pattern"], phase, bucket(event), as_of, as_of - 365 * 86400, event["id"]),
    ).fetchone()
    return {
        **probability(w, n),
        "formation": probability(f, n) if phase == "FORMING" else None,
        "as_of": as_of,
        "score_bucket": bucket(event),
        "basis": "PRIOR_YEAR_RESOLVED_ONLY",
    }


def observed_reference(conn, replay_context, recorded_context, event, as_of, *, score_bucket_override=None):
    """Actual observations take precedence over replay of the same candidate.

    Resolve as-of, not with a future outcome. Conservative chronological overlap
    exclusion is applied across the TWO sources, before filtering successes.
    """
    counts = conn.execute(
        """WITH candidates AS (
        SELECT DISTINCT ON(candidate_id) context_id,candidate_id,phase,stage_time,resolved_time,sampled,formed
        FROM market_structure_stages WHERE context_id=ANY(%s) AND pattern=%s AND entry_phase=%s
          AND score_bucket=%s AND stage_time<%s AND stage_time>=%s AND candidate_id<>%s
        ORDER BY candidate_id,(context_id=%s) DESC), ordered AS (
        SELECT *,max(CASE WHEN resolved_time<%s THEN resolved_time ELSE %s END)
          OVER(ORDER BY stage_time,candidate_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_end
        FROM candidates WHERE sampled), eligible AS (
        SELECT * FROM ordered WHERE (prior_end IS NULL OR stage_time>prior_end)
          AND resolved_time<%s AND phase IN ('CONFIRMED','INVALIDATED','EXPIRED'))
        SELECT count(*),count(*) FILTER(WHERE phase='CONFIRMED'),
          count(*) FILTER(WHERE context_id=%s),count(*) FILTER(WHERE phase='INVALIDATED'),
          count(*) FILTER(WHERE phase='EXPIRED'),count(*) FILTER(WHERE formed),
          (SELECT count(*) FROM ordered WHERE stage_time<=prior_end),
          COALESCE((SELECT jsonb_agg(example ORDER BY resolved_time DESC,candidate_id) FROM (
            SELECT resolved_time,candidate_id,jsonb_build_object('context_id',context_id,'id',candidate_id,
              'phase',phase,'observed_time',resolved_time,'source',CASE WHEN context_id=%s THEN 'RECORDED' ELSE 'REPLAY' END) AS example
            FROM eligible WHERE phase IN ('INVALIDATED','EXPIRED') ORDER BY resolved_time DESC,candidate_id LIMIT 3
          ) failures),'[]'::jsonb)
        FROM eligible""",
        (
            [c for c in (replay_context, recorded_context) if c],
            event["pattern"],
            event["phase"],
            score_bucket_override or bucket(event),
            as_of,
            as_of - 365 * 86400,
            event["id"],
            recorded_context,
            as_of,
            as_of,
            as_of,
            recorded_context,
            recorded_context,
        ),
    ).fetchone()
    total, wins, recorded, invalidated, expired, formed, overlap, examples = counts
    return {
        **probability(wins, total),
        "as_of": as_of,
        "score_bucket": score_bucket_override or bucket(event),
        "basis": "RECORDED_AND_REPLAY_DEDUPED_V1",
        "recorded_count": recorded,
        "replay_count": total - recorded,
        "invalidated": invalidated,
        "expired": expired,
        "overlap_excluded": overlap,
        "failure_examples": examples,
        "formation": probability(formed, total) if event["phase"] == "FORMING" else None,
    }


def record_reference(conn, history_context, record, as_of, *, recorded_context=None):
    """Display historical evidence for a record's original decision stage.

    Terminal records have no second chance to become confirmed. Their reference
    describes the already-recorded FORMED decision (or FORMING if that was the
    only stage), using its original score stratum and strictly earlier resolved
    cases. The immutable prediction/journal is not rewritten; this is a separate,
    reproducible retrospective reference using the same model's replay evidence.

    An absent stage/history projection is N/A, not a measured zero. Once a
    projection exists, zero matching resolved cases is a real empty cohort.
    Existing ``reference`` remains the detector's unchanged prediction path.
    """
    stages = record.get("stages", {}) if isinstance(record, Mapping) else {}
    stages = stages if isinstance(stages, Mapping) else {}
    phase = "FORMED" if "FORMED" in stages else "FORMING" if "FORMING" in stages else None
    stage = stages.get(phase, {}) if phase else {}
    stage = stage if isinstance(stage, Mapping) else {}
    stage_time = stage.get("time")
    score = stage.get("score_bucket")
    candidate = record.get("id") if isinstance(record, Mapping) else None
    pattern = record.get("pattern") if isinstance(record, Mapping) else None
    current_phase = record.get("phase") if isinstance(record, Mapping) else None

    def valid_time(value):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 253402300799

    metadata = {
        "basis": "ENTRY_STAGE_HISTORICAL_REFERENCE_V1",
        "reference_phase": phase,
        "reference_as_of": stage_time if valid_time(stage_time) else None,
        "reference_score_bucket": score if score in ("UNSCORED", "LOW", "MID", "HIGH") else None,
        "record_phase": current_phase,
        "reference_is_live_prediction": False,
        "reference_label": "形成时的历史突破确认率" if phase == "FORMED" else "首次形成中时的历史确认率",
    }

    def unavailable(state, reason):
        empty = {**probability(0, 0), "sample_count": None, "successes": None}
        return {
            **empty,
            **metadata,
            "reference_state": state,
            "reference_reason": reason,
            "formation": dict(empty) if phase == "FORMING" else None,
            "recorded_count": None,
            "replay_count": None,
            "invalidated": None,
            "expired": None,
            "overlap_excluded": None,
            "failure_examples": [],
        }

    if (
        not phase
        or not valid_time(stage_time)
        or score not in ("UNSCORED", "LOW", "MID", "HIGH")
        or not isinstance(candidate, str)
        or not candidate
        or not isinstance(pattern, str)
        or not pattern
    ):
        return unavailable("STAGE_UNAVAILABLE", "原始判断阶段或评分分组缺失，不能用当前状态补造历史样本")
    if not valid_time(as_of) or stage_time > as_of:
        return unavailable("STAGE_UNAVAILABLE", "原始判断时间尚未到达当前数据边界")
    contexts = list(dict.fromkeys(c for c in (history_context, recorded_context) if c))
    if not contexts:
        return unavailable("HISTORY_NOT_READY", "同版本历史回放与实测样本尚未就绪")
    ready = conn.execute(
        """SELECT 1 FROM market_structure_stages
        WHERE context_id=ANY(%s) AND candidate_id<>%s LIMIT 1""",
        (contexts, candidate),
    ).fetchone()
    if not ready:
        return unavailable("HISTORY_NOT_READY", "同版本历史样本投影尚未就绪，暂不显示为零样本")
    # Never use a terminal/latest geometry score to select the earlier stage's
    # stratum. No fallback to ALL or other model versions to inflate its count.
    event = {"id": candidate, "pattern": pattern, "phase": phase}
    result = observed_reference(
        conn, history_context, recorded_context, event, stage_time, score_bucket_override=score
    )
    return {
        **result,
        **metadata,
        "history_basis": result["basis"],
        "reference_state": "READY" if result["sample_count"] else "NO_MATCHING_SAMPLES",
        "reference_reason": "此前同类、同阶段、同评分分组已结束的非重叠样本；不是当前交易盈利概率"
        if result["sample_count"]
        else "已有历史投影，但原始判断时刻之前没有匹配的已结束样本",
    }


def report(conn, context, state, metadata, count, first_time, *, environment_key=None):
    through = state["last_ts"] + state["step_seconds"]
    left = max(metadata["requested_from"], through - 365 * 86400)
    grouped = conn.execute(
        """SELECT pattern,entry_phase,score_bucket,phase,sampled,formed,count(*),
        count(prediction) FILTER(WHERE prediction BETWEEN 0 AND 1),
        sum(power(prediction-CASE WHEN phase='CONFIRMED' THEN 1 ELSE 0 END,2)) FILTER(WHERE prediction BETWEEN 0 AND 1),
        CASE WHEN stage_time>=%s THEN 'RECENT' ELSE 'OLDER' END,
        CASE WHEN prediction BETWEEN 0 AND 1 THEN least(4,floor(prediction*5)::integer) ELSE -1 END,
        sum(prediction) FILTER(WHERE prediction BETWEEN 0 AND 1), coalesce(resolved_time<%s,false),
        coalesce(environment_key=%s,false),environment_key IS NOT NULL
        FROM market_structure_stages WHERE context_id=%s AND stage_time>=%s AND stage_time<%s
        GROUP BY pattern,entry_phase,score_bucket,phase,sampled,formed,10,11,13,14,15""",
        (max(left, through - 30 * 86400), through, environment_key, context, left, through),
    ).fetchall()
    completion = []
    for meta in catalog():
        for phase in ("FORMING", "FORMED"):
            for score in ("ALL", "UNSCORED", "LOW", "MID", "HIGH"):
                cases = [
                    r
                    for r in grouped
                    if r[0] == meta["id"] and r[1] == phase and (score == "ALL" or r[2] == score)
                ]
                counts = {p: sum(r[6] for r in cases if r[3] == p) for p in PHASE_LABELS}
                done = [
                    r for r in cases if r[4] and r[3] in TERMINAL - {"CENSORED"} and (len(r) < 13 or r[12])
                ]
                n = sum(r[6] for r in done)
                w = sum(r[6] for r in done if r[3] == "CONFIRMED")
                cal = sum(r[7] for r in done)
                completion.append(
                    {
                        "pattern": meta["id"],
                        "phase": phase,
                        "score_bucket": score,
                        **counts,
                        "total": sum(r[6] for r in cases),
                        "pending": counts["FORMING"] + counts["FORMED"],
                        "overlap_excluded": sum(r[6] for r in cases if not r[4]),
                        "formed_count": sum(r[6] for r in cases if r[5]),
                        **probability(w, n),
                        "formation": probability(sum(r[6] for r in done if r[5]), n)
                        if phase == "FORMING"
                        else None,
                        "calibration_count": cal,
                        "brier": sum(r[8] or 0 for r in done) / cal if cal else None,
                        **(
                            {
                                "reliability": reliability_comparison(
                                    cases,
                                    phase,
                                    through=through,
                                    annual_from=max(first_time, left),
                                    environment_key=environment_key,
                                )
                            }
                            if score == "ALL" and cases and all(len(r) >= 13 for r in cases)
                            else {}
                        ),
                    }
                )
    returns = conn.execute(
        """SELECT pattern,phase,horizon,status,count(*),count(*) FILTER(WHERE net_return>0),
        avg(net_return),avg(mfe),avg(mae) FROM market_structure_returns WHERE context_id=%s AND observed_time>=%s
        GROUP BY pattern,phase,horizon,status""",
        (context, left),
    ).fetchall()
    recent = conn.execute(
        """SELECT pattern,phase,horizon,observed_time,entry_time,exit_time,side,net_return,evidence_id FROM (
        SELECT *,row_number() OVER(PARTITION BY pattern,phase,horizon ORDER BY observed_time DESC,evidence_id) AS rank
        FROM market_structure_returns WHERE context_id=%s AND status='COMPLETE' AND observed_time>=%s) r WHERE rank<=5""",
        (context, left),
    ).fetchall()
    rows = []
    hypotheses = len(catalog()) * 2 * len(HORIZONS)
    for meta in catalog():
        for phase in ("FORMED", "CONFIRMED"):
            for horizon in HORIZONS:
                return_cases = {r[3]: r for r in returns if r[:3] == (meta["id"], phase, horizon)}
                completed = return_cases.get("COMPLETE")
                n = completed[4] if completed else 0
                w = completed[5] if completed else 0
                interval = None
                if n >= 20:
                    p = w / n
                    z = NormalDist().inv_cdf(1 - 0.05 / (2 * hypotheses))
                    den = 1 + z * z / n
                    center = (p + z * z / (2 * n)) / den
                    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
                    interval = {"lower": max(0, center - half), "upper": min(1, center + half)}
                samples = [
                    dict(
                        zip(
                            ("observed_time", "entry_time", "exit_time", "side", "net_return", "evidence_id"),
                            r[3:],
                            strict=True,
                        )
                    )
                    for r in recent
                    if r[:3] == (meta["id"], phase, horizon)
                ]
                rows.append(
                    {
                        "pattern": meta["id"],
                        "phase": phase,
                        "horizon": horizon,
                        "detected": sum(r[4] for r in return_cases.values()),
                        "pending": return_cases.get("PENDING", [0] * 5)[4],
                        "overlap_excluded": return_cases.get("OVERLAP", [0] * 5)[4],
                        "neutral_excluded": return_cases.get("NEUTRAL", [0] * 5)[4],
                        "censored": return_cases.get("CENSORED", [0] * 5)[4],
                        "sample_count": n,
                        "win_rate": w / n if n else None,
                        "interval": interval,
                        "mean_net_return": completed[6] if completed else None,
                        "mean_mfe": completed[7] if completed else None,
                        "mean_mae": completed[8] if completed else None,
                        "recent_samples": list(reversed(samples)),
                    }
                )
    candidate_count = conn.execute(
        "SELECT count(DISTINCT candidate_id) FROM market_structure_stages WHERE context_id=%s AND stage_time>=%s",
        (context, left),
    ).fetchone()[0]
    forecasts = conn.execute(
        """SELECT outcome_json->>'status',count(*) FROM market_structure_stages
        WHERE context_id=%s AND entry_phase='FORMING' AND sampled AND stage_time>=%s GROUP BY 1""",
        (context, left),
    ).fetchall()
    return {
        "version": VERSION,
        "catalog": reference_catalog(),
        "bar_count": count,
        "from_time": max(first_time, left),
        "to_time": through,
        "window": WINDOW,
        "pivot_order": PIVOT_ORDER,
        "minimum_score": 0,
        "quality_reference_score": MINIMUM,
        "round_trip_cost": ROUND_TRIP_COST,
        "hypotheses": hypotheses,
        "availability_mode": "HISTORICAL_CLOSE",
        "metadata": metadata,
        "rows": rows,
        "completion_rows": completion,
        "candidate_count": candidate_count,
        "lifecycle": True,
        "forecast_outcomes": {r[0]: r[1] for r in forecasts if r[0]},
        "forecast_basis": "FIRST_FORECAST_NONOVERLAPPING",
    }


def compact(state):
    cutoff = state["last_index"] - WINDOW
    state["records"] = {
        k: r for k, r in state["records"].items() if r["phase"] not in TERMINAL or r["last_index"] >= cutoff
    }
    # Sampling pointers to pruned resolved cases may be forgotten safely.
    state["sampling"] = {k: v for k, v in state.get("sampling", {}).items() if v in state["records"]}
