import pytest
from gatescope.quant.mining import Candidate,FAMILIES,candidates,mining_features,candidate_signals,evaluate
from tests.unit.test_research_backtest import make_bars


@pytest.mark.parametrize("family",FAMILIES)
def test_mined_signals_are_prefix_causal_and_never_override_costs(family):
    bars=make_bars(450)
    spec=Candidate(family,0,"ANY","BOTH",2.,.7,6)
    full=candidate_signals(bars,mining_features(bars),spec)
    partial=candidate_signals(bars[:300],mining_features(bars[:300]),spec)
    assert full[:300]==partial
    a=evaluate(bars,full,spec,201,len(bars))
    b=evaluate(bars,full,spec,201,len(bars),2)
    assert not a["screening"]["deployable"]
    assert spec.execution_spec(2).fee_bps==10
    assert b["metrics"]["trade_count"]==a["metrics"]["trade_count"]


def test_development_grid_is_bounded_and_deterministic():
    assert len(candidates())==576
    assert candidates()==candidates()
