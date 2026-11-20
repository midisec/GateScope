"""Immutable, owner-isolated PAPER decision snapshots stored once per order.

No detector or provider runs here. Frozen raw candles and existing exact archive
transitions are restricted to facts available at the declared decision time.
Client reasoning and Coinglass summaries remain explicitly unverified claims.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres_paper import PaperPreconditionFailed
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.domain.paper_decision import PaperDecisionContext
from gatescope.indicators.builtins import EMA, RSI

MAX_SNAPSHOT_BYTES = 256 * 1024
BAR_LIMIT = 256


def _json(value):
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _encoded(value):
    return json.dumps(_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def decision_snapshot(connection, tenant_id, owner_id, instrument_id, symbol, timeframe, context, *, now):
    """Called only inside the order transaction; never creates orphan records."""
    from gatescope.quant.structure_archive_codec import unpack_structure_record

    validated = PaperDecisionContext.model_validate(context)
    observed = validated.observed_at
    if observed > now or now-observed > timedelta(days=7):
        raise PaperPreconditionFailed("DECISION_TIME_OUT_OF_RANGE")
    connection.execute("SET LOCAL statement_timeout='2s'")
    step = TIMEFRAME_SECONDS[timeframe]
    # Indexed instrument/timeframe/open-time slice: do not sort an entire year
    # while an account lock is held. Two windows allow visible gaps, never fill them.
    rows = connection.execute("""SELECT * FROM (
        SELECT DISTINCT ON(open_time) open_time,close_time,open,high,low,close,volume,
          revision,available_at,payload_hash,quality_flags,created_at
        FROM bars WHERE instrument_id=%s AND timeframe=%s AND is_final=true
          AND open_time>=%s AND close_time<=%s AND available_at<=%s AND created_at<=%s
        ORDER BY open_time DESC,revision DESC
        LIMIT %s) frozen ORDER BY open_time""",
        (instrument_id, timeframe, observed-timedelta(seconds=step*BAR_LIMIT*2), observed, observed, observed, BAR_LIMIT)).fetchall()
    bars = [_json(dict(zip(("open_time", "close_time", "open", "high", "low", "close", "volume",
                            "revision", "available_at", "payload_hash", "quality_flags", "created_at"), row, strict=True))) for row in rows]
    for bar in bars:
        bar["is_final"] = True
    indicators: dict[str, EMA | RSI] = {"EMA9": EMA({"period": 9}), "EMA21": EMA({"period": 21}), "RSI14": RSI({"period": 14})}
    series: dict[str, list[dict]] = {key: [] for key in indicators}
    gaps = []
    previous = None
    for bar in bars:
        opened = datetime.fromisoformat(bar["open_time"])
        if previous is not None and opened-previous != timedelta(seconds=step):
            gaps.append({"after": previous.isoformat(), "before": opened.isoformat()})
            # The platform resets at missing candles; don't bridge unknown prices.
            indicators = {"EMA9": EMA({"period": 9}), "EMA21": EMA({"period": 21}), "RSI14": RSI({"period": 14})}
        previous = opened
        for name, indicator in indicators.items():
            value = next(iter(indicator.update(SimpleNamespace(close=Decimal(bar["close"])), {}).values()))
            series[name].append({"open_time": bar["open_time"], "value": value})
    events = []
    for reference in validated.pattern_events:
        row = connection.execute("""SELECT version,source_mode,phase,observed_time,recorded_at,
          gatescope_security.structure_event_record(event_json,event_blob)
          FROM market_pattern_events WHERE context_id=%s AND candidate_id=%s AND sequence=%s
            AND instrument_id=%s AND timeframe=%s AND observed_time<=%s AND recorded_at<=%s""",
            (reference.context_id, reference.candidate_id, reference.sequence, instrument_id, timeframe, observed, observed)).fetchone()
        if row is None:
            raise PaperPreconditionFailed("DECISION_PATTERN_NOT_AVAILABLE_AT_OBSERVATION")
        record = unpack_structure_record(row[5])
        # recorded_at is a transaction-start timestamp in legacy writers;
        # the payload's later computation clock must also be past-only.
        for clock_field in ("snapshot_at", "first_snapshot_at"):
            if record.get(clock_field) is not None:
                try:
                    clock = datetime.fromisoformat(record[clock_field])
                    if clock.tzinfo is None or clock > observed:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise PaperPreconditionFailed("DECISION_PATTERN_NOT_AVAILABLE_AT_OBSERVATION") from None
        events.append({**reference.model_dump(), "version": row[0], "source_mode": row[1], "phase": row[2],
                       "observed_time": _json(row[3]), "recorded_at": _json(row[4]),
                       "original_record_sha256": hashlib.sha256(_encoded(record)).hexdigest(), "record": record})
    payload = _json({
        "version": "paper-decision-1", "instrument_id": instrument_id, "symbol": symbol,
        "venue": "gate", "market": "futures_usdt", "timeframe": timeframe,
        "observed_at": observed, "captured_at": now,
        "server_evidence": {"availability_policy": "CLOSED_CANDLES_AND_ARCHIVE_FACTS_AVAILABLE_BY_OBSERVED_AT",
            "candles": bars, "bar_limit": BAR_LIMIT, "bar_count": len(bars),
            "window_limited": len(bars) == BAR_LIMIT,
            "last_closed_candle_at": bars[-1]["close_time"] if bars else None,
            "includes_unclosed_candles": False, "gaps": gaps,
            "data_status": "AVAILABLE_WITH_GAPS" if gaps else "AVAILABLE" if bars else "NO_CACHED_CANDLES",
            "indicators": series, "indicator_policy": "FROZEN_WINDOW_SMA_SEEDED_EMA_WILDER_RSI_RESET_AT_GAPS",
            "pattern_events": events},
        "client_analysis": {"verification": "UNVERIFIED_CLIENT_REPORTED_NOT_SERVER_FINDINGS",
            "rationale": validated.rationale, "analysis_model": validated.analysis_model,
            "coinglass_claims": [item.model_dump(mode="json") for item in validated.coinglass_claims]},
        "limitations": ["NOT_PROOF_CLIENT_SAW_ALL_SERVER_EVIDENCE", "NOT_A_PROFITABILITY_GUARANTEE",
                        "REPLAY_SOURCE_IS_NOT_A_LIVE_PREDICTION", "INDICATOR_WARMUP_IS_WINDOW_LIMITED"],
    })
    serialized = _encoded(payload)
    if len(serialized) > MAX_SNAPSHOT_BYTES:
        raise PaperPreconditionFailed("DECISION_EVIDENCE_TOO_LARGE")
    digest = hashlib.sha256(serialized).hexdigest()
    identity = uuid4()
    connection.execute("""INSERT INTO paper_decision_snapshots
      (id,tenant_id,owner_user_id,instrument_id,timeframe,observed_at,captured_at,evidence_digest,evidence_json)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
      (identity, tenant_id, owner_id, instrument_id, timeframe, observed, now, digest, Jsonb(payload)))
    return identity
