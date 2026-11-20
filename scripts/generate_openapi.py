#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def generate() -> dict:
    os.environ.setdefault("GATESCOPE_START_WORKERS", "false")
    os.environ.setdefault("LIVE_TRADING_ENABLED", "false")
    os.environ.setdefault("GATESCOPE_EXECUTION_ENVIRONMENT", "PAPER")
    if "GATESCOPE_DB_PATH" not in os.environ:
        temp_root = Path(tempfile.mkdtemp(prefix="gatescope-openapi-"))
        os.environ["GATESCOPE_DB_PATH"] = str(temp_root / "contract.db")
    from app import app

    contract = app.openapi()
    contract.setdefault("components", {}).setdefault("securitySchemes", {})["SessionCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": "gatescope_session",
    }
    for path, methods in contract.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path not in {"/health/live", "/health/ready", "/login", "/auth/login"}:
                operation["security"] = [{"SessionCookie": []}]
            if method.lower() in {"post", "put", "patch", "delete"} and path != "/auth/login":
                operation["x-csrf-header"] = "X-CSRF-Token"
    return contract


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="contracts/openapi/v1.json")
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(generate(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

