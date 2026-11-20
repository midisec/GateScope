from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import UUID
from jsonschema import Draft202012Validator

from gatescope.application.alerts import AlertRuleSnapshot, PatternObservation, evaluate_alert


NOW = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
BAR_OPEN = NOW - timedelta(minutes=1)


def rule(match_mode: str = "ANY") -> AlertRuleSnapshot:
    return AlertRuleSnapshot(
        rule_id=UUID("10000000-0000-0000-0000-000000000001"),
        version_id=UUID("10000000-0000-0000-0000-000000000002"),
        version=1,
        instrument_id=UUID("10000000-0000-0000-0000-000000000003"),
        timeframe="1m",
        pattern_ids=("double_bottom", "bull_flag"),
        match_mode=match_mode,  # type: ignore[arg-type]
        minimum_confidence=0.6,
        confirmed_only=True,
        coincidence_bars=3,
        cooldown_seconds=60,
    )


def observation(pattern: str, index: int, *, confidence: float = 0.8) -> PatternObservation:
    return PatternObservation(
        fact_id=f"fact-{pattern}-{index}",
        series_id=f"series-{pattern}",
        pattern_id=pattern,
        confidence=confidence,
        confirmed=True,
        end_time=NOW - timedelta(minutes=index),
        available_at=NOW - timedelta(minutes=index) + timedelta(seconds=30),
        payload_hash="a" * 64,
        direction="bullish",
    )


def test_any_alert_is_deterministic_and_replay_has_same_key() -> None:
    observations = [observation("double_bottom", 1), observation("bull_flag", 2)]
    first = evaluate_alert(rule(), observations, bar_open_time=BAR_OPEN, watermark=NOW, now=NOW)
    replay = evaluate_alert(rule(), reversed(observations), bar_open_time=BAR_OPEN, watermark=NOW, now=NOW)
    assert first is not None and replay is not None
    assert first.event_key == replay.event_key
    assert first.matched_fact_ids == ("fact-double_bottom-1",)
    assert first.payload_hash == replay.payload_hash


def test_alert_freezes_trade_notice_and_source_evidence():
    source = {"id": "original-candidate", "name": "双底", "evidence_id": "f" * 64}
    notice = {"version": "pattern-trade-notice-1", "status": "UNAVAILABLE", "auto_order": False}
    item = replace(observation("double_bottom", 1), source_event=source, trade_notice=notice)
    decision = evaluate_alert(rule(), [item], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW)
    assert decision is not None
    source["name"] = "later-revised-name"
    notice["status"] = "READY"
    payload = json.loads(decision.payload_json)
    assert payload["matched"][0]["source_event"]["name"] == "双底"
    assert payload["matched"][0]["trade_notice"]["status"] == "UNAVAILABLE"
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts/events/notification.requested.v1.schema.json").read_text())
    Draft202012Validator(schema).validate(payload)


def test_all_requires_every_pattern_in_same_causal_window() -> None:
    assert (
        evaluate_alert(
            rule("ALL"),
            [observation("double_bottom", 1)],
            bar_open_time=BAR_OPEN,
            watermark=NOW,
            now=NOW,
        )
        is None
    )
    decision = evaluate_alert(
        rule("ALL"),
        [observation("double_bottom", 1), observation("bull_flag", 2)],
        bar_open_time=BAR_OPEN,
        watermark=NOW,
        now=NOW,
    )
    assert decision is not None
    assert decision.matched_fact_ids == ("fact-bull_flag-2", "fact-double_bottom-1")


def test_alert_rejects_future_stale_low_confidence_and_cooldown() -> None:
    current_rule = rule()
    assert (
        evaluate_alert(
            current_rule,
            [observation("double_bottom", 1, confidence=0.4)],
            bar_open_time=BAR_OPEN,
            watermark=NOW,
            now=NOW,
        )
        is None
    )
    future = observation("double_bottom", -1)
    assert evaluate_alert(current_rule, [future], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW) is None
    stale = observation("double_bottom", 10)
    assert evaluate_alert(current_rule, [stale], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW) is None
    assert (
        evaluate_alert(
            current_rule,
            [observation("double_bottom", 1)],
            bar_open_time=BAR_OPEN,
            watermark=NOW,
            now=NOW,
            last_emitted_at=NOW - timedelta(seconds=30),
        )
        is None
    )


def test_old_watermark_cannot_make_old_candles_current_after_restart():
    assert (
        evaluate_alert(
            rule(),
            [observation("double_bottom", 1)],
            bar_open_time=BAR_OPEN,
            watermark=NOW,
            now=NOW + timedelta(days=1),
        )
        is None
    )
    assert (
        evaluate_alert(
            rule(),
            [observation("double_bottom", 1)],
            bar_open_time=BAR_OPEN,
            watermark=NOW + timedelta(seconds=1),
            now=NOW,
        )
        is None
    )


def test_all_rejects_opposing_or_unknown_directions():
    bullish = observation("double_bottom", 1)
    for direction in ("bearish", "neutral"):
        other = replace(observation("bull_flag", 1), direction=direction)
        assert (
            evaluate_alert(rule("ALL"), [bullish, other], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW)
            is None
        )


def test_signal_age_is_phase_observation_not_old_pivot_or_later_delivery():
    confirmed = replace(
        observation("double_bottom", 20),
        observed_at=NOW,
        available_at=NOW,
        detector_version="structure-study-3.2",
        phase="CONFIRMED",
    )
    decision = evaluate_alert(rule(), [confirmed], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW)
    assert decision is not None
    payload = json.loads(decision.payload_json)
    assert payload["observed_at"] == NOW.isoformat()
    assert payload["expires_at"] == (NOW + timedelta(minutes=2)).isoformat()
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/events/notification.requested.v1.schema.json').read_text())
    Draft202012Validator(schema,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(payload)
    stale = replace(confirmed, observed_at=NOW - timedelta(minutes=2))
    assert evaluate_alert(rule(), [stale], bar_open_time=BAR_OPEN, watermark=NOW, now=NOW) is None
