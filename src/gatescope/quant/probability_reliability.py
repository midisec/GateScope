"""Descriptive prequential reliability, never a new prediction or trade gate.

Input is a bounded SQL aggregation of immutable issue-time predictions and
subsequently resolved non-overlapping outcomes. Recent and annual windows overlap;
their differences are descriptive, not independent-sample significance tests.
"""
from __future__ import annotations

import math
from statistics import NormalDist

from gatescope.quant.structure_lifecycle import TRIALS

MINIMUM = 20
# Cover both time windows, five fixed bins + aggregate, and both endpoints.
COMPARISONS = TRIALS * 24


def _interval(successes, count):
    if count < MINIMUM:
        return None
    p, z = successes/count, NormalDist().inv_cdf(1-.05/(2*COMPARISONS))
    scale = 1+z*z/count
    centre = (p+z*z/(2*count))/scale
    radius = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/scale
    return {"lower": max(0., centre-radius), "upper": min(1., centre+radius)}


def _window(rows, phase, *, include_bins=True):
    sampled_count = sum(row[6] for row in rows)
    # A terminal state learned after the report cutoff is only UNRESOLVED at
    # this cutoff, even if the current projection already knows its outcome.
    censored_count = sum(row[6] for row in rows if row[12] and row[3] == "CENSORED")
    unresolved_count = sum(row[6] for row in rows if not row[12]
                           or row[3] not in ("CONFIRMED", "INVALIDATED", "EXPIRED", "CENSORED"))
    rows = [row for row in rows if row[12] and row[3] in ("CONFIRMED", "INVALIDATED", "EXPIRED")]
    count = sum(row[6] for row in rows)
    confirmed = sum(row[6] for row in rows if row[3] == "CONFIRMED")
    formed = sum(row[6] for row in rows if row[5])
    calibrated = sum(row[7] for row in rows)
    prediction_sum = sum(float(row[11] or 0) for row in rows)
    brier_sum = sum(float(row[8] or 0) for row in rows)
    predicted_successes = sum(row[7] for row in rows if row[3] == "CONFIRMED")
    bins = []
    for index in range(5 if include_bins else 0):
        selected = [row for row in rows if row[10] == index]
        n = sum(row[7] for row in selected)
        wins = sum(row[7] for row in selected if row[3] == "CONFIRMED")
        bins.append({"lower": index/5, "upper": (index+1)/5, "sample_count": n,
                     "mean_prediction": sum(float(row[11] or 0) for row in selected)/n if n else None,
                     "observed_confirmation_rate": wins/n if n else None, "interval": _interval(wins, n)})
    return {"status": "READY" if calibrated >= MINIMUM else "INSUFFICIENT_PREDICTIONS",
            "sample_count": count, "calibration_count": calibrated, "unpredicted_count": count-calibrated,
            "sampled_candidate_count": sampled_count, "unresolved_count": unresolved_count,
            "censored_count": censored_count,
            "confirmation_rate": confirmed/count if count else None,
            "confirmation_interval": _interval(confirmed, count),
            "formation_rate": formed/count if count and phase == "FORMING" else None,
            "formation_interval": _interval(formed, count) if phase == "FORMING" else None,
            "mean_prediction": prediction_sum/calibrated if calibrated else None,
            "predicted_cohort_confirmation_rate": predicted_successes/calibrated if calibrated else None,
            "overconfidence_gap": (prediction_sum-predicted_successes)/calibrated if calibrated else None,
            "brier": brier_sum/calibrated if calibrated else None, "bins": bins}


def reliability_comparison(cases, phase, *, through, annual_from, environment_key=None):
    # Legacy pre-change aggregated reports cannot reconstruct issue-time bins.
    rows = [row for row in cases if len(row) >= 13 and row[4]]
    annual = _window(rows, phase)
    recent = _window([row for row in rows if row[9] == "RECENT"], phase)
    environment = _window([row for row in rows if len(row) >= 15 and row[13]], phase, include_bins=False)
    known = sum(row[6] for row in rows if len(row) >= 15 and row[14] and row[12]
                and row[3] in ("CONFIRMED", "INVALIDATED", "EXPIRED"))
    return {"version": "probability-reliability-1", "as_of": through,
            "annual_from": annual_from, "recent_from": max(annual_from, through-30*86400),
            "annual": annual, "recent": recent,
            "brier_delta": recent["brier"]-annual["brier"] if recent["brier"] is not None and annual["brier"] is not None else None,
            "minimum_predictions": MINIMUM, "interval_method": "WILSON_BONFERRONI_FIXED_GRID",
            "interval_comparisons": COMPARISONS,
            "scope": "SAME_MODEL_CONTEXT_PATTERN_ENTRY_STAGE_ALL_GEOMETRY_SCORES",
            "comparison": "OVERLAPPING_WINDOWS_DESCRIPTIVE_NOT_SIGNIFICANCE_TEST",
            "current_regime_status": "UNAVAILABLE_CURRENT_CONTEXT" if environment_key is None else "READY" if environment["sample_count"] >= MINIMUM else "ACCUMULATING",
            "environment_reference": {"key": environment_key, "as_of": through, "matched": environment,
                "known_sample_count": known, "legacy_unlabelled_count": annual["sample_count"]-known,
                "fallback": "OVERALL" if environment_key is None or environment["sample_count"] < MINIMUM else "COMPARISON_ONLY",
                "confidence_applied": False},
            "formation_calibration_status": "UNAVAILABLE_FORMATION_PREDICTIONS_NOT_PROJECTED",
            "profit_probability": None}
