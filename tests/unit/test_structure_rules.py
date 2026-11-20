import copy
import math

import pytest

from gatescope.quant.pattern_study import catalog, strict_impulse
from gatescope.quant.structure_detection import proposals, refine
from gatescope.quant.structure_rules import audit, retest_proposals
from gatescope.quant.structure_lifecycle import completion_summary, observe, probability
from tests.unit.test_pattern_study import candles
from tests.unit.test_structure_lifecycle import candidate, replay


def pivot_case(key):
    if key.startswith("double"):
        prices, labels = [110,100,110.2], ["1","N","2"]
    elif key.startswith("triple"):
        prices, labels = [110,100,110.1,100.2,110.2], ["1","N1","2","N2","3"]
    elif "shoulders" in key:
        prices, labels = [110,100,118,101,110], ["LS","N1","H","N2","RS"]
    elif key.startswith("elliott_impulse"):
        prices, labels = [100,110,104,124,114,130], list("012345")
    elif key.startswith("elliott_cycle"):
        prices, labels = [100,110,104,124,114,130,120,125,114], list("012345ABC")
    else:
        prices, labels = [130,115,123,110], list("0ABC")
    mirrored = key.endswith(("bottom","bear")) or key=="inverse_head_shoulders"
    if mirrored:prices=[240-p for p in prices]
    indices = [20+12*i for i in range(len(prices))]
    rows = candles(indices[-1]+6)
    vertices=[(0,prices[0]+(5 if prices[1]>prices[0] else -5)),*zip(indices,prices),
              (len(rows)-1,prices[-1]+(2 if prices[-1]<prices[-2] else -2))]
    for (a,pa),(b,pb) in zip(vertices,vertices[1:]):
        for i in range(a,b+1):
            p=pa+(pb-pa)*(i-a)/(b-a)
            rows[i].update(open=p,close=p,high=p+.02,low=p-.02)
    points=[]
    for n,(i,p,label) in enumerate(zip(indices,prices,labels)):
        high = prices[1]<p if n==0 else prices[n-1]<p
        points.append({"time":rows[i]["ts"],"price":rows[i]["high" if high else "low"],"label":label})
    meta=next(m for m in catalog() if m["id"]==key)
    raw={**meta,"pattern":key,"confidence":.7,"confirmed":False,"phase":"FORMED", "signal_direction":meta["direction"],
        "start_time":points[0]["time"],"end_time":points[-1]["time"],"points":points,
        "lines":[{"from":a,"to":b,"style":"solid"} for a,b in zip(points,points[1:])]}
    return rows,raw


PIVOT_KEYS=[m["id"] for m in catalog() if m["id"].startswith(("double","triple","elliott")) or "shoulders" in m["id"]]


@pytest.mark.parametrize("key",PIVOT_KEYS)
def test_pivot_families_have_valid_positive_geometry_and_real_anchors(key):
    rows,raw=pivot_case(key)
    diagnostics={};result=refine(raw,rows,diagnostics=diagnostics)
    assert result is not None,diagnostics
    assert result["recognition"]["checks"] and result["phase"]=="FORMED"
    assert key in {e["pattern"] for e in proposals(rows)}
    bad=copy.deepcopy(raw);bad["points"][-1]["time"]=bad["points"][-2]["time"]
    assert refine(bad,rows) is None


def cup_case(inverse=False, rounded=True):
    rows=candles(90)
    for i,b in enumerate(rows):
        x=i/69
        p=100+10*((2*x-1)**2 if rounded else abs(2*x-1)) if i<70 else 110-3*math.sin(math.pi*(i-69)/20)
        if inverse:p=220-p
        b.update(open=p,close=p,high=p+.15,low=p-.15)
    key="inverse_cup_handle" if inverse else "cup_handle"
    pts=[{"time":rows[i]["ts"],"price":rows[i]["close"],"label":label,"basis":"CLOSE"} for i,label in ((0,"L"),(34,"B"),(69,"R"),(79,"H"),(89,"E"))]
    meta=next(m for m in catalog() if m["id"]==key)
    return rows,{**meta,"pattern":key,"points":pts,"lines":[],"start_time":pts[0]["time"],"phase":"FORMED","confidence":.7,"confirmed":False}


@pytest.mark.parametrize("inverse",[False,True])
def test_cups_distinguish_rounded_body_from_v_and_deep_handle(inverse):
    rows,event=cup_case(inverse)
    refined=refine(event,rows)
    assert refined is not None
    assert len([line for line in refined["lines"] if line.get("curve")])==2
    from gatescope.application.telegram_chart import chart_spec,render_chart
    assert render_chart(chart_spec('TEST','1h',rows,refined,'圆弧原理线')).startswith(b'\x89PNG')
    assert event["pattern"] in {e["pattern"] for e in proposals(rows)}
    bad_rows,bad=cup_case(inverse,False)
    assert audit(bad,bad_rows,1)[0]=="CUP_NOT_ROUNDED"
    bad=copy.deepcopy(event)
    bad["points"][3]["price"]=105 if not inverse else 115
    assert audit(bad,rows,1)[0]=="CUP_HANDLE_DEPTH"


BOUNDARIES={"ascending_triangle":(0,5),"descending_triangle":(-5,0),"symmetrical_triangle":(-3,3),
    "rectangle":(0,0),"rising_wedge":(2,5),"falling_wedge":(-5,-2),
    "uptrend_channel":(3,3),"downtrend_channel":(-3,-3)}


@pytest.mark.parametrize("key",BOUNDARIES)
def test_all_boundary_classes_validate_named_slopes_not_just_fit(key):
    u,l=BOUNDARIES[key];rows=candles(100)
    def point(i,p,label):return {"time":rows[i]["ts"],"price":p,"label":label}
    lines=[{"from":point(10,110,"U"),"to":point(99,110+u,"U")},
           {"from":point(10,100,"L"),"to":point(99,100+l,"L")}]
    event={"pattern":key,"points":[point(10,110,"H"),point(30,100,"L")],"lines":lines,"region":True}
    assert audit(event,rows,1)[0] is None
    event["pattern"]="rectangle" if key!="rectangle" else "symmetrical_triangle"
    assert audit(event,rows,1)[0]=="BOUNDARY_CLASSIFICATION"
    actual=candles(180)
    for i,b in enumerate(actual):
        top=110+u*i/179;bottom=100+l*i/179
        p=(top+bottom)/2+(top-bottom)/2*math.sin(i*math.pi/7)
        b.update(open=p,close=p,high=p+.05,low=p-.05)
    assert key in {e["pattern"] for e in proposals(actual)}


@pytest.mark.parametrize("key",["bull_flag","bear_flag","bull_pennant","bear_pennant"])
def test_flags_and_pennants_need_both_named_boundaries(key):
    rows=candles(50)
    event={"pattern":key,"points":[],"lines":[]}
    assert audit(event,rows,1)[0]=="BOUNDARIES_MISSING"
    event["lines"]=[{"from":{"label":k,"price":v},"to":{"price":v+d}} for k,v,d in (("U",110,-2),("L",100,2 if "pennant" in key else -2))]
    assert audit(event,rows,1)[0] is None
    actual=candles(95)
    for i,b in enumerate(actual):
        t=i-69
        p=100 if i<45 else 100+.9*(i-45) if i<69 else 120+((3-.09*t)*math.sin(math.pi*t/5) if "pennant" in key else -.08*t+2*math.sin(math.pi*t/5))
        if key.startswith("bear"):p=240-p
        b.update(open=p,close=p,high=p+.05,low=p-.05)
    assert key in {e["pattern"] for e in proposals(actual)}


def test_fibonacci_guidelines_do_not_exclude_valid_standard_impulse():
    # Wave 2 retraces 95%, but never crosses origin. Wave 3 is not shortest.
    values=[100,110,100.5,125,112,130]
    assert not strict_impulse(values,True)
    assert strict_impulse(values,True,guidelines=False)
    for invalid in ([100,110,99,125,112,130],[100,110,104,125,109,130],[100,115,110,120,116,135]):
        assert not strict_impulse(invalid,True,guidelines=False)


@pytest.mark.parametrize("sign",[1,-1])
def test_new_retest_is_causal_once_only_and_keeps_actual_ohlc(sign):
    rows=candles(39)
    for i,b in enumerate(rows):b.update(open=100,close=100,high=102 if i%8==0 else 100.5,low=99.5)
    for i,p in ((32,104),(33,105),(34,104),(35,102.2),(36,103),(37,103),(38,103)):
        rows[i].update(open=p,close=p,high=p+.2,low=p-.2)
    if sign<0:
        for b in rows:
            o,h,l,c=[b[k] for k in ('open','high','low','close')]
            b.update(open=220-o,high=220-l,low=220-h,close=220-c)
    # Prefix padded at the start; fixed public-data setup has no future dependency.
    step=3600
    padding=[{**rows[0],"ts":rows[0]["ts"]-(8-i)*step} for i in range(8)]
    rows=padding+rows
    events=retest_proposals(rows[:44])
    key="bull_breakout_retest" if sign>0 else "bear_breakout_retest"
    assert len(events)==1 and events[0]["pattern"]==key
    assert refine(events[0],rows[:44]) is not None
    assert not retest_proposals(rows[:45])
    bad=copy.deepcopy(rows[:44]);bad[-2]["close"]=100 if sign>0 else 120
    assert not retest_proposals(bad)


def test_formation_success_is_not_breakout_success_and_predictions_are_causal():
    bars=candles(32)
    state,_=replay(bars,lambda p:[candidate(p,"FORMED" if len(p)>=22 else "FORMING")])
    rows,_=completion_summary(state)
    row=next(r for r in rows if r["pattern"]=="double_top" and r["phase"]=="FORMING" and r["score_bucket"]=="ALL")
    assert row["successes"]==0 and row["formation"]["successes"]==1
    assert row["sample_count"]==row["formation"]["sample_count"]==1
    original=state["records"]["a"]["stages"]["FORMING"]["prediction"]
    assert original["formation"]["sample_count"]==0
    assert probability(19,19)["probability"] is None
    assert probability(20,20)["interval"]["lower"]<1
    assert probability(50,100)["interval"]["upper"]-probability(50,100)["interval"]["lower"] > probability(500,1000)["interval"]["upper"]-probability(500,1000)["interval"]["lower"]
