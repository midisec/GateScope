"""Canonical research JSON hashing stable across PostgreSQL JSONB round trips."""
import hashlib
import json
import math
from decimal import Decimal
from typing import Any

DIGEST_METHOD = "json-numeric-canonical-v1"


def canonical_json(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=True) + ":" + canonical_json(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("artifact numbers must be finite")
        # No Decimal.normalize(): its default precision could round large ints.
        number = format(Decimal(str(value)), "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return "0" if number in ("-0", "0") else number
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def report_digest(report: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({k:v for k,v in report.items() if k != "artifact_digest"}).encode()).hexdigest()
