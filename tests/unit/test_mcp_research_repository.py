from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from gatescope.adapters.postgres_mcp_research import McpResearchRepository, _page

TENANT, USER, RUN, INSTRUMENT = (UUID(int=value) for value in (1, 2, 3, 4))


class Result:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class Pool:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []
        self.contexts = []

    @contextmanager
    def read_transaction(self, tenant, user):
        self.contexts.append((tenant, user))
        yield self

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        assert statement.lstrip().startswith(("SELECT", "WITH", "SET LOCAL"))
        if statement.startswith("SET LOCAL"):
            return Result(None)
        return Result(self.results.pop(0))


def run(mode="INDICATOR", available=True):
    return {
        "run_id": str(RUN),
        "engine_mode": mode,
        "report_available": available,
        "state": "SUCCEEDED" if available else "RUNNING",
        "training": {"trade_count": 10},
        "validation": {"trade_count": 3},
        "range": {"embargo_bars": 48},
        "evidence": {"sealed_holdout": False},
        "cost_model": {"fee_bps": 5, "slippage_bps": 5, "funding_bps_per_8h": 1},
    }


def test_watchlist_is_explicitly_owned_read_only_and_json_ready():
    pool = Pool(
        [
            (1,),
            [
                (
                    {
                        "instrument_id": INSTRUMENT,
                        "default_timeframe": "1h",
                        "cache": {"cached_bars": 50},
                        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                        "price": Decimal("1.23"),
                    },
                )
            ],
        ]
    )
    result = McpResearchRepository(pool).watchlist(TENANT, USER)
    assert result["read_only"] and result["items"][0]["instrument_id"] == str(INSTRUMENT)
    assert result["items"][0]["price"] == "1.23" and result["items"][0]["created_at"].endswith("+00:00")
    assert pool.contexts == [(TENANT, USER)]
    for statement, parameters in pool.calls[2:]:
        assert "w.tenant_id=%s" in statement and "w.owner_user_id=%s" in statement
        assert "wi.tenant_id=%s" in statement and parameters[:3] == (TENANT, USER, TENANT)
        assert "request_" not in statement and "INSERT" not in statement


def test_backtest_list_keeps_costs_and_training_validation_without_full_report():
    pool = Pool([(3,), [(run(),)]])
    result = McpResearchRepository(pool).backtests(
        TENANT, USER, instrument=str(INSTRUMENT), timeframe="1h", limit=1, offset=1
    )
    assert result["has_more"] and result["next_offset"] == 2
    assert result["items"][0]["training"] == {"trade_count": 10}
    assert result["items"][0]["validation"] == {"trade_count": 3}
    assert result["items"][0]["cost_model"]["fee_bps"] == 5
    statement, params = pool.calls[-1]
    assert "SELECT j.report_json " not in statement
    assert "r.tenant_id=%s AND r.owner_user_id=%s" in statement
    assert params == (TENANT, USER, INSTRUMENT, INSTRUMENT, "1h", "1h", 1, 1)


@pytest.mark.parametrize(
    "section,path",
    [
        ("trades", ["full", "trades"]),
        ("equity", ["full", "equity_curve"]),
        ("events", ["pattern_events"]),
        ("inputs", ["input_bars"]),
    ],
)
def test_indicator_evidence_json_is_paged_in_sql(section, path):
    pool = Pool([(run(),), (5, [{"original": "unchanged", "available_at": 1000}])])
    result = McpResearchRepository(pool).backtest_detail(
        TENANT, USER, RUN, section=section, limit=1, offset=2
    )
    assert result["items"] == [{"original": "unchanged", "available_at": 1000}]
    assert result["next_offset"] == 3 and result["immutable"]
    statement, params = pool.calls[-1]
    assert "generate_series" in statement and "jsonb_array_length" in statement
    assert params[:7] == (path, path, RUN, TENANT, USER, TENANT, USER)
    assert params[-2:] == (2, 2)


@pytest.mark.parametrize("section", ["cases", "trades", "events", "inputs"])
def test_structure_table_pages_have_explicit_owner_even_for_admin_connections(section):
    pool = Pool([(run("STRUCTURE", False),), (2,), [({"saved": "evidence"},)]])
    result = McpResearchRepository(pool).backtest_detail(TENANT, USER, RUN, section=section, limit=1)
    assert result["partial_run"] and result["availability"] == "AVAILABLE"
    assert result["immutable"] == (section in {"events", "inputs"})
    for statement, params in pool.calls[-2:]:
        assert "run_id=%s AND tenant_id=%s AND owner_user_id=%s" in statement
        assert params[:3] == (RUN, TENANT, USER)
    assert result["portfolio_returns_additive"] is False


def test_validation_labels_actual_time_split_and_preserves_saved_metrics():
    pool = Pool([(run(),)])
    result = McpResearchRepository(pool).backtest_detail(TENANT, USER, RUN, section="validation")
    assert result["items"][0]["training"] == {"trade_count": 10}
    assert result["items"][0]["validation"] == {"trade_count": 3}
    assert result["items"][0]["evidence"]["sealed_holdout"] is False
    assert result["items"][0]["range"]["embargo_bars"] == 48


def test_no_fake_portfolio_curve_for_independent_structure_cases():
    pool = Pool([(run("STRUCTURE"),)])
    result = McpResearchRepository(pool).backtest_detail(TENANT, USER, RUN, section="equity")
    assert result["availability"] == "NOT_APPLICABLE" and result["items"] == []


def test_wrong_owner_or_missing_run_is_not_found_before_evidence_lookup():
    pool = Pool([None])
    with pytest.raises(LookupError, match="RESEARCH_NOT_FOUND"):
        McpResearchRepository(pool).backtest_detail(TENANT, USER, RUN, section="events")
    assert len(pool.calls) == 3
    assert pool.calls[-1][1] == (TENANT, USER, RUN)


@pytest.mark.parametrize(
    "arguments", [{"limit": True}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 1_000_001}]
)
def test_bad_page_parameters_do_not_touch_database(arguments):
    pool = Pool()
    with pytest.raises(ValueError, match="INVALID_RESEARCH_PAGINATION"):
        McpResearchRepository(pool).watchlist(TENANT, USER, **arguments)
    assert not pool.calls


def test_response_byte_budget_keeps_next_offset_exact_without_changing_evidence():
    items = [{"value": "a" * 260_000}, {"value": "b" * 260_000}]
    result = _page(items, 2, 2, 0)
    assert result["returned"] == 1 and result["next_offset"] == 1
    assert result["truncation_reason"] == "OUTPUT_BYTE_BUDGET" and result["items"][0] == items[0]
