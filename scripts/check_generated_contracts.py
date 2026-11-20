#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from scripts.check_openapi_compat import incompatibilities
from scripts.generate_openapi import generate
from scripts.generate_formal_openapi import generate as generate_v2


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    baseline_path = root / "contracts/openapi/v1.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = generate()
    errors = incompatibilities(baseline, candidate)
    if errors:
        raise SystemExit("\n".join(errors))
    if baseline != candidate:
        raise SystemExit("OpenAPI snapshot is stale; run scripts/generate_openapi.py")
    v2_path = root / "contracts/openapi/v2.json"
    v2_baseline = json.loads(v2_path.read_text(encoding="utf-8"))
    if v2_baseline != generate_v2():
        raise SystemExit("V2 OpenAPI snapshot is stale; run scripts/generate_formal_openapi.py")
    print("generated V1/V2 OpenAPI matches committed baselines")


if __name__ == "__main__":
    main()
