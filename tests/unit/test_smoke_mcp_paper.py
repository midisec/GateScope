import importlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from gatescope.adapters.gate_paper_market import MarketSnapshot
from gatescope.domain.paper_matching import BookLevel, ContractSpec, OrderBook

ACCOUNT = str(UUID(int=1))
INSTRUMENT = str(UUID(int=2))
ORDER = str(UUID(int=3))
POSITION = str(UUID(int=4))
CLOSE_ORDER = str(UUID(int=5))
NAME = "MCP模拟验收-fixture"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("smoke_mcp_paper")


def quote():
    now = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)
    book = OrderBook("SOL_USDT", (BookLevel(Decimal("99.99"), Decimal("100")),),
                     (BookLevel(Decimal("100.01"), Decimal("100")),), now, now)
    return MarketSnapshot(ContractSpec("SOL_USDT", Decimal("1"), Decimal("0.01"), Decimal("0.1"),
                                      Decimal("1000"), Decimal("0.001")), book, None, None, now)


class Response:
    def __init__(self, result):
        self.result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self.result}


class FakeHttp:
    def __init__(self, module, *, uncertain_open=0, uncertain_close=0, existing=False, empty_preview=False):
        self.module = module
        self.calls, self.orders, self.fills = [], [], []
        self.position = None
        self.headers = {}
        self.uncertain_open, self.uncertain_close = uncertain_open, uncertain_close
        self.empty_preview = empty_preview
        if existing:
            self.orders = [{"id": "preexisting", "account_id": ACCOUNT}]

    def factory(self, **kwargs):
        self.headers = kwargs["headers"]
        assert kwargs["follow_redirects"] is False
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def post(self, _url, *, json):
        method = json["method"]
        if method == "initialize":
            return Response({"protocolVersion": "2025-03-26"})
        if method == "notifications/initialized":
            return Response({})
        if method == "tools/list":
            return Response({"tools": [{"name": name, "annotations": {"readOnlyHint": name in self.module.READ_TOOLS}}
                                       for name in self.module.READ_TOOLS | self.module.WRITE_TOOLS]})
        assert method == "tools/call"
        name, args = json["params"]["name"], json["params"]["arguments"]
        self.calls.append((name, dict(args)))
        value = {"environment": "PAPER"}
        if name == "paper_account_list":
            value["items"] = [{"id": ACCOUNT, "name": NAME, "status": "ACTIVE"}]
        elif name == "paper_account_snapshot":
            cash = Decimal("10000") + sum((Decimal(row["realized_pnl"])-Decimal(row["fee"]) for row in self.fills), Decimal(0))
            margin = "0" if not self.position or self.position["status"] == "CLOSED" else "100"
            value.update(account={"id": ACCOUNT}, balance={"cash_balance": str(cash), "position_margin": margin, "order_reserved_margin": "0"})
        elif name == "paper_order_list":
            value["items"] = self.orders
        elif name == "paper_position_list":
            value["items"] = [self.position] if self.position else []
        elif name == "paper_fill_list":
            value["items"] = self.fills
        elif name == "paper_order_preview":
            value.update(preview_only=True, order_created=False, estimate={"filled_contracts": 0 if self.empty_preview else 1})
        elif name == "paper_order_submit":
            assert args["contracts"] == 1 and args["confirm_paper"] is True and args["account_id"] == ACCOUNT
            replayed = bool(self.orders)
            if not self.orders:
                self.orders.append({"id": ORDER, "account_id": ACCOUNT, "effect": "OPEN", "filled_contracts": 1,
                                    "position_id": POSITION, "idempotency_key": args["idempotency_key"]})
                self.position = {"id": POSITION, "account_id": ACCOUNT, "opening_order_id": ORDER,
                                 "status": "OPEN", "remaining_contracts": 1}
                self.fills.append({"account_id": ACCOUNT, "effect": "OPEN", "contracts": 1, "realized_pnl": "0", "fee": "0.1"})
            if self.uncertain_open:
                self.uncertain_open -= 1
                raise RuntimeError("secret raw reply must never escape")
            value.update(order=self.orders[0], replayed=replayed, accepted=True)
        elif name == "paper_position_close":
            assert args["position_id"] == POSITION and args["confirm_paper"] is True
            if self.position["status"] == "OPEN":
                self.position.update(status="CLOSED", remaining_contracts=0)
                self.orders.append({"id": CLOSE_ORDER, "account_id": ACCOUNT, "effect": "CLOSE", "position_id": POSITION})
                self.fills.append({"account_id": ACCOUNT, "effect": "CLOSE", "contracts": 1, "realized_pnl": "1", "fee": "0.1"})
            if self.uncertain_close:
                self.uncertain_close -= 1
                raise RuntimeError("secret raw close reply must never escape")
            value.update(order=self.orders[1], accepted=True)
        else:
            raise AssertionError(name)
        return Response({"structuredContent": value, "content": []})


def execute(module, http):
    return module.run("http://127.0.0.1/mcp", "in-memory-only", ACCOUNT, execute_paper=True,
                      instrument_id=INSTRUMENT, snapshot=quote(), account_name=NAME,
                      run_id="fixture12345678", client_factory=http.factory)


def test_read_only_default_cannot_create_accounts_credentials_or_orders(module):
    http = FakeHttp(module)
    report = module.run("http://127.0.0.1/mcp", "in-memory-only", ACCOUNT, client_factory=http.factory)
    assert report["ok"] and report["orders_changed"] is False
    assert not ({name for name, _ in http.calls} & module.WRITE_TOOLS)
    assert "10000" not in json.dumps(report) and "in-memory-only" not in json.dumps(report)


def test_one_contract_open_same_key_replay_close_and_balance_verification(module):
    http = FakeHttp(module)
    report = execute(module, http)
    assert report["ok"] and report["closed"] and report["idempotency_same_order"]
    assert report["balance_reconciled_without_disclosure"]
    assert report["counts"] == {"orders": 2, "positions": 1, "fills": 2, "open_positions": 0}
    submits = [args for name, args in http.calls if name == "paper_order_submit"]
    assert len(submits) == 2 and submits[0] == submits[1]
    text = json.dumps(report)
    assert "10000" not in text and "in-memory-only" not in text and "secret" not in text


def test_lost_open_response_only_retries_identical_request_never_second_opening_intention(module):
    http = FakeHttp(module, uncertain_open=1)
    report = execute(module, http)
    assert report["ok"] and len(http.orders) == 2
    submits = [args for name, args in http.calls if name == "paper_order_submit"]
    assert all(args == submits[0] for args in submits)


def test_twice_lost_open_response_recovers_known_order_and_closes_only_its_position(module):
    http = FakeHttp(module, uncertain_open=2)
    report = execute(module, http)
    assert not report["ok"] and report["closed"]
    assert report["opening_order_id"] == ORDER and report["position_id"] == POSITION
    assert len([row for row in http.orders if row["effect"] == "OPEN"]) == 1
    assert "secret" not in json.dumps(report)


def test_lost_close_response_retries_same_reduce_only_key(module):
    http = FakeHttp(module, uncertain_close=1)
    report = execute(module, http)
    assert report["ok"]
    closes = [args for name, args in http.calls if name == "paper_position_close"]
    assert len(closes) == 2 and closes[0] == closes[1]


def test_existing_account_facts_block_writes_even_with_matching_smoke_name(module):
    http = FakeHttp(module, existing=True)
    with pytest.raises(AssertionError):
        execute(module, http)
    assert not ({name for name, _ in http.calls} & module.WRITE_TOOLS)


def test_no_executable_preview_does_not_submit_or_fake_success(module):
    http = FakeHttp(module, empty_preview=True)
    report = execute(module, http)
    assert not report["ok"] and report["orders_changed"] is False
    assert not ({name for name, _ in http.calls} & module.WRITE_TOOLS)


@pytest.mark.parametrize("url", ["http://example.com/mcp", "https://u:p@example.com/mcp", "https://example.com/mcp?token=x", "https://example.com/other"])
def test_invalid_origin_never_receives_credential(module, url):
    with pytest.raises(ValueError):
        module.run(url, "in-memory-only", ACCOUNT)


def test_write_mode_requires_dedicated_named_account_and_explicit_quote(module):
    with pytest.raises(ValueError):
        module.run("http://127.0.0.1/mcp", "in-memory-only", ACCOUNT, execute_paper=True)
