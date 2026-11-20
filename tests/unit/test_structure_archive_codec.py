import base64
import copy
import hashlib
import zlib

import pytest

from gatescope.quant.structure_archive_codec import (
    MAX_RAW_BYTES,
    canonical_record,
    forecast_comparison,
    pack_structure_record,
    unpack_structure_record,
)


def record():
    forecast = {
        "id": "f" * 64,
        "issued_at": 100,
        "deadline_time": 800,
        "status": "PENDING",
        "kind": "NEXT_PIVOT",
        "condition": "历史形成条件与原始理论依据。" * 80,
        "history_count": 33,
        "target": {
            "price": 99.123456789,
            "lower": 98,
            "upper": 102,
            "theory_price": 100,
            "index": 3,
            "label": "波浪",
        },
        "confirmation": [{"side": 1, "price": 103, "edge": {"origin": 100, "slope": 0.2}}],
    }
    shape = {
        "id": "a" * 64,
        "pattern": "double_bottom",
        "name": "双底",
        "stage": "形成失败也保留",
        "evidence_id": "b" * 64,
        "points": [{"time": 11, "price": 99.123456789}],
        "lines": [{"from": 11, "to": 20, "price": 100}],
        "forecast": forecast,
    }
    return {
        "id": "a" * 64,
        "pattern": "double_bottom",
        "phase": "INVALIDATED",
        "sequence": 3,
        "reason": "INVALIDATION",
        "first_observed_time": 100,
        "observed_time": 200,
        "deadline_time": 800,
        "source_mode": "REPLAY",
        "snapshot_at": "2026-09-14T00:00:00+00:00",
        "latest": shape,
        "first": copy.deepcopy(shape),
        "stages": {"FORMING": {"sampled": True, "prediction": {"probability": 0.61, "sample_count": 100}}},
        "forecast_outcome": {"status": "NOT_REACHED", "target_price": 99.123456789},
    }


def envelope(original):
    header, blob = pack_structure_record(original)
    assert blob is not None
    return {**header, "_archive_data": base64.b64encode(blob).decode()}


def test_roundtrip_preserves_failed_case_original_geometry_precise_prices_and_prediction():
    original = record()
    frozen = copy.deepcopy(original)
    packed = envelope(original)
    assert unpack_structure_record(packed) == frozen and original == frozen
    assert packed["latest"]["evidence_id"] == original["latest"]["evidence_id"]
    assert "points" not in packed["latest"]
    assert len(canonical_record(packed)) < len(canonical_record(original))


def test_legacy_rows_and_small_rows_remain_unchanged():
    original = {"id": "legacy", "latest": {"name": "旧版本"}}
    assert pack_structure_record(original) == (original, None)
    assert unpack_structure_record(original) == original


@pytest.mark.parametrize(
    "mutation", ["hash", "size", "format", "body", "projection", "trailing", "missing", "bomb"]
)
def test_corrupt_or_unbounded_evidence_fails_closed(mutation):
    packed = envelope(record())
    if mutation == "hash":
        packed["_archive"]["sha256"] = "0" * 64
    if mutation == "size":
        packed["_archive"]["raw_bytes"] += 1
    if mutation == "format":
        packed["_archive"]["format"] = "unknown"
    if mutation == "body":
        packed["_archive_data"] = "not!base64"
    if mutation == "projection":
        packed["phase"] = "CONFIRMED"
    if mutation == "trailing":
        packed["_archive_data"] = base64.b64encode(base64.b64decode(packed["_archive_data"]) + b"x").decode()
    if mutation == "missing":
        del packed["_archive_data"]
    if mutation == "bomb":
        packed["_archive_data"] = base64.b64encode(zlib.compress(b"x" * 1000000)).decode()
        packed["_archive"]["raw_bytes"] = 8
    with pytest.raises(ValueError, match="INTEGRITY_FAILED"):
        unpack_structure_record(packed)


def test_oversized_declared_bytes_rejected():
    packed = envelope(record())
    packed["_archive"]["raw_bytes"] = MAX_RAW_BYTES + 1
    with pytest.raises(ValueError):
        unpack_structure_record(packed)


def test_comparison_keeps_target_history_and_original_reference_not_duplicate_prose():
    original = record()["first"]["forecast"]
    summary = forecast_comparison(original, 3)
    assert summary["target"]["price"] == original["target"]["price"]
    assert summary["history_count"] == 33 and summary["status"] == "PENDING"
    assert summary["_evidence"] == {
        "sequence": 3,
        "path": "first.forecast",
        "sha256": hashlib.sha256(canonical_record(original)).hexdigest(),
    }
    assert "condition" not in summary
    assert forecast_comparison(None, 1) is None


def test_compact_forecast_keeps_rule_version_and_fallback_without_rewriting_evidence():
    original = record()["first"]["forecast"]
    metadata = {
        "version": "structure-forecast-2",
        "guideline_fit": "STRUCTURAL_FALLBACK",
        "confirmation_basis_time": 60,
    }
    original.update(metadata)
    frozen = copy.deepcopy(original)
    summary = forecast_comparison(original, 3)
    assert "condition" not in summary
    assert all(summary[key] == value for key, value in metadata.items())
    assert summary["_evidence"]["sha256"] == hashlib.sha256(canonical_record(frozen)).hexdigest()
    assert original == frozen
