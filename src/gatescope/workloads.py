"""Phase-bound workload guards."""

from __future__ import annotations

import json


def unavailable(name: str) -> None:
    print(json.dumps({"workload": name, "status": "disabled", "reason": "not_available_in_phase0"}))
    raise SystemExit(78)

