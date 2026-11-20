from __future__ import annotations

import json
from pathlib import Path

from gate_client import GateClient


def test_gate_catalog_contract_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "contracts/external/gate/catalog.fixture.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    client = GateClient("")
    client.get_json = lambda endpoint, params=None: fixture["responses"][endpoint]  # type: ignore[method-assign]
    actual = client.fetch_catalog(include_btc_futures=True)
    projected = [
        {key: row[key] for key in ("id", "market", "symbol", "status")}
        for row in actual
    ]
    assert projected == fixture["expected"]

