import pytest
from gatescope.quant.artifacts import canonical_json, report_digest


def test_numeric_canonicalization_survives_jsonb_semantics():
    original = {"zero":-0.0,"equity":10000.0,"big":1e20,"small":1e-8,"string":"1","flag":True}
    round_trip = {"zero":0.0,"equity":10000,"big":100000000000000000000,"small":.00000001,"string":"1","flag":True}
    assert report_digest(original)==report_digest(round_trip)
    assert canonical_json(123456789012345678901234567890123456789) == "123456789012345678901234567890123456789"
    assert report_digest({"v":1})!=report_digest({"v":"1"})
    assert report_digest({"v":1})!=report_digest({"v":True})
    assert report_digest({"v":1})!=report_digest({"v":1.01})
    with pytest.raises(ValueError):
        canonical_json(float("inf"))
