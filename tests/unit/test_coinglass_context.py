from __future__ import annotations

import copy
import math

import pytest

from gatescope.domain.coinglass_context import build_review, match_instrument, normalize_series

OPEN = 1_800_000_000
NOW = OPEN + 180
INSTRUMENT = {"id": "local-btc", "venue": "GATE", "market": "FUTURES_USDT", "symbol": "BTC_USDT",
              "base_asset": "BTC", "quote_asset": "USDT", "settle_asset": "USDT"}
PAIR = {"instrument_id": "BTC_USDT", "base_asset": "BTC", "quote_asset": "USDT",
        "settlement_currency": "USDT", "funding_interval": 8}


def row(timestamp=OPEN, value="100"):
    return {"time": timestamp * 1000, "open": value, "high": value, "low": value, "close": value}


def bundle():
    return {"version": "coinglass-context-1", "status": "PARTIAL", "instrument_id": "local-btc", "timeframe": "1m",
            "scope": match_instrument(INSTRUMENT, {"Gate": [PAIR]})["scope"],
            "match": {"status": "EXACT"}, "fetched_at": NOW, "expires_at": NOW + 120, "snapshot_id": "fixture",
            "series": [normalize_series("oi", [row(), row(OPEN + 60, "110")], timeframe="1m", fetched_at=NOW)]}


def event(*, bullish=True, forming=False):
    return {"id": "candidate", "evidence_id": "evidence", "instrument_id": "local-btc", "timeframe": "1m",
            "pattern": "double_bottom" if bullish else "double_top", "phase": "FORMING" if forming else "FORMED",
            "signal_direction": "neutral" if forming else "bullish" if bullish else "bearish", "confidence": .8,
            "forecast": {"id": "forecast", "kind": "NEXT_PIVOT" if forming else "CONDITIONAL_MOVE",
                         "issued_at": OPEN, "deadline_time": NOW + 300,
                         "target": {"price": 115 if bullish else 85, "lower": 110 if bullish else 80, "upper": 120 if bullish else 90}},
            "levels": {"buffer": 1, "closes": 2, "invalid_price": 90 if bullish else 110,
                       "invalid_side": -1 if bullish else 1,
                       "trigger": {"from": {"time": OPEN, "price": 100}, "to": {"time": OPEN + 60, "price": 101}}}}


def test_exact_full_identity_and_funding_interval():
    result = match_instrument(INSTRUMENT, {"Gate": [PAIR]})
    assert result["status"] == "EXACT"
    assert result["scope"]["funding_interval_seconds"] == 28800
    assert result["scope"]["market"] == "FUTURES_USDT"


@pytest.mark.parametrize("change", [
    {"venue": "Binance"}, {"venue": None}, {"market": "spot"}, {"settle_asset": "BTC"},
    {"base_asset": "1000BTC"}, {"symbol": "BTCUSDT"}, {"contract_type": "DATED"},
    {"symbol": "BTC_USDT\u202e"}, {"base": "ETH"}, {"symbol": "BTC_USDT?key=bad"},
])
def test_dimension_mismatch_never_falls_back(change):
    assert match_instrument({**INSTRUMENT, **change}, {"Gate": [PAIR]})["status"] == "UNSUPPORTED"


def test_ambiguity_is_not_first_match_wins():
    result = match_instrument(INSTRUMENT, {"Gate": [PAIR, dict(PAIR)]})
    assert result["status"] == "AMBIGUOUS"
    assert result["scope"] is None


def test_exchange_must_be_explicit_in_list_catalog():
    assert match_instrument(INSTRUMENT, [PAIR])["status"] == "UNSUPPORTED"
    assert match_instrument(INSTRUMENT, [{**PAIR, "exchange": "Gate"}])["status"] == "EXACT"
    assert match_instrument(INSTRUMENT, {"Gate": [{**PAIR, "exchange": "Binance"}]})["status"] == "UNSUPPORTED"


@pytest.mark.parametrize("base", ["龙虾", "1000PEPE", "é"])
def test_unicode_nfc_and_multiplier_identity_preserved(base):
    local = {**INSTRUMENT, "base_asset": base, "symbol": base + "_USDT"}
    pair = {**PAIR, "base_asset": base, "instrument_id": base + "_USDT"}
    assert match_instrument(local, {"Gate": [pair]})["status"] == "EXACT"
    if base == "é":
        pair.update(base_asset="e\u0301", instrument_id="e\u0301_USDT")
        assert match_instrument(local, {"Gate": [pair]})["status"] == "EXACT"
    if base == "1000PEPE":
        pair.update(base_asset="PEPE", instrument_id="PEPE_USDT")
        assert match_instrument(local, {"Gate": [pair]})["status"] == "UNSUPPORTED"


def test_closed_sorted_deduplicated_and_known_at_collection():
    values = [row(OPEN + 60, "110"), row(), row(), row(NOW), row(NOW + 60)]
    result = normalize_series("oi", values, timeframe="1m", fetched_at=NOW)
    assert [point["time"] for point in result["points"]] == [OPEN, OPEN + 60]
    assert all(point["available_at"] == NOW for point in result["points"])
    assert result["status"] == "READY"
    assert "OPEN_OR_FUTURE_PERIOD_EXCLUDED" in result["quality_flags"]
    assert result["coverage"] == 1


def test_response_latency_does_not_finalize_an_open_provider_bar():
    result = normalize_series("oi", [row(OPEN + 60)], timeframe="1m", fetched_at=OPEN + 121, closed_at=OPEN + 119)
    assert not result["points"]


def test_conflicting_duplicates_drop_the_timestamp_regardless_of_order():
    rows = [row(), row(value="101"), row(OPEN + 60)]
    a = normalize_series("oi", rows, timeframe="1m", fetched_at=NOW)
    b = normalize_series("oi", rows[::-1], timeframe="1m", fetched_at=NOW)
    assert a == b
    assert [point["time"] for point in a["points"]] == [OPEN + 60]
    assert "CONFLICTING_DUPLICATE_EXCLUDED" in a["quality_flags"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, "NaN", True, -1, None])
def test_invalid_or_negative_oi_is_never_zero_filled(value):
    result = normalize_series("oi", [row(value=value)], timeframe="1m", fetched_at=NOW)
    assert result["points"] == []
    assert result["status"] == "EMPTY"


def test_gaps_remain_missing_and_not_invented_zeros():
    result = normalize_series("oi", [row(), row(OPEN + 120)], timeframe="1m", fetched_at=NOW)
    assert len(result["points"]) == 2
    assert result["coverage"] == pytest.approx(2 / 3)
    assert result["gaps"][0]["missing_count"] == 1
    assert result["status"] == "PARTIAL"


def test_coarser_or_wrong_population_is_rejected_not_upsampled():
    result = normalize_series("oi", [{**row(), "interval": "5m"}], timeframe="1m", fetched_at=NOW)
    assert result["status"] == "MISMATCH"
    assert not result["points"]
    ratio = {"time": OPEN * 1000, "global_account_long_short_ratio": 2, "population": "TOP_POSITIONS"}
    assert normalize_series("lsr", [ratio], timeframe="1m", fetched_at=NOW)["status"] == "MISMATCH"


def test_unknown_funding_unit_is_quarantined_and_percent_never_rescaled():
    unknown = normalize_series("funding", [row(value="0.01")], timeframe="1m", fetched_at=NOW, unit_status="UNKNOWN")
    assert unknown["status"] == "MISMATCH" and not unknown["points"]
    verified = normalize_series("funding", [row(value="-0.01")], timeframe="1m", fetched_at=NOW)
    assert verified["points"][0]["value"] == -.01


def test_provider_coin_unit_cannot_be_labeled_as_usd():
    result = normalize_series("oi", [{**row(), "unit": "coin"}], timeframe="1m", fetched_at=NOW)
    assert result["status"] == "MISMATCH" and not result["points"]


def test_invalid_ohlc_extrema_and_future_edge_projection_are_excluded():
    result = normalize_series("oi", [{**row(), "high": 90}], timeframe="1m", fetched_at=NOW)
    assert not result["points"]
    original = event()
    original["levels"]["trigger"]["to"]["time"] = NOW + 60
    review = build_review(original, bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert review["original_conditions"]["confirmation"] == []


def test_rolling_liquidation_and_seconds_timestamps_not_accepted():
    rows = [{"time": OPEN * 1000, "longLiquidationUsd24h": 100},
            {"time": OPEN, "long_liquidation_usd": 100}]
    assert not normalize_series("liquidation_long", rows, timeframe="1m", fetched_at=NOW)["points"]


def test_review_freezes_original_and_does_not_invent_probability_or_net_flow():
    original, snapshot = event(), bundle()
    before = copy.deepcopy((original, snapshot))
    result = build_review(original, snapshot, assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert (original, snapshot) == before
    assert result["original"]["forecast"] == original["forecast"]
    assert result["adjusted_formation_probability"] is None
    assert result["adjusted_win_rate"] is None
    assert result["auto_order"] is False
    assert result["metric_evidence"][0]["window_change_percent"] == pytest.approx(10)
    assert "不等于净多" in result["metric_evidence"][0]["note"]
    assert result["basis"] == "CURRENT_REASSESSMENT_NOT_ORIGINAL_EVIDENCE"
    result["original"]["forecast"]["target"]["price"] = 999
    assert original["forecast"]["target"]["price"] == 115


def test_sloping_confirmation_is_projected_to_decision_bar_not_issuance():
    result = build_review(event(), bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert result["original_conditions"]["confirmation"][0]["price"] == 103
    assert result["original_conditions"]["confirmation"][0]["projected_at"] == OPEN + 120


def test_forming_next_pivot_cannot_be_a_take_profit_or_reward():
    result = build_review(event(forming=True), bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120,
                          scenario={"entry": 100, "target_lower": 110, "target_upper": 120})
    assert result["target_semantics"] == "NEXT_PIVOT_OBSERVATION_NOT_TAKE_PROFIT"
    assert result["scenario"]["risk_reward"] is None
    assert result["scenario"]["direction"] == "neutral"
    assert result["scenario"]["status"] == "WAIT_STRUCTURE"


@pytest.mark.parametrize("bullish,lower,upper", [(True, 111, 122), (False, 78, 89)])
def test_long_short_manual_scenarios_have_symmetric_risk_reward(bullish, lower, upper):
    result = build_review(event(bullish=bullish), bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120,
                          scenario={"entry": 100, "target_lower": lower, "target_upper": upper})
    assert result["scenario"]["risk_reward"] == {"lower": 1, "upper": 2}
    assert result["scenario"]["costs_included"] is False


@pytest.mark.parametrize("change", [
    {"entry": 89}, {"target_lower": 90}, {"target_upper": 109}, {"entry": math.inf},
    {"target_lower": True}, {"direction": "bearish"}, {"stop": 80},
])
def test_illegal_manual_scenarios_rejected(change):
    scenario = {"entry": 100, "target_lower": 110, "target_upper": 120, **change}
    with pytest.raises(ValueError):
        build_review(event(), bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120, scenario=scenario)


@pytest.mark.parametrize("change", [
    {"phase": "INVALIDATED"}, {"instrument_id": "local-eth"}, {"timeframe": "5m"},
])
def test_terminal_or_mismatched_candidate_cannot_make_scenario(change):
    with pytest.raises(ValueError):
        build_review({**event(), **change}, bundle(), assessed_at=NOW, decision_bar_time=OPEN + 120,
                     scenario={"entry": 100, "target_lower": 110, "target_upper": 120})


def test_backdated_review_never_uses_data_fetched_later():
    result = build_review(event(), bundle(), assessed_at=NOW - 1, decision_bar_time=OPEN + 120)
    assert result["state"] == "UNAVAILABLE"
    assert result["metric_evidence"] == []
    assert "SNAPSHOT_TIME_INVALID" in result["reason_codes"]


def test_stale_evidence_cannot_create_new_scenario():
    snapshot = bundle()
    snapshot["expires_at"] = NOW
    with pytest.raises(ValueError):
        build_review(event(), snapshot, assessed_at=NOW, decision_bar_time=OPEN + 120,
                     scenario={"entry": 100, "target_lower": 110, "target_upper": 120})


@pytest.mark.parametrize("change", [
    {"expires_at": NOW}, {"fetched_at": NOW + 1}, {"native_timeframe": "5m"},
    {"expires_at": None}, {"fetched_at": None},
])
def test_review_does_not_use_an_expired_future_or_wrong_period_series(change):
    snapshot = bundle()
    snapshot["series"][0].update(change)
    result = build_review(event(), snapshot, assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert result["metric_evidence"] == []
    assert "NO_USABLE_METRICS" in result["reason_codes"]


def test_review_keeps_legacy_series_without_per_series_clock_compatible():
    snapshot = bundle()
    for name in ("fetched_at", "expires_at", "native_timeframe"):
        snapshot["series"][0].pop(name, None)
    result = build_review(event(), snapshot, assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert len(result["metric_evidence"]) == 1


def test_later_metric_expiry_does_not_extend_the_earlier_metric_in_review():
    snapshot = bundle()
    snapshot["series"][0]["expires_at"] = NOW
    later = normalize_series("lsr", [{"time": OPEN * 1000, "global_account_long_short_ratio": 1.1}],
        timeframe="1m", fetched_at=NOW)
    later.update(fetched_at=NOW, expires_at=NOW + 60)
    snapshot["series"].append(later)
    result = build_review(event(), snapshot, assessed_at=NOW, decision_bar_time=OPEN + 120)
    assert [item["id"] for item in result["metric_evidence"]] == ["lsr"]
