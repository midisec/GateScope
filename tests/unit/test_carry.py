from datetime import datetime,timezone
import math

import pytest
from gatescope.quant.carry import carry_backtest,STEP


def fixture(count=1000):
    start=int(datetime(2021,1,1,tzinfo=timezone.utc).timestamp()*1000)
    rows=[]
    for i in range(count):
        price=100+math.sin(i/20)
        rows.append([start+i*STEP,str(price),str(price+1),str(price-1),str(price+.1)])
    return {"spot":{"rows":rows},"perp":{"rows":[r[:] for r in rows]},"mark":{"rows":[r[:] for r in rows]},
        "funding":{"rows":[dict(fundingTime=r[0],fundingRate="0.0002",markPrice=r[1]) for r in rows]}},start


def test_carry_reconciles_all_costs_and_funding_without_directional_pnl():
    data,start=fixture()
    report=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)
    assert report["metrics"]["net_pnl"]==pytest.approx(sum(t["net_pnl"] for t in report["trades"]))
    assert report["metrics"]["fees"]>0
    assert report["metrics"]["funding"]<0
    assert not report["margin_failure"]
    assert not report["screening"]["deployable"]
    assert report["screening"]["validation_scope"]=="FULL_SAMPLE_RESEARCH"
    assert "尚无独立留出样本验证" in report["screening"]["basis"]
    first=report["trades"][0]
    assert first["funding_count"]==125  # Skip entry-boundary funding, unwind before next.
    assert first["net_pnl"] < first["funding_income"]-first["fees"]


def test_cost_stress_keeps_signal_schedule_and_cannot_improve_net_returns():
    data,start=fixture()
    a=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)
    b=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP,cost_multiplier=2)
    assert [t["entry_time"] for t in a["trades"]]==[t["entry_time"] for t in b["trades"]]
    assert b["metrics"]["total_return"]<a["metrics"]["total_return"]


def test_future_funding_does_not_change_past_nav_or_entry():
    data,start=fixture()
    a=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)
    for event in data["funding"]["rows"][700:]:event["fundingRate"]="-0.002"
    b=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)
    assert a["equity_curve"][:630]==b["equity_curve"][:630]


def test_missing_pair_bar_is_not_interpolated():
    data,start=fixture();data["perp"]["rows"].pop(500)
    with pytest.raises(ValueError,match="continuous"):
        carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)


def test_margin_exhaustion_blocks_even_an_offsetting_spot_gain():
    data,start=fixture();data["mark"]["rows"][200][2]="1000"
    result=carry_backtest(data,family="calendar",start=start+70*STEP,end=start+1000*STEP)
    assert result["margin_failure"]
    assert "COLLATERAL_EXHAUSTION" in result["screening"]["reason_codes"]
