from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    path = tmp_path_factory.mktemp("v2-api") / "api.db"
    env = {
        # This legacy fixture always targets its temporary SQLite database,
        # regardless of formal-worker environment left by other test modules.
        "GATESCOPE_DATABASE_URL": "",
        "GATESCOPE_DB_PATH": str(path),
        "GATESCOPE_BOOTSTRAP_USERNAME": "owner",
        "GATESCOPE_BOOTSTRAP_PASSWORD": "correct-horse-battery-staple",
        "GATESCOPE_PUBLIC_ORIGIN": "http://testserver",
        "GATESCOPE_COOKIE_SECURE": "false",
        "GATESCOPE_START_WORKERS": "false",
        "LIVE_TRADING_ENABLED": "false",
        "GATESCOPE_EXECUTION_ENVIRONMENT": "PAPER",
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    module.DB.upsert_instruments([
        {
            "id": "futures:usdt:BTC_USDT", "market": "futures", "settle": "usdt",
            "symbol": "BTC_USDT", "display_name": "BTC", "base": "BTC", "quote": "USDT",
            "status": "tradable",
        },
        {
            "id": "futures:usdt:ETH_USDT", "market": "futures", "settle": "usdt",
            "symbol": "ETH_USDT", "display_name": "ETH", "base": "ETH", "quote": "USDT",
            "status": "tradable",
        },
    ])
    module.DB.upsert_candles(
        "futures:usdt:BTC_USDT",
        "1m",
        [
            (1_767_225_600, 100, 101, 99, 100, 10),
            (1_767_225_660, 101, 102, 100, 101, 20),
            (1_767_225_720, 102, 103, 101, 102, 30),
        ],
    )
    with TestClient(module.app, base_url="http://testserver") as test_client:
        response = test_client.post(
            "/auth/login",
            json={"username": "owner", "password": "correct-horse-battery-staple"},
        )
        assert response.status_code == 200
        yield test_client
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_strategy_catalog_uses_current_model_and_confirmed_wave_direction(client: TestClient) -> None:
    from gatescope.quant.structure_detection import VERSION

    result = client.get('/api/v2/patterns/catalog')
    assert result.status_code == 200
    payload = result.json()
    assert payload['detector_version'] == VERSION
    patterns = {item['pattern_id']: item for item in payload['items']}
    assert {'bull_breakout_retest','bear_breakout_retest'}.issubset(patterns)
    assert patterns['elliott_impulse_bull']['direction'] == 'bearish'
    assert patterns['elliott_impulse_bull']['structural_direction'] == 'bullish'
    assert patterns['elliott_impulse_bear']['direction'] == 'bullish'


def test_cursor_pagination_is_opaque_and_bound_to_filters(client: TestClient) -> None:
    first = client.get("/api/v2/instruments", params={"market": "futures:usdt", "limit": 1})
    assert first.status_code == 200
    payload = first.json()
    assert len(payload["items"]) == 1
    assert payload["has_more"]
    assert payload["next_cursor"] and "." in payload["next_cursor"]

    second = client.get(
        "/api/v2/instruments",
        params={"market": "futures:usdt", "limit": 1, "cursor": payload["next_cursor"]},
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] != payload["items"][0]["id"]
    rebound = client.get(
        "/api/v2/instruments",
        params={"market": "all", "limit": 1, "cursor": payload["next_cursor"]},
    )
    assert rebound.status_code == 422


def test_bar_page_carries_temporal_and_quality_contract(client: TestClient) -> None:
    response = client.get(
        "/api/v2/bars",
        params={"instrument_id": "futures:usdt:BTC_USDT", "timeframe": "1m", "limit": 2},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_more"]
    assert payload["data_watermark"] == payload["items"][-1]["available_at"]
    assert payload["quality_flags"] == ["AVAILABLE_AT_UNKNOWN", "LEGACY_FINALITY_ASSUMED", "LEGACY_SOURCE"]
    assert payload["items"][0]["is_final"]
    assert payload["items"][0]["revision"] == 1
    assert payload["items"][0]["live_eligible"] is False
    assert len(payload["items"][0]["payload_hash"]) == 64


def test_indicator_preview_uses_versioned_batch_engine(client: TestClient) -> None:
    csrf = client.cookies.get("gatescope_csrf")
    response = client.post(
        "/api/v2/indicators/preview",
        headers={"X-CSRF-Token": csrf},
        json={
            "instrument_id": "futures:usdt:BTC_USDT", "timeframe": "1m", "limit": 3,
            "nodes": [{"instance_id": "trend", "indicator_id": "sma", "params": {"period": 2}}],
        },
    )
    assert response.status_code == 200
    points = response.json()["items"]
    assert len(points) == 3
    assert points[0]["ready"] is False
    assert points[-1]["values"]["sma"] == pytest.approx(101.5)
    assert points[-1]["indicator_version"] == "1.0.0"


def test_websocket_detects_cursor_gap_and_sequences_per_stream(client: TestClient) -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/openapi/ws-v2.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    with client.websocket_connect("/ws/v2") as websocket:
        request = {
            "request_id": "r1", "action": "subscribe",
            "topic": "market.bar.delta:btc-usdt:1m", "cursor": "stale",
        }
        assert not list(validator.iter_errors(request))
        websocket.send_json(request)
        resync = websocket.receive_json()
        assert resync["type"] == "resync_required"
        assert resync["sequence"] == 1
        assert not list(validator.iter_errors(resync))
        websocket.send_json({
            "request_id": "r2", "action": "subscribe", "topic": request["topic"],
            "cursor": resync["payload"]["watermark"],
        })
        subscribed = websocket.receive_json()
        assert subscribed["type"] == "subscribed"
        assert subscribed["sequence"] == 2
        assert not list(validator.iter_errors(subscribed))
