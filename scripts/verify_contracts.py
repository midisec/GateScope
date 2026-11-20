#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, validate


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "contracts"
    files = sorted(root.rglob("*.json"))
    if not files:
        raise SystemExit("no contracts found")
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if path.name.endswith("schema.json"):
            Draft202012Validator.check_schema(payload)
    manifest_schema = json.loads((root / "mcp/manifest.v1.schema.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "mcp/manifest.v1.json").read_text(encoding="utf-8"))
    validate(manifest, manifest_schema)
    print(f"validated {len(files)} JSON contracts")


if __name__ == "__main__":
    main()
