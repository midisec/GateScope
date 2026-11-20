import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gatescope.adapters.postgres_paper import PaperPreconditionFailed
from gatescope.adapters.postgres_paper_decisions import decision_snapshot
from gatescope.adapters.postgres_paper_orders import normalized_request, request_hash
from gatescope.adapters.postgres_worker_health import PostgresWorkerHealth, paper_execution_health
from gatescope.domain.paper_decision import PaperDecisionContext
from gatescope.quant.structure_archive_codec import pack_structure_record
from tests.unit.test_paper_orders_repository import opening

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def context(**overrides):
    return {"observed_at": NOW.isoformat(), "rationale": "突破后等待回踩，未到目标区间放弃追单", **overrides}


class EvidenceConnection:
    def __init__(self, bars=(), record=None):
        self.bars, self.record, self.saved, self.queries = bars, record, [], []

    def execute(self, sql, args=()):
        self.queries.append((sql, args))
        if sql.startswith("SET LOCAL"):
            return None
        if "FROM bars" in sql:
            assert "is_final=true" in sql and "close_time<=%s" in sql and "available_at<=%s" in sql
            assert "created_at<=%s" in sql and args[3:6] == (NOW, NOW, NOW) and args[-1] == 256
            return SimpleNamespace(fetchall=lambda: self.bars)
        if "FROM market_pattern_events" in sql:
            assert "recorded_at<=%s" in sql and "observed_time<=%s" in sql
            assert "structure_event_record(event_json,event_blob)" in sql
            row = ("test-1", "RECORDED", "FORMING", NOW, NOW, self.record) if self.record is not None else None
            return SimpleNamespace(fetchone=lambda: row)
        if "INSERT INTO paper_decision_snapshots" in sql:
            self.saved.append(copy.deepcopy(args[-1].obj))
            return None
        pytest.fail(sql)


def bars(count=30):
    return [(NOW-timedelta(minutes=5*(count-i)), NOW-timedelta(minutes=5*(count-i-1)),
             Decimal(100+i), Decimal(102+i), Decimal(99+i), Decimal(101+i), Decimal(10),
             1, NOW-timedelta(minutes=5*(count-i-1)), f"hash-{i}", [], NOW-timedelta(minutes=5*(count-i-1))) for i in range(count)]


def test_snapshot_preserves_frozen_candles_and_indicator_series_and_unverified_claim_label():
    conn = EvidenceConnection(bars())
    identifier = decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(
        analysis_model="client-model", coinglass_claims=[{"metric": "open_interest", "value": "123.45", "unit": "USD",
            "source_at": NOW.isoformat(), "market_scope": "SOL aggregate futures"}]), now=NOW)
    assert identifier and len(conn.saved) == 1
    saved = conn.saved[0]
    assert saved["server_evidence"]["candles"][0]["close"] == "101"
    assert saved["server_evidence"]["indicators"]["EMA21"][-1]["value"] is not None
    assert saved["server_evidence"]["last_closed_candle_at"] == NOW.isoformat()
    assert not saved["server_evidence"]["includes_unclosed_candles"]
    assert saved["client_analysis"]["verification"] == "UNVERIFIED_CLIENT_REPORTED_NOT_SERVER_FINDINGS"
    assert saved["client_analysis"]["coinglass_claims"][0]["value"] == "123.45"


def test_missing_bar_resets_indicator_warmup_and_missing_history_is_not_zero_probability():
    series = bars()
    conn = EvidenceConnection(series[:22]+series[23:])
    decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(), now=NOW)
    evidence = conn.saved[0]["server_evidence"]
    assert evidence["data_status"] == "AVAILABLE_WITH_GAPS" and len(evidence["gaps"]) == 1
    assert evidence["indicators"]["EMA9"][-1]["value"] is None
    empty = EvidenceConnection()
    decision_snapshot(empty, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(), now=NOW)
    assert empty.saved[0]["server_evidence"]["data_status"] == "NO_CACHED_CANDLES"


def test_original_compressed_shape_is_restored_without_later_reinterpretation():
    record = {"id": "case-1", "phase": "FORMING", "first": {"forecast": {"target": {"price": 120}}},
              "latest": {"name": "双底", "note": "original" * 1000}}
    header, blob = pack_structure_record(record)
    assert blob
    conn = EvidenceConnection(record={**header, "_archive_data": base64.b64encode(blob).decode()})
    decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(
        pattern_events=[{"context_id": "ctx-1", "candidate_id": "case-1", "sequence": 1}]), now=NOW)
    saved = conn.saved[0]["server_evidence"]["pattern_events"][0]
    assert saved["record"] == record and len(saved["original_record_sha256"]) == 64
    record["phase"] = "INVALIDATED"
    assert saved["record"]["phase"] == "FORMING"


def test_nonexistent_wrong_dimension_or_future_pattern_never_creates_snapshot():
    conn = EvidenceConnection()
    with pytest.raises(PaperPreconditionFailed, match="NOT_AVAILABLE"):
        decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(
            pattern_events=[{"context_id": "ctx-1", "candidate_id": "case-1", "sequence": 2}]), now=NOW)
    assert not conn.saved


def test_earlier_transaction_timestamp_cannot_hide_later_pattern_computation():
    conn = EvidenceConnection(record={"snapshot_at": (NOW+timedelta(seconds=1)).isoformat()})
    with pytest.raises(PaperPreconditionFailed, match="NOT_AVAILABLE"):
        decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(
            pattern_events=[{"context_id": "ctx-1", "candidate_id": "case-1", "sequence": 2}]), now=NOW)
    assert not conn.saved


@pytest.mark.parametrize("change", [
    {"observed_at": "2026-09-14T12:00:00"}, {"rationale": " "}, {"rationale": "x"*2001},
    {"secret": "must not accept arbitrary JSON"}, {"pattern_events": [{"context_id": "c", "candidate_id": "p", "sequence": True}]},
    {"pattern_events": [{"context_id": "c", "candidate_id": "p", "sequence": 1}]*2},
    {"coinglass_claims": [{"metric": "oi", "value": "NaN", "unit": "USD", "source_at": NOW.isoformat(), "market_scope": "SOL"}]},
    {"coinglass_claims": [{"metric": "oi", "value": "1", "unit": "USD", "source_at": (NOW+timedelta(seconds=1)).isoformat(), "market_scope": "SOL"}]},
])
def test_bounded_analysis_context_rejects_invalid_inputs(change):
    with pytest.raises(ValueError):
        PaperDecisionContext.model_validate(context(**change))


@pytest.mark.parametrize("observed", [NOW+timedelta(seconds=1), NOW-timedelta(days=8)])
def test_snapshot_time_boundaries_precede_database_reads(observed):
    conn = EvidenceConnection()
    with pytest.raises(PaperPreconditionFailed, match="TIME_OUT_OF_RANGE"):
        decision_snapshot(conn, uuid4(), uuid4(), uuid4(), "SOL_USDT", "5m", context(observed_at=observed.isoformat()), now=NOW)
    assert not conn.queries


def test_evidence_participates_in_idempotency_without_changing_existing_manual_hash():
    original = request_hash(normalized_request(opening()))
    assert original == request_hash(normalized_request(opening(decision_context=None)))
    associated = request_hash(normalized_request(opening(decision_context=context())))
    assert original != associated
    assert associated != request_hash(normalized_request(opening(decision_context=context(rationale="different"))))
    with pytest.raises(ValueError):
        normalized_request({"idempotency_key": "close-key", "decision_context": context()}, effect="CLOSE", position_id=uuid4())


def test_heartbeat_keeps_previous_success_on_failure_and_limits_logged_fields():
    queries = []
    @contextmanager
    def transaction():
        yield SimpleNamespace(execute=lambda *args: queries.append(args))
    health = PostgresWorkerHealth(SimpleNamespace(public_transaction=transaction))
    health.record("paper-order-worker", "test-worker", running=True, healthy=False,
                  safe_error="PAPER_ORDER_MARKET_UNAVAILABLE", counters={"unavailable": 2, "owner_id": "private"})
    sql, params = queries[-1]
    assert "ELSE worker_heartbeats.status_json->'last_success_at'" in sql
    assert params[3].obj["counters"] == {"unavailable": 2} and params[-2:] == (False, False)
    with pytest.raises(ValueError):
        health.record("paper-order-worker", "test-worker", running=True, safe_error="raw private error")


def test_account_business_health_hides_other_user_counts_and_requires_recent_success():
    assert paper_execution_health(None, NOW)["state"] == "UNAVAILABLE"
    assert paper_execution_health((True, NOW, NOW.isoformat(), None), NOW)["state"] == "HEALTHY"
    assert paper_execution_health((True, NOW-timedelta(seconds=46), NOW.isoformat(), None), NOW)["state"] == "STALE"
    assert paper_execution_health((True, NOW, (NOW-timedelta(seconds=46)).isoformat(), None), NOW)["state"] == "NO_RECENT_SUCCESS"
    result = paper_execution_health((True, NOW, NOW.isoformat(), "PAPER_ORDER_MARKET_UNAVAILABLE"), NOW)
    assert result["state"] == "DEGRADED" and "counters" not in result
