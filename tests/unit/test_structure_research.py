from dataclasses import replace
from decimal import Decimal

import pytest

from apps.api.research import BacktestRequest
from gatescope.quant.backtest import StrategySpec
from gatescope.quant.research_rules import PatternReplay
from gatescope.quant.structure_research import case_context, dependence_horizon, policy_from_spec, selection, walk_forward_windows
from tests.unit.test_research_backtest import make_bars
from tests.unit.test_structure_rules import pivot_case


def make_structure_bars(count=360):
    """Known double-bottom pivots, then two real closes beyond its neckline."""
    rows, _ = pivot_case("double_bottom")
    result = []
    for index, bar in enumerate(make_bars(count)):
        price = rows[index]["close"] if index < len(rows) else min(150.,rows[-1]["close"]+index-len(rows)+1)
        result.append(replace(bar,open=Decimal(str(price)),close=Decimal(str(price)),
            high=Decimal(str(price+.02)),low=Decimal(str(price-.02)),payload_hash=""))
    return result


def test_long_history_bounded_structure_and_indicator_modes_with_wave_catalog():
    instrument = "00000000-0000-0000-0000-000000000001"
    request = BacktestRequest(instrument_id=instrument,bar_limit=525600,
        spec=StrategySpec(engine_mode="STRUCTURE",structure_patterns=["elliott_impulse_bull"]))
    assert request.bar_limit == 525600
    assert BacktestRequest(instrument_id=instrument,bar_limit=525600).bar_limit==525600
    with pytest.raises(ValueError,match="年度形态"):
        BacktestRequest(instrument_id=instrument,bar_limit=525600,
            spec=StrategySpec(strategy="composite",rule_ids=["pattern:double_bottom"]))
    with pytest.raises(ValueError):
        BacktestRequest(instrument_id=instrument,bar_limit=550001)
    for patterns in (["invalid"],["double_top","double_top"]):
        with pytest.raises(ValueError):
            StrategySpec(engine_mode="STRUCTURE",structure_patterns=patterns)


def test_selection_preserves_baseline_and_never_reads_trade_outcome():
    event = {"pattern":"double_bottom","signal_direction":"bullish","confidence":.6}
    spec = StrategySpec(engine_mode="STRUCTURE",direction="BOTH",regimes=["UP"])
    selected = selection(event,{"trend":"DOWN"},spec)
    assert selected == {"selected":False,"baseline":True,
        "selection_reasons":["BELOW_GEOMETRY_FILTER","REGIME_NOT_SELECTED"]}
    assert selection({**event,"future_profit":9000},{"trend":"DOWN"},spec) == selected
    outside = selection(event,{"trend":"UP"},spec.model_copy(update={"structure_patterns":["double_top"]}))
    assert not outside["baseline"] and "PATTERN_NOT_SELECTED" in outside["selection_reasons"]


def test_walk_forward_prefix_purge_and_nonoverlapping_test_windows():
    folds = walk_forward_windows(1000,1000+10000*3600,3600,4,50)
    assert len(folds)==4
    for fold in folds:
        assert fold["train_from"] == 1000
        assert fold["train_to"] + 50*3600 == fold["test_from"]
        assert fold["train_to"] < fold["test_from"] < fold["test_to"]
    assert all(a["test_to"]==b["test_from"] for a,b in zip(folds,folds[1:],strict=False))
    assert not walk_forward_windows(0,100*3600,3600,4,200)


def test_dependence_blocks_include_formed_waiting_and_never_shrink_actual_span():
    formed=StrategySpec(engine_mode="STRUCTURE",structure_entry_phase="FORMED",max_holding_bars=48,entry_delay_bars=2)
    horizon=dependence_horizon(formed,3600)
    assert horizon["embargo_bars"]==530 and horizon["block_days"]==23
    larger=dependence_horizon(formed,3600,40*86400)
    assert larger["embargo_bars"]==960 and larger["block_days"]==40
    assert dependence_horizon(formed,3600,3*3600)=={**horizon,"observed_span_seconds":3*3600}
    assert dependence_horizon(formed.model_copy(update={"structure_entry_phase":"CONFIRMED"}),3600)["block_days"]==7
    extreme=dependence_horizon(formed,3600,1001*86400)
    assert extreme["block_days"]==1000 and extreme["required_block_days"]==1001


def test_context_alignment_and_spot_costs_are_causal_model_inputs():
    context = case_context({"confidence":.9,"signal_direction":"bearish"},
        {"trend":"DOWN","higher_timeframe":{"trend":"UP"}})
    assert context["higher_alignment"] == "OPPOSED" and context["score_bucket"]=="HIGH"
    policy = policy_from_spec(StrategySpec(entry_delay_bars=2),300,spot=True)
    assert policy["entry_delay_seconds"]==600 and policy["funding_bps_per_8h"]==0


def test_default_research_pattern_replay_confirms_real_same_engine_structure():
    from gatescope.quant.structure_detection import VERSION
    replay = PatternReplay(["double_bottom"],.65)
    events = []
    for index,bar in enumerate(make_structure_bars(90)):
        events.extend(replay.append(bar,index,bar.close_time.isoformat()))
    assert len(events)==1
    event = events[0]
    assert event["phase"]=="CONFIRMED" and event["detector_version"]==VERSION
    assert event["lifecycle_record"]["stages"]["FORMED"]["time"] < event["observed_time"]
    assert max(point["time"] for point in event["points"]) <= event["observed_time"]


def test_same_engine_warmup_and_rolling_window_keep_absolute_bar_index(monkeypatch):
    import gatescope.quant.structure_lifecycle as lifecycle
    seen=[]
    def observer(candles,index,state,**kwargs):
        absolute=index+kwargs["index_offset"]
        seen.append((len(candles),index,absolute))
        state.update(records={},sampling={},last_index=absolute,last_ts=candles[index]["ts"])
        return []
    monkeypatch.setattr(lifecycle,"observe",observer)
    replay=PatternReplay(["double_bottom"],.5)
    for index,bar in enumerate(make_structure_bars(490)):
        replay.append(bar,index,bar.close_time.isoformat())
    assert seen[0]==(20,19,19)
    assert [item[2] for item in seen]==list(range(19,490))
    assert seen[-1]==(480,479,489)


def test_persisted_terminal_guard_survives_hot_window_pruning_and_restart():
    import json
    from uuid import UUID
    from gatescope.adapters.annual_structure_study import compact
    from gatescope.adapters.postgres_structure_research import StructureResearchRepository
    from gatescope.quant.pattern_study import WINDOW
    from gatescope.quant.structure_lifecycle import TERMINAL,observe
    from tests.unit.test_pattern_study import candles
    from tests.unit.test_structure_lifecycle import candidate
    run=UUID("00000000-0000-0000-0000-000000000009")
    recorded=set()
    class Journal:
        def execute(self,query,params):
            assert "run_id=%s" in query and params[0]==run
            assert all(phase in query for phase in TERMINAL)
            self.matches=[(key,) for key in params[1] if key in recorded]
            return self
        def fetchall(self):
            return self.matches
    conn=Journal()
    bars=candles(WINDOW+35)
    fixed=candidate(bars[:20],"FORMED",identifier="same-failed-candidate",expiry=2)
    state={"step_seconds":3600}
    admitted=[]
    for index in range(19,len(bars)):
        window=bars[max(0,index-WINDOW+1):index+1]
        if index==WINDOW+25:
            assert fixed["id"] not in state["records"]
        def detector(prefix,current_index=index):
            return [fixed] if current_index in (19,WINDOW+25) else []
        journal=observe(window,len(window)-1,state,index_offset=index-len(window)+1,detector=detector,
            terminal_guard=lambda ids:StructureResearchRepository.terminal_candidates(conn,run,ids))
        for transition in journal:
            if transition["phase"] in TERMINAL:
                recorded.add(transition["candidate_id"])
            if transition["record"]["reason"]=="ADMITTED":
                admitted.append(transition["candidate_id"])
        state["facts"]=[]
        compact(state)
        if index%73==0:
            state=json.loads(json.dumps(state))
    assert admitted==[fixed["id"]] and fixed["id"] in recorded
