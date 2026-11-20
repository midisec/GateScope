from gatescope.quant.probability_reliability import reliability_comparison


def group(*, n=20, probability=.8, phase="CONFIRMED", recent=True, sampled=True, formed=True, before=True):
    predicted = n if probability is not None else 0
    return ("double_bottom", "FORMING", "HIGH", phase, sampled, formed, n, predicted,
            (probability-int(phase == "CONFIRMED"))**2*n if predicted else None,
            "RECENT" if recent else "OLDER", min(4, int(probability*5)) if predicted else -1,
            probability*n if predicted else None, before)


def compare(*rows):
    return reliability_comparison(rows, "FORMING", through=365*86400, annual_from=0)


def test_recent_and_annual_use_original_probabilities_on_identical_predicted_cohort():
    result = compare(group(n=20, probability=.8), group(n=20, probability=.8, phase="INVALIDATED"),
                     group(n=20, probability=.8, recent=False), group(n=7, probability=None, phase="EXPIRED"))
    recent, annual = result["recent"], result["annual"]
    assert recent["sample_count"] == 47 and recent["calibration_count"] == 40
    assert recent["unpredicted_count"] == 7
    assert recent["mean_prediction"] == .8 and recent["predicted_cohort_confirmation_rate"] == .5
    assert abs(recent["overconfidence_gap"]-.3) < 1e-12
    assert abs(recent["brier"]-.34) < 1e-12
    assert annual["sample_count"] == 67 and annual["calibration_count"] == 60
    assert result["brier_delta"] > 0 and result["comparison"] == "OVERLAPPING_WINDOWS_DESCRIPTIVE_NOT_SIGNIFICANCE_TEST"
    assert recent["bins"][4]["sample_count"] == 40 and recent["bins"][4]["interval"]


def test_failures_retained_pending_censored_overlap_and_future_resolutions_excluded():
    result = compare(group(phase="INVALIDATED"), group(phase="EXPIRED"), group(phase="FORMING"),
                     group(phase="CENSORED"), group(sampled=False), group(before=False))
    recent = result["recent"]
    assert recent["sample_count"] == 40 and recent["confirmation_rate"] == 0
    assert recent["sampled_candidate_count"] == 100
    assert recent["unresolved_count"] == 40 and recent["censored_count"] == 20
    assert recent["formation_rate"] == 1  # Previously formed, subsequently failed is still a formation success.
    assert result["profit_probability"] is None
    assert result["formation_calibration_status"].startswith("UNAVAILABLE")
    assert result["current_regime_status"].startswith("UNAVAILABLE")


def test_asof_exclusions_count_future_outcomes_only_as_unresolved():
    earlier = group(n=5, phase="CENSORED", recent=False)
    future_success = group(n=9, phase="CONFIRMED", before=False)
    future_censored = group(n=4, phase="CENSORED", before=False)
    result = compare(earlier, future_success, future_censored)
    assert result["annual"]["sample_count"] == 0
    assert result["annual"]["censored_count"] == 5
    assert result["annual"]["unresolved_count"] == 13
    assert result["recent"]["sampled_candidate_count"] == 13
    assert result["recent"]["censored_count"] == 0
    # Neither success/failure nor censoring status from the future is exposed.
    changed = compare(earlier, group(n=9, phase="INVALIDATED", before=False), future_censored)
    assert result == changed


def test_sparse_and_empty_buckets_are_uncertain_not_zero_probabilities():
    result = compare(group(n=3, probability=1))
    assert result["recent"]["status"] == "INSUFFICIENT_PREDICTIONS"
    assert result["recent"]["bins"][4]["sample_count"] == 3
    assert result["recent"]["bins"][4]["interval"] is None
    assert result["recent"]["bins"][0]["observed_confirmation_rate"] is None
    empty = compare()
    assert empty["recent"]["brier"] is None and empty["recent"]["confirmation_rate"] is None


def test_legacy_aggregates_cannot_invent_recent_windows_or_probability_bins():
    legacy = group()[:9]
    assert compare(legacy)["recent"]["calibration_count"] == 0


def test_environment_matches_only_frozen_known_labels_and_falls_back_without_replacing_confidence():
    legacy = group(n=100)+(False, False)
    matched = group(n=12)+(True, True)
    other = group(n=30)+(False, True)
    result = reliability_comparison([legacy, matched, other], "FORMING", through=365*86400,
                                    annual_from=0, environment_key="se1:UP:NORMAL:NORMAL")
    environment = result["environment_reference"]
    assert environment["matched"]["sample_count"] == 12
    assert environment["legacy_unlabelled_count"] == 100 and environment["known_sample_count"] == 42
    assert environment["fallback"] == "OVERALL" and not environment["confidence_applied"]
    assert result["current_regime_status"] == "ACCUMULATING" and result["annual"]["sample_count"] == 142
    ready = reliability_comparison([group(n=25)+(True, True)], "FORMING", through=365*86400,
                                   annual_from=0, environment_key="se1:UP:NORMAL:NORMAL")
    assert ready["current_regime_status"] == "READY"
    assert ready["environment_reference"]["matched"]["confirmation_interval"]
