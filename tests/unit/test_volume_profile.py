from __future__ import annotations

import copy
import json
import math
import random
from decimal import Decimal
from uuid import UUID

import pytest

from gatescope.domain.volume_profile import (
    MAX_BARS, MAX_RANGE_SECONDS, VolumeProfileInputError, build_volume_profile,
    compute_volume_profile, plan_volume_profile,
)

OPEN = 1_800_000_000
INSTRUMENT = {"id": UUID("e3f9e4c1-f433-4581-ab69-666cbb896923"), "venue": "GATE", "market": "FUTURES_USDT", "symbol": "BTC_USDT"}


def bar(index=0, low=100, high=124, volume=240, **changes):
    ts = OPEN+index*60
    return {"ts": ts, "close_ts": ts+60, "open": low+(high-low)/2, "low": low,
            "high": high, "close": low+(high-low)/2, "volume": volume, "is_final": True,
            "revision": 1, "available_at": ts+60, "created_at": ts+60,
            "instrument_id": INSTRUMENT["id"], "venue": "GATE", "market": "FUTURES_USDT", "timeframe": "1m",
            "quality_flags": ["REST_POLLING"], **changes}


def result(source, *, count=5, instrument=INSTRUMENT, asof=OPEN+3600):
    plan = plan_volume_profile("1h", OPEN, OPEN+count*60, asof=asof)
    return build_volume_profile(instrument, plan, source, asof=asof)


@pytest.mark.parametrize("seconds,source", [(34*86400, "1m"), (50_000*60, "1m"), (50_001*60, "5m"), (366*86400, "15m")])
def test_finest_fixed_plan_is_bounded_without_consulting_data_completeness(seconds, source):
    plan = plan_volume_profile("1h", OPEN, OPEN+seconds, asof=OPEN+400*86400)
    assert plan.source_timeframe == source
    assert plan.expected_bars <= MAX_BARS


@pytest.mark.parametrize("changes", [
    {"timeframe": "2m"}, {"from_time": -1}, {"to_time": OPEN}, {"rows": 25}, {"rows": True},
    {"value_area": 49}, {"value_area": 96}, {"from_time": OPEN+.5},
    {"to_time": OPEN+MAX_RANGE_SECONDS+1}, {"from_time": OPEN+10000, "to_time": OPEN+12000},
])
def test_range_settings_and_future_limits(changes):
    args = dict(timeframe="1h", from_time=OPEN, to_time=OPEN+300, asof=OPEN+3600)
    args.update(changes)
    with pytest.raises(VolumeProfileInputError):
        plan_volume_profile(**args)


def test_cannot_satisfy_budget_by_silently_choosing_coarser_than_chart():
    with pytest.raises(VolumeProfileInputError):
        plan_volume_profile("1m", OPEN, OPEN+50_001*60, asof=OPEN+400*86400)


def test_full_coverage_exact_binding_metadata_and_no_mutation():
    source = [bar(index) for index in range(5)]
    original = copy.deepcopy(source)
    output = result(source)
    assert output["status"] == "AVAILABLE"
    assert output["coverage"] == 1
    assert output["used_bars"] == output["expected_bars"] == output["profile"]["includedCount"] == 5
    assert output["source_timeframe"] == "1m" and output["timeframe"] == "1h"
    assert output["from_time"] == OPEN and output["to_time"] == OPEN+300
    assert output["generated_at"] == OPEN+3600 and output["expires_at"] == OPEN+3630
    assert output["profile"]["firstTime"] == "2027-01-15T08:00:00+00:00"
    assert output["profile"]["lastTime"] == "2027-01-15T08:04:00+00:00"
    assert output["profile"]["firstIndex"] is output["profile"]["lastIndex"] is None
    assert output["profile"]["totalVolume"] == pytest.approx(1200)
    assert "REST_POLLING" in output["quality_flags"]
    assert source == original
    json.dumps(output, allow_nan=False)


def test_holes_oldest_and_latest_are_not_filled_or_replaced_with_a_coarse_source():
    output = result([bar(1), bar(3)])
    assert output["status"] == "PARTIAL" and output["coverage"] == .4
    assert output["source_timeframe"] == "1m" and output["expected_bars"] == 5
    assert output["profile"]["firstTime"].endswith("08:01:00+00:00")
    assert output["profile"]["lastTime"].endswith("08:03:00+00:00")
    empty = result([])
    assert empty["status"] == "NO_DATA" and empty["coverage"] == 0
    assert empty["source_timeframe"] == "1m"
    assert empty["profile"]["poc"] is None


def test_partial_unclosed_end_keeps_requested_binding_and_excludes_future_candle():
    output = result([bar(0), bar(1), bar(2)], count=3, asof=OPEN+125)
    assert output["to_time"] == OPEN+180 and output["effective_to_time"] == OPEN+125
    assert output["status"] == "PARTIAL" and output["coverage"] == pytest.approx(2/3)
    assert "FUTURE_RANGE_NOT_CLOSED" in output["quality_flags"]
    assert "FUTURE_SOURCE_REVISION_EXCLUDED" in output["quality_flags"]


def test_non_aligned_edges_do_not_invent_fractional_candle_volume():
    plan = plan_volume_profile("1h", OPEN+5, OPEN+305, asof=OPEN+3600)
    output = build_volume_profile(INSTRUMENT, plan, [bar(i) for i in range(6)], asof=OPEN+3600)
    assert output["expected_bars"] == output["used_bars"] == 4
    assert output["coverage"] == 1 and output["status"] == "PARTIAL"
    assert "PARTIAL_SOURCE_BOUNDARIES" in output["quality_flags"]


@pytest.mark.parametrize("change", [{"is_final": False}, {"is_final": None}, {"open": 300}, {"volume": -1}])
def test_latest_known_bad_or_non_final_revision_never_revives_older_final(change):
    source = [bar(0), bar(0, revision=2, **change)]
    assert result(source, count=1)["status"] == "NO_DATA"
    assert result(source[::-1], count=1)["status"] == "NO_DATA"


def test_latest_known_revision_and_identical_duplicates_do_not_double_count():
    source = [bar(0), bar(0, revision=2, volume=480), bar(0, revision=2, volume=480)]
    output = result(source, count=1)
    assert output["used_bars"] == 1 and output["profile"]["totalVolume"] == 480
    conflict = result([bar(0), bar(0, volume=999)], count=1)
    assert conflict["status"] == "NO_DATA"
    assert "CONFLICTING_SOURCE_REVISION_EXCLUDED" in conflict["quality_flags"]


def test_future_revision_not_yet_observable_does_not_erase_known_revision():
    output = result([bar(0), bar(0, revision=2, volume=999, available_at=OPEN+5000)], count=1)
    assert output["profile"]["totalVolume"] == 240
    assert "FUTURE_SOURCE_REVISION_EXCLUDED" in output["quality_flags"]


@pytest.mark.parametrize("change", [
    {"venue": "OTHER"}, {"market": "SPOT"}, {"instrument_id": UUID(int=5)}, {"timeframe": "5m"},
    {"available_at": None}, {"created_at": None}, {"created_at": OPEN+5000}, {"ts": Decimal(OPEN)+Decimal("0.5")},
    {"ts": OPEN+1, "close_ts": OPEN+61}, {"close_ts": OPEN+59}, {"revision": True},
    {"available_at": OPEN+59}, {"created_at": -1},
    {"volume": "1e-999"}, {"volume": "NaN"}, {"volume": "1_000"}, {"volume": True},
])
def test_foreign_unknown_future_and_invalid_precision_rows_cannot_enter_profile(change):
    assert result([bar(0, **change)], count=1)["status"] == "NO_DATA"


@pytest.mark.parametrize("flags", [[], ["GATE_MONTHLY_ARCHIVE"], ["REST_POLLING", "LEGACY_SOURCE"], ["REST_POLLING", "GATE_MONTHLY_ARCHIVE"]])
def test_spot_unverified_archive_units_are_excluded_not_mixed_even_for_percentages(flags):
    instrument = {**INSTRUMENT, "market": "SPOT"}
    output = result([bar(0, market="SPOT", quality_flags=["REST_POLLING"]), bar(1, market="SPOT", quality_flags=flags)],
                    count=2, instrument=instrument)
    assert output["used_bars"] == 1 and output["coverage"] == .5
    assert "SOURCE_VOLUME_UNIT_UNVERIFIED" in output["quality_flags"]
    assert output["profile"]["totalVolume"] == 240


def test_futures_archive_contract_volume_can_mix_with_same_instrument_rest():
    output = result([bar(0, quality_flags=["GATE_MONTHLY_ARCHIVE"]), bar(1)], count=2)
    assert output["status"] == "AVAILABLE" and output["used_bars"] == 2
    assert "VOLUME_UNIT_UNSPECIFIED" in output["quality_flags"]


def test_never_silently_truncates_to_latest_50000_rows():
    with pytest.raises(VolumeProfileInputError):
        result([bar()]*(MAX_BARS+1))


@pytest.mark.parametrize("rows", [24, 48, 96])
def test_math_conserves_volume_and_matches_slow_overlap_oracle(rows):
    randomizer = random.Random(782)
    source = []
    for index in range(240):
        low = 10+randomizer.random()*100
        source.append(bar(index, low=low, high=low+randomizer.random()*100, volume=randomizer.random()*1e6))
    output = compute_volume_profile(source, rows=rows)
    total = math.fsum(item["volume"] for item in source)
    for item in output["bins"]:
        expected = math.fsum(row["volume"]*max(0, min(item["high"], row["high"])-max(item["low"], row["low"]))
                             /(row["high"]-row["low"]) for row in source)
        assert item["volumeFraction"] == pytest.approx(expected/total, rel=1e-11, abs=1e-13)
    assert math.fsum(item["volume"] for item in output["bins"]) == pytest.approx(total)
    assert math.fsum(item["volumeFraction"] for item in output["bins"]) == pytest.approx(1)


def test_finer_candles_shift_poc_where_volume_was_concentrated_not_toward_coarse_uniform_close():
    coarse = compute_volume_profile([bar(0)], rows=24)
    fine = compute_volume_profile([bar(0, volume=0), bar(1, low=120, high=120)], rows=24)
    assert coarse["poc"] == 100.5 and fine["poc"] == 120.5
    assert coarse["totalVolume"] == fine["totalVolume"]


def test_partial_edge_allocation_and_poc_connected_value_area():
    output = compute_volume_profile([bar(0, volume=0), bar(1, low=100, high=104, volume=80),
                                     bar(2, low=100.5, high=101.5, volume=20)], rows=24)
    assert [item["volume"] for item in output["bins"][:4]] == pytest.approx([30, 30, 20, 20])
    uniform = compute_volume_profile([bar(low=100, high=148, volume=480)])
    assert (uniform["poc"], uniform["val"], uniform["vah"]) == (100.5, 100, 134)
    assert uniform["actualValueArea"] == pytest.approx(34/48)


def test_tiny_prices_flat_zero_and_volume_overflow_remain_honest_and_json_safe():
    tiny = compute_volume_profile([bar(low=1e-20, high=1.48e-20, volume=480)])
    assert tiny["effectiveBins"] == 48 and tiny["poc"] > 0
    flat = compute_volume_profile([bar(low=100, high=100, volume=12)])
    assert flat["effectiveBins"] == 1 and flat["poc"] == flat["val"] == flat["vah"] == 100
    zero = compute_volume_profile([bar(volume=0)])
    assert zero["status"] == "ZERO_VOLUME" and zero["poc"] is None
    assert result([bar(volume=0)], count=1)["status"] == "ZERO_VOLUME"
    huge = compute_volume_profile([bar(volume=1e308), bar(1, volume=1e308)])
    assert huge["totalVolume"] is None
    assert all(item["volume"] is None for item in huge["bins"])
    assert math.fsum(item["volumeFraction"] for item in huge["bins"]) == pytest.approx(1)
    narrow = compute_volume_profile([bar(low=1, high=1+math.ulp(1))])
    assert narrow["effectiveBins"] == 1 and "PRICE_BINS_COLLAPSED_AT_FLOAT_PRECISION" in narrow["qualityFlags"]
    for output in (tiny, flat, zero, huge, narrow):
        json.dumps(output, allow_nan=False)
