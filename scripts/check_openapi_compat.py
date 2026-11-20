#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def resolve_schema(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/components/schemas/"):
        return schema
    name = reference.rsplit("/", 1)[-1]
    return document.get("components", {}).get("schemas", {}).get(name, {})


def required_request_fields(document: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    content = operation.get("requestBody", {}).get("content", {})
    media = content.get("application/json") or next(iter(content.values()), {})
    schema = resolve_schema(document, media.get("schema", {}))
    return {str(name) for name in schema.get("required", [])}


def incompatibilities(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    old_paths = baseline.get("paths", {})
    new_paths = candidate.get("paths", {})
    for path, old_item in old_paths.items():
        if path not in new_paths:
            errors.append(f"removed path: {path}")
            continue
        for method, old_operation in old_item.items():
            if method not in METHODS:
                continue
            new_operation = new_paths[path].get(method)
            if new_operation is None:
                errors.append(f"removed method: {method.upper()} {path}")
                continue
            old_responses = set(old_operation.get("responses", {}))
            new_responses = set(new_operation.get("responses", {}))
            for status in sorted(old_responses - new_responses):
                errors.append(f"removed response {status}: {method.upper()} {path}")
            old_required = required_request_fields(baseline, old_operation)
            new_required = required_request_fields(candidate, new_operation)
            for field in sorted(new_required - old_required):
                errors.append(f"new required request field {field}: {method.upper()} {path}")
            old_parameters = {
                (str(item.get("in")), str(item.get("name")))
                for item in old_operation.get("parameters", [])
                if item.get("required")
            }
            new_parameters = {
                (str(item.get("in")), str(item.get("name")))
                for item in new_operation.get("parameters", [])
                if item.get("required")
            }
            for location, name in sorted(new_parameters - old_parameters):
                errors.append(f"new required {location} parameter {name}: {method.upper()} {path}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args()
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    errors = incompatibilities(baseline, candidate)
    if errors:
        raise SystemExit("\n".join(errors))
    print("OpenAPI compatibility check passed")


if __name__ == "__main__":
    main()
