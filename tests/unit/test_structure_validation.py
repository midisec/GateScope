import pytest

from gatescope.quant.structure_validation import time_block_comparison


def daily(day, *, selected=.01, baseline=.01):
    return dict(day=day, selected_count=1, selected_sum=selected, selected_wins=int(selected > 0),
                baseline_count=2, baseline_sum=2 * baseline, baseline_wins=2 * int(baseline > 0))


def test_same_case_rule_has_no_fabricated_improvement():
    rows = [daily(day, selected=.01 if day % 7 else -.02, baseline=.01 if day % 7 else -.02) for day in range(84)]
    result = time_block_comparison(rows, resamples=200)
    assert result["status"] == "DESCRIPTIVE_ONLY"
    assert result["mean_net_return_delta"] == pytest.approx(0)
    assert result["mean_delta_interval"]["lower"] == pytest.approx(0)
    assert result["mean_delta_interval"]["upper"] == pytest.approx(0)
    assert not result["thresholds_enforced"] and not result["sealed_holdout"]
    assert not result["multiple_search_corrected"] and not result["portfolio_pnl"]


def test_joint_calendar_resampling_preserves_signal_retention_and_direction():
    rows = [daily(day, selected=.02, baseline=.01) for day in range(84)]
    result = time_block_comparison(rows, resamples=200)
    assert result["retention_ratio"] == .5
    assert result["mean_net_return_delta"] == pytest.approx(.01)
    assert result["mean_delta_interval"]["lower"] > 0
    assert result == time_block_comparison(rows, resamples=200)


def test_empty_and_single_market_burst_do_not_claim_a_reliable_interval():
    assert time_block_comparison([])["status"] == "INSUFFICIENT_SAMPLES"
    rows = [{**daily(1), "selected_count": 100, "selected_sum": 1., "selected_wins": 100,
             "baseline_count": 100, "baseline_sum": 1., "baseline_wins": 100}]
    assert time_block_comparison(rows)["status"] == "INSUFFICIENT_TIME_BLOCKS"


def test_missing_calendar_days_are_not_compressed_and_long_holding_needs_long_blocks():
    rows = [daily(day * 4) for day in range(30)]
    result = time_block_comparison(rows, resamples=100)
    assert result["calendar_days"] == 117
    assert result["approximate_time_blocks"] == 16
    assert time_block_comparison(rows, block_days=48)["status"] == "INSUFFICIENT_TIME_BLOCKS"


def test_duplicate_daily_groups_merge_without_losing_samples():
    rows = [daily(day) for day in range(60)]
    result = time_block_comparison(rows + rows, resamples=100)
    assert result["selected_samples"] == 120 and result["baseline_samples"] == 240
    assert result["calendar_days"] == 60


@pytest.mark.parametrize("row", [{"day": "today"}, {**daily(0), "selected_count": 3},
                                {**daily(0), "selected_sum": float("nan")}, {**daily(0), "selected_wins": 2}])
def test_invalid_daily_groups_fail_closed(row):
    with pytest.raises(ValueError):
        time_block_comparison([row])
