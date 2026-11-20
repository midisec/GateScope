from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from gatescope.domain.market import Bar
from gatescope.quant.directional import Rule, append_pivot, geometry, rules, signal_book, simulate_directional
from tests.unit.test_research_backtest import make_bars


def test_fixed_grid_is_directional_and_bounded():
    assert len(rules()) == 144
    assert len(set(rules())) == 144
    assert all(r.direction in ("LONG", "BOTH") for r in rules())


def test_features_and_structures_do_not_repaint_past():
    bars = make_bars(500)
    full_points,full = signal_book(bars)
    points,prefix = signal_book(bars[:350])
    assert full_points[:350] == points
    assert all(full[f][:350] == prefix[f] for f in prefix)
    for events in full.values():
        for i,e in enumerate(events):
            if e:
                assert all(p["known_index"] <= i and p["index"]+3 == p["known_index"] for p in e["anchors"])


def test_fractal_requires_three_right_bars_and_never_earlier():
    bars = make_bars(10)
    b = bars[4]
    bars[4] = Bar(b.venue,b.market,b.instrument_id,b.timeframe,b.open_time,b.close_time,
                  b.open,Decimal(120),b.low,b.close,b.volume,True,1,b.available_at)
    pivots = []
    for i in range(7):
        append_pivot(pivots,bars,i)
    assert not any(p["index"]==4 for p in pivots)
    append_pivot(pivots,bars,7)
    assert pivots[-1] == dict(index=4,kind=1,price=120.,known_index=7)


def test_wave_three_is_candidate_and_uses_fixed_observed_stop_and_target():
    pivots = [dict(index=i,kind=k,price=p,known_index=i+3) for i,k,p in ((0,-1,100),(10,1,120),(20,-1,110))]
    event = next(e for e in geometry(pivots,112,2,23) if e["family"]=="wave3")
    assert event["stop"] == 109.5
    assert event["projected_target"] == pytest.approx(142.36)
    assert len(event["anchors"]) == 3  # No invented future waves 3/4/5.
    frozen = deepcopy(event)
    pivots[-1]["price"] = 99
    assert event == frozen
    assert not any(e["family"]=="wave3" for e in geometry(pivots,112,2,23))


def test_wave_five_rejects_wave_four_overlap():
    pivots = [dict(index=i,kind=k,price=p,known_index=i+3) for i,k,p in ((0,-1,100),(10,1,120),(20,-1,110),(30,1,145),(40,-1,130))]
    assert any(e["family"]=="wave5" for e in geometry(pivots,132,2,43))
    pivots[-1]["price"] = 119
    assert not any(e["family"]=="wave5" for e in geometry(pivots,132,2,43))


def simulation_input():
    bars = make_bars(12)
    points = [dict(values={"atr.atr":1.,"fast.ema":101.,"slow.ema":100.}) for _ in bars]
    signal = dict(side=1,stop=98.,projected_target=106.,family="wave3",known_index=0,anchors=[],evidence_id="test")
    signals = [signal]+[None]*(len(bars)-1)
    return bars,points,signals


def test_next_open_ambiguity_stop_first_cost_reconciliation():
    bars,points,signals = simulation_input()
    b = bars[1]
    bars[1] = Bar(b.venue,b.market,b.instrument_id,b.timeframe,b.open_time,b.close_time,b.open,Decimal(150),Decimal(50),b.close,b.volume,True,1,b.available_at)
    r = simulate_directional(bars,points,signals,Rule("wave3","LONG","ANY","MEASURED"),0,len(bars),[.01]*len(bars),detailed=True)
    t = r["trades"][0]
    assert t["entry_time"] == bars[1].open_time.isoformat()
    assert t["exit_reason"] == "STOP" and t["funding"] > 0
    assert r["metrics"]["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in r["trades"]))
    assert t["fees"] > 0 and not r["hedged"] and not r["deployable"]


def test_cost_stress_and_daily_only_optimization_are_consistent():
    bars = make_bars(500)
    points,book = signal_book(bars)
    rule = Rule("macd_cross","BOTH","ANY","QUICK")
    a = simulate_directional(bars,points,book[rule.family],rule,201,len(bars),[0.]*len(bars))
    b = simulate_directional(bars,points,book[rule.family],rule,201,len(bars),[0.]*len(bars),detailed=True)
    assert a["metrics"] == b["metrics"]
    assert a["trades"] == [{k:v for k,v in t.items() if k not in ("signal","indicators")} for t in b["trades"]]
    assert not a["screening"]["deployable"]


def test_settlement_credit_not_claimed_on_ambiguous_entry_or_exit_bar():
    bars,points,signals = simulation_input()
    rule = Rule("wave3","LONG","ANY","MEASURED")
    fees = [0.]*len(bars);fees[1] = -100.;fees[-1] = -100.
    a = simulate_directional(bars,points,signals,rule,0,len(bars),fees)
    b = simulate_directional(bars,points,signals,rule,0,len(bars),[0.]*len(bars))
    assert a["metrics"] == b["metrics"]
