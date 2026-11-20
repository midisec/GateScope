from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from apps.mcp_server.main import SERVER_INSTRUCTIONS, _transport_security, _validated_public_url, create_mcp_server
from gatescope.adapters.postgres_mcp import VerifiedMcpToken, _scopes
from gatescope.application.mcp_tools import McpAnalysisService, McpToolDenied


class FakeMarket:
    instrument = {
        "id": "30000000-0000-0000-0000-000000000033",
        "market": "futures_usdt",
        "settle": "usdt",
        "symbol": "BTC_USDT",
        "display_name": "BTC_USDT",
        "base": "BTC",
        "quote": "USDT",
        "status": "ACTIVE",
        "is_stock": False,
    }

    def search_instruments(self, query: str, market: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return [self.instrument] if "BTC".startswith(query.upper()) else []

    def get_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        return self.instrument if instrument_id == self.instrument["id"] else None

    def get_canonical_candles(
        self,
        instrument_id: str,
        timeframe: str,
        start_ts: int | None,
        end_ts: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        base = 1_700_000_000
        return [
            {
                "ts": base + index * 900,
                "open": 30_000 + index,
                "high": 30_010 + index,
                "low": 29_990 + index,
                "close": 30_005 + index,
                "volume": 100 + index,
                "updated_at": base + index * 900 + 900,
            }
            for index in range(min(limit, 80))
        ]


class FakeAlerts:
    def list(self, tenant_id: UUID, owner_id: UUID, *, limit: int = 100) -> list[Any]:
        return []


class FakeAccess:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []

    def audit_invocation(self, principal: VerifiedMcpToken, **payload: Any) -> None:
        self.audits.append(payload)


class FakePaper:
    signal_id = UUID("40000000-0000-0000-0000-000000000044")

    def list_signals(self, tenant_id: UUID, owner_id: UUID, *, limit: int) -> list[Any]:
        return [self.signal()]

    def get_signal(self, tenant_id: UUID, owner_id: UUID, signal_id: UUID) -> Any:
        return self.signal()

    def signal_gates(self, tenant_id: UUID, owner_id: UUID, signal_id: UUID) -> list[dict[str, Any]]:
        return [{"gate_code": "EXPECTED_RETURN_LCB_POSITIVE", "result": "PASS"}]

    def signal_chart(self, tenant_id: UUID, owner_id: UUID, signal_id: UUID) -> dict[str, Any]:
        return {"bars": [], "quality_flags": [], "indicator_snapshot": {}}

    def get_trade(self, tenant_id: UUID, owner_id: UUID, public_trade_id: str) -> Any:
        return SimpleNamespace(
            public_trade_id=public_trade_id,
            signal_evidence_snapshot_id=self.signal_id,
            symbol="BTC_USDT",
            timeframe="15m",
            environment="PAPER",
            side="LONG",
            exact_terms={"stop_price": "95", "take_profit_price": "110"},
            exact_terms_digest="b" * 64,
            state="OPEN",
            close_reason=None,
            entry_fill_price=Decimal("100"),
            exit_fill_price=None,
            quantity=Decimal("1"),
            fees=Decimal("0.05"),
            net_pnl=None,
            evaluated_bars=2,
            protection_status="SIMULATED_ACTIVE",
            last_evaluated_available_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )

    def trade_timeline(
        self, tenant_id: UUID, owner_id: UUID, public_trade_id: str,
    ) -> list[dict[str, Any]]:
        return [{"sequence": 1, "event_type": "TRADE_CREATED", "event_digest": "c" * 64}]

    def signal(self) -> Any:
        timestamp = datetime(2026, 9, 7, tzinfo=timezone.utc)
        return SimpleNamespace(
            id=self.signal_id,
            instrument_id=UUID(FakeMarket.instrument["id"]),
            symbol="BTC_USDT",
            timeframe="15m",
            strategy_version_ref="fixture-v1",
            policy_version="paper-v1",
            evidence_tier="PAPER",
            eligibility_status="PAPER_ELIGIBLE",
            side="LONG",
            entry_reference=Decimal("100"),
            stop_price=Decimal("95"),
            take_profit_price=Decimal("110"),
            time_stop_bars=10,
            metrics={"win_probability_lcb": 0.61},
            assumptions=("sealed",),
            reason_codes=(),
            indicator_snapshot={},
            evidence_digest="a" * 64,
            decision_at=timestamp,
            data_watermark=timestamp,
        )


def principal(*scopes: str) -> VerifiedMcpToken:
    return VerifiedMcpToken(
        id=UUID("10000000-0000-0000-0000-000000000001"),
        tenant_id=UUID("10000000-0000-0000-0000-000000000002"),
        subject_user_id=UUID("10000000-0000-0000-0000-000000000003"),
        client_id=UUID("10000000-0000-0000-0000-000000000004"),
        scopes=tuple(scopes),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def service() -> tuple[McpAnalysisService, FakeAccess]:
    access = FakeAccess()
    instance = McpAnalysisService(FakeMarket(), FakeAlerts(), access)  # type: ignore[arg-type]
    return instance, access


def paper_service() -> tuple[McpAnalysisService, FakeAccess]:
    access = FakeAccess()
    instance = McpAnalysisService(
        FakeMarket(), FakeAlerts(), access, FakePaper(),  # type: ignore[arg-type]
    )
    return instance, access


def test_mcp_tools_are_bounded_read_only_and_audited() -> None:
    instance, access = service()
    result = instance.search_instruments(
        principal("mcp:connect", "market:read"), query="BT", limit=5,
    )
    assert result["research_only"] is True
    assert result["items"][0]["symbol"] == "BTC_USDT"
    assert "guarantee" in result["usage_notice"]
    assert access.audits[-1]["outcome"] == "SUCCESS"

    candles = instance.get_candles(
        principal("mcp:connect", "market:read"),
        instrument_id=FakeMarket.instrument["id"],
        limit=10,
    )
    assert len(candles["items"]) == 10
    assert all(item["live_eligible"] is False for item in candles["items"])


def test_mcp_scope_denial_is_audited() -> None:
    instance, access = service()
    with pytest.raises(McpToolDenied, match="analysis:read"):
        instance.indicator_snapshot(
            principal("mcp:connect", "market:read"),
            instrument_id=FakeMarket.instrument["id"],
        )
    assert access.audits[-1]["outcome"] == "DENIED"
    assert access.audits[-1]["safe_error_code"] == "INSUFFICIENT_SCOPE"


def test_mcp_exposes_frozen_signal_and_persisted_paper_trade_reads() -> None:
    instance, access = paper_service()
    identity = principal("mcp:connect", "analysis:read")
    signals = instance.list_signals(identity, limit=5)
    assert signals["items"][0]["evidence_tier"] == "PAPER"
    explained = instance.explain_signal(identity, signal_id=str(FakePaper.signal_id))
    assert explained["items"][0]["history_policy"].startswith("exact frozen")
    trade = instance.get_trade_status(
        identity, public_trade_id="paper_00000000000000000000000000",
    )
    assert trade["items"][0]["simulation"] is True
    assert trade["items"][0]["live_eligible"] is False
    timeline = instance.get_trade_timeline(
        identity, public_trade_id="paper_00000000000000000000000000",
    )
    assert timeline["immutable"] is True
    assert [event["tool_name"] for event in access.audits[-4:]] == [
        "signal_list", "signal_explain", "trade_get_status", "trade_get_timeline",
    ]


def test_indicator_snapshot_uses_only_final_historical_bars() -> None:
    instance, access = service()
    result = instance.indicator_snapshot(
        principal("mcp:connect", "analysis:read"),
        instrument_id=FakeMarket.instrument["id"],
        indicator_ids=["rsi", "macd", "atr"],
        bar_limit=80,
    )
    assert result["bars_evaluated"] == 80
    assert {item["indicator_id"] for item in result["items"]} == {"rsi", "macd", "atr"}
    assert all(item["ready"] for item in result["items"])
    assert access.audits[-1]["tool_name"] == "indicator_get_snapshot"


def test_mcp_token_policy_and_public_url_fail_closed() -> None:
    assert _scopes(["market:read", "mcp:connect", "market:read"]) == (
        "market:read",
        "mcp:connect",
    )
    with pytest.raises(ValueError, match="mcp:connect"):
        _scopes(["market:read"])
    with pytest.raises(RuntimeError, match="HTTPS"):
        _validated_public_url("http://example.com/mcp")
    assert _validated_public_url("https://example.com/mcp").path == "/mcp"


def test_mcp_manifested_server_has_only_read_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATESCOPE_DATABASE_URL", "postgresql://unused:unused@127.0.0.1/unused")
    monkeypatch.setenv("GATESCOPE_MCP_PUBLIC_URL", "http://127.0.0.1:8780/mcp")
    server = create_mcp_server()
    tools = __import__("anyio").run(server.list_tools)
    names = {tool.name for tool in tools}
    from apps.mcp_server.paper import PAPER_READ_TOOLS, PAPER_WRITE_TOOLS
    assert names == set(PAPER_READ_TOOLS) | set(PAPER_WRITE_TOOLS) | {
        "market_search_instruments",
        "market_get_candles",
        "indicator_get_snapshot",
        "market_analysis_snapshot",
        "coinglass_get_snapshot",
        "alert_rule_list",
        "signal_list",
        "signal_explain",
        "trade_get_status",
        "trade_get_timeline",
        "structure_current",
        "structure_study",
        "structure_event_list",
        "structure_event_detail",
        "watchlist_list",
        "research_backtest_list",
        "research_backtest_detail",
        "indicator_get_series",
        "market_research_bundle",
        "market_chart_snapshot",
    }
    assert all(tool.annotations and tool.annotations.read_only_hint == (tool.name not in PAPER_WRITE_TOOLS) for tool in tools)
    assert all(tool.annotations and tool.annotations.destructive_hint == (tool.name in PAPER_WRITE_TOOLS) for tool in tools)
    assert "cannot place" in SERVER_INSTRUCTIONS
    assert "supplied on each call" in SERVER_INSTRUCTIONS
    assert "preview does not bind" in SERVER_INSTRUCTIONS
    for tool in tools:
        if tool.name not in {"market_analysis_snapshot", "coinglass_get_snapshot"}:
            continue
        properties = tool.input_schema["properties"]
        assert {"instrument_id", "symbol", "market", "timeframe"} <= properties.keys()
        assert {"type": "string"} in properties["symbol"]["anyOf"]
        assert {"type": "string"} in properties["market"]["anyOf"]
        assert properties["timeframe"]["type"] == "string"
        assert properties["timeframe"]["default"] == "15m"
        assert "const" not in properties["symbol"]
        assert "const" not in properties["timeframe"]
        assert "on each call" in tool.description


def test_mcp_public_proxy_host_is_allowed_without_disabling_rebinding_protection() -> None:
    from mcp.server.transport_security import TransportSecurityMiddleware
    settings = _transport_security(_validated_public_url("https://43.108.44.74/mcp"), "127.0.0.1", 17800)
    middleware = TransportSecurityMiddleware(settings)
    assert settings.enable_dns_rebinding_protection is True
    assert all("*" not in value for value in settings.allowed_hosts + settings.allowed_origins)
    assert middleware._validate_host("43.108.44.74")
    assert middleware._validate_host("127.0.0.1:17800")
    assert not middleware._validate_host("43.108.44.74:5555")
    assert not middleware._validate_host("43.108.44.74.attacker.example")
    assert not middleware._validate_host("127.0.0.1:12345")
    assert middleware._validate_origin(None)
    assert middleware._validate_origin("https://43.108.44.74")
    assert not middleware._validate_origin("https://attacker.example")
    assert not middleware._validate_origin("null")


@pytest.mark.parametrize("value", ["https://user:pass@example.com/mcp", "https://*.example.com/mcp", "https://example.com:bad/mcp", "https://example.com/mcp?key=x", "https://example.com/mcp\n"])
def test_mcp_public_url_does_not_accept_credentials_or_broad_host_patterns(value) -> None:
    with pytest.raises(RuntimeError):
        _validated_public_url(value)
