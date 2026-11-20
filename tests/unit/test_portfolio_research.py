from datetime import datetime,timezone

import pytest

from gatescope.quant.portfolio_research import PortfolioPolicy,run_portfolio

START=int(datetime(2024,1,1,tzinfo=timezone.utc).timestamp())


def candidate(name="a",symbol="BTC",*,entry=60,exit=120,price=110,state="TARGET",selected=True):
    return {"source_run_id":name,"candidate_id":name,"instrument_id":symbol,"symbol":symbol,"timeframe":"1m",
        "pattern":"double_bottom","observed_time":START+1,"selected":selected,"state":state,
        "entry_time":START+entry if entry is not None else None,"exit_time":START+exit if exit is not None else None,
        "entry_price":100.,"raw_entry_price":100.,"exit_price":price,"side":1,"stop_price":95.,
        "fee_bps":10.,"funding_bps_per_8h":0.,"step_seconds":60,"available_at":START+1,"eligible_at":START+1,
        "outcome_time":START+120,"net_return":.0979,"ambiguous":False}


def simulate(cases,policy=None,*,end=300,bar_values=None):
    data={}
    def sequence(name):
        data[name]=[]
        return data[name]
    bars=bar_values if bar_values is not None else sorted([{"source_run_id":case["source_run_id"],"mark_time":START+t,"close":100.}
        for case in cases for t in range(60,end+1,60)],key=lambda row:row["mark_time"])
    report=run_portfolio(cases,bars,policy or PortfolioPolicy(initial_equity=1000),start=START,end=START+end,sequence_factory=sequence)
    return report,data


def test_finite_cash_costs_and_release_only_after_exit():
    first=candidate()
    competing=candidate("b","SOL",price=10000)
    later=candidate("c","ETH",entry=180,exit=240)
    report,data=simulate([first,competing,later],PortfolioPolicy(initial_equity=1000,max_positions=1))
    assert report["accepted_count"]==2
    assert report["rejected_count"]==1
    assert report["rejection_reasons"]=={"MAX_CONCURRENT_POSITIONS":1}
    expected_notional=10/(.05+.001*(1+.95))
    expected_profit=expected_notional*(.1-.001*(1+1.1))
    assert data["trades"][0]["notional"]==pytest.approx(expected_notional)
    assert data["trades"][0]["net_pnl"]==pytest.approx(expected_profit)
    assert data["trades"][1]["notional"]==pytest.approx((1000+expected_profit)*.01/(.05+.001*(1+.95)))
    assert report["final_cash"]==pytest.approx(1000+sum(trade["net_pnl"] for trade in data["trades"]))
    assert report["final_reserved"]==0
    assert all(point["cash"]>=0 and point["positions"]<=1 for point in data["curve"])


def test_source_future_profit_cannot_change_admission_before_realized_exit():
    positive=candidate(price=10000)
    negative=candidate(price=1,state="STOP")
    competing=candidate("b","SOL",entry=60,exit=180)
    _,rich=simulate([positive,competing],PortfolioPolicy(initial_equity=1000,max_positions=1))
    _,poor=simulate([negative,competing],PortfolioPolicy(initial_equity=1000,max_positions=1))
    assert rich["decisions"]==poor["decisions"]
    assert rich["trades"][0]["notional"]==poor["trades"][0]["notional"]


def test_no_hedge_and_duplicate_signal_is_not_counted_twice():
    first=candidate()
    duplicate={**first,"source_run_id":"copy"}
    same_symbol=candidate("other","BTC")
    report,_=simulate([first,duplicate,same_symbol])
    assert report["rejection_reasons"]=={"DUPLICATE_SIGNAL":1,"SYMBOL_ALREADY_OPEN_NO_HEDGE":1}
    assert report["source_case_count"]==3 and report["accepted_count"]==1


def test_failed_unfilled_and_censored_cases_are_kept_and_never_fake_closed():
    open_case=candidate(state="CENSORED",exit=None)
    missed=candidate("m","SOL",entry=None,exit=None,state="MISSED")
    excluded=candidate("x","ETH",selected=False)
    report,data=simulate([open_case,missed,excluded])
    assert report["open_position_count"]==1
    assert len(data["trades"])==0
    assert report["open_positions"][0]["net_pnl"] is None
    assert report["rejection_reasons"]=={"SOURCE_MISSED":1,"SOURCE_RULE_EXCLUDED":1}
    assert len(data["decisions"])==3
    assert report["final_reserved"]>0


def test_stale_marks_null_account_metrics_and_block_new_risk():
    open_case=candidate(state="CENSORED",exit=None)
    late=candidate("late","SOL",entry=240,exit=300)
    report,data=simulate([open_case,late],bar_values=[{"source_run_id":"a","mark_time":START+60,"close":100}])
    assert report["metrics_available"] is False
    assert report["metrics"]["max_drawdown"] is None
    assert report["metrics"]["total_return"] is None
    assert report["rejection_reasons"]["STALE_MARK_NO_NEW_RISK"]==1
    assert data["curve"][-1]["equity"] is None


def test_nominal_exposure_and_fees_cannot_overdraw_cash():
    rows=[{**candidate(name,str(index),exit=240),"stop_price":99.99} for index,name in enumerate(("a","b","c"))]
    report,data=simulate(rows,PortfolioPolicy(initial_equity=1000,risk_fraction=.03,per_position_fraction=1))
    for point in data["curve"]:
        assert point["cash"]>=0
        assert point["reserved"]<=point["equity"]+1e-7
    assert report["accepted_count"]==1


def test_policy_bounds_do_not_enable_leverage_or_arbitrary_risk():
    with pytest.raises(ValueError):
        PortfolioPolicy(max_exposure_fraction=2)
    with pytest.raises(ValueError):
        PortfolioPolicy(max_exposure_fraction=.1,per_position_fraction=.2)
    with pytest.raises(ValueError):
        PortfolioPolicy(risk_fraction=.99)


def test_common_window_does_not_disclose_later_terminal_state_or_later_fill():
    future_failure={**candidate("failure","BTC",entry=None,exit=None,state="MISSED"),"outcome_time":START+900}
    future_entry=candidate("future","SOL",entry=600,exit=900)
    report,data=simulate([future_failure,future_entry],end=300)
    assert report["rejection_reasons"]=={"WINDOW_END_CENSORED":2}
    assert all(decision["decision_time"]==datetime.fromtimestamp(START+300,timezone.utc).isoformat() for decision in data["decisions"])
    assert all(decision["reason"]!="SOURCE_MISSED" for decision in data["decisions"])
    assert report["accepted_count"]==0 and report["open_position_count"]==0


def test_known_stop_fees_and_slippage_are_inside_the_size_budget():
    source={**candidate(price=94.905,state="STOP"),"slippage_bps":10.}
    report,data=simulate([source])
    assert data["trades"][0]["net_pnl"]==pytest.approx(-10)
    assert data["decisions"][0]["risk_budget"]==pytest.approx(10)
    assert report["final_cash"]==pytest.approx(990)


def test_delayed_old_close_does_not_renew_mark_freshness():
    open_case=candidate(state="CENSORED",exit=None)
    later=candidate("later","SOL",entry=600,exit=660)
    report,_=simulate([open_case,later],end=720,bar_values=[
        {"source_run_id":"a","mark_time":START+60,"close_time":START+60,"close":100.},
        {"source_run_id":"a","mark_time":START+600,"close_time":START+120,"close":100.}])
    assert report["rejection_reasons"]["STALE_MARK_NO_NEW_RISK"]==1
    assert report["metrics_available"] is False


def test_exit_cash_is_reused_only_when_recorded_exit_is_available():
    delayed={**candidate(),"exit_available_at":START+240}
    competing=candidate("b","SOL",entry=180,exit=300)
    report,data=simulate([delayed,competing],PortfolioPolicy(initial_equity=1000,max_positions=1))
    assert report["accepted_count"]==1
    assert report["rejection_reasons"]["MAX_CONCURRENT_POSITIONS"]==1
    assert data["trades"][0]["exit_time"]==datetime.fromtimestamp(START+120,timezone.utc).isoformat()
    assert data["trades"][0]["settled_at"]==datetime.fromtimestamp(START+240,timezone.utc).isoformat()
