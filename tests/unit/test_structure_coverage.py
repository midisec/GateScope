import copy
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

from pattern_engine import scan_patterns
from gatescope.quant.structure_detection import _canonical_boundary_windows, proposals, refine
from gatescope.adapters.postgres_pattern_studies import PatternStudyRepository
from tests.unit.test_pattern_study import candles


def channel_bars():
    rows = candles(180)
    for i,b in enumerate(rows):
        price = 100 + i*.1 + 5*math.sin(i*math.pi/7)
        b.update(open=price,close=price,high=price+.2,low=price-.2)
    return rows


def test_nested_boundaries_are_not_deleted_for_sharing_an_end_time():
    rows = channel_bars()
    opts = dict(selected_patterns=["uptrend_channel"], min_confidence=0,
        latest_only=True, precision_mode=True, pivot_order=4)
    old = scan_patterns(rows, **opts)["events"]
    full = scan_patterns(rows, **opts, preserve_candidates=True)["events"]
    assert len(old) == 1 and len(full) >= 3
    assert len({e["end_time"] for e in full}) == 1
    result = [e for e in proposals(rows) if e["pattern"]=="uptrend_channel"]
    assert len(result) >= 2
    assert len({e["id"] for e in result}) == len(result)


def test_low_fit_valid_geometry_is_kept_and_crossed_boundaries_are_rejected():
    rows=channel_bars()
    raw=scan_patterns(rows,selected_patterns=["uptrend_channel"],min_confidence=0,
        latest_only=True,precision_mode=True,pivot_order=4,preserve_candidates=True)["events"][0]
    assert refine(raw,rows) is not None
    invalid=copy.deepcopy(raw)
    invalid["lines"][0]["to"]["price"]=invalid["lines"][1]["to"]["price"]-1
    d={}
    assert refine(invalid,rows,diagnostics=d) is None
    assert d["rejected"]=={"BOUNDARIES_CROSSED":1}
    p=[{"time":rows[i]["ts"],"price":rows[i][basis],"label":label}
        for i,basis,label in ((140,"high","1"),(150,"low","N"),(170,"high","2"))]
    # Admission has already passed detector geometry; score must not re-filter.
    low={**raw,"pattern":"double_top","direction":"bearish","signal_direction":"bearish",
        "region":None,"confirmed":False,"confidence":.1,"points":p,
        "lines":[{"from":a,"to":b,"style":"solid"} for a,b in zip(p,p[1:])]}
    accepted=refine(low,rows)
    assert accepted is not None and accepted["quality"]=="LOW_FIT"


def test_counts_balance_and_price_scale_preserves_coverage():
    rows=channel_bars(); counts={}
    result=proposals(rows,diagnostics=counts)
    assert counts["raw_count"]==sum(counts.get("rejected",{}).values())+counts["geometry_valid"]
    assert counts["geometry_valid"]==counts["identity_duplicates"]+len(result)
    scaled=copy.deepcopy(rows)
    for b in scaled:
        for k in ("open","high","low","close"): b[k]*=1e-8
    other=proposals(scaled)
    assert sorted((e["pattern"],e["phase"],e["start_time"]) for e in result)==sorted((e["pattern"],e["phase"],e["start_time"]) for e in other)
    for e in result:
        assert all(p["time"]<=rows[-1]["ts"] for p in e["points"])


@pytest.mark.parametrize("scale", [1e-9, 1e-8, .007, .3, 3., 100., 1e8])
@pytest.mark.parametrize("descending", [False, True])
def test_boundary_window_identity_and_geometry_are_price_unit_invariant(scale, descending):
    rows = channel_bars()
    if descending:
        for row in rows:
            row.update(open=200-row["open"], close=200-row["close"],
                       high=200-row["low"], low=200-row["high"])
    scaled = copy.deepcopy(rows)
    for row in scaled:
        for key in ("open", "high", "low", "close"):
            row[key] *= scale
    def sort_key(event):
        return event["pattern"], event["phase"], event["start_time"]
    baseline = sorted(proposals(rows), key=sort_key)
    diagnostics = {}
    actual = sorted(proposals(scaled, diagnostics=diagnostics), key=sort_key)
    assert [sort_key(event) for event in actual] == [sort_key(event) for event in baseline]
    pattern = "downtrend_channel" if descending else "uptrend_channel"
    # Four detector windows yield three distinct anchor sets; none is suppressed
    # for overlapping in time, and the identical 100/140-bar pair uses 140 bars.
    assert sorted(event["start_time"] for event in actual if event["pattern"] == pattern) == [
        rows[index]["ts"] for index in (40, 110, 135)
    ]
    assert diagnostics["geometry_valid"] == diagnostics["identity_duplicates"] + len(actual)
    assert diagnostics["raw_count"] == diagnostics["geometry_valid"] + sum(diagnostics["rejected"].values())
    for reference, event in zip(baseline, actual, strict=True):
        assert [(point["time"], point["basis"], point["label"]) for point in event["points"]] == [
            (point["time"], point["basis"], point["label"]) for point in reference["points"]
        ]
        assert [point["price"] / scale for point in event["points"]] == pytest.approx(
            [point["price"] for point in reference["points"]], rel=1e-12, abs=0)
        assert event["confidence"] == pytest.approx(reference["confidence"], rel=1e-12, abs=0)
        for source, guide in zip(reference["lines"], event["lines"], strict=True):
            for endpoint in ("from", "to"):
                assert guide[endpoint]["time"] == source[endpoint]["time"]
                assert guide[endpoint]["price"] / scale == pytest.approx(source[endpoint]["price"], rel=1e-12, abs=0)


def test_identical_boundary_anchors_do_not_compete_on_interpolation_noise():
    rows = channel_bars()
    raw = scan_patterns(rows, selected_patterns=["uptrend_channel"], min_confidence=0,
                        latest_only=True, precision_mode=True, pivot_order=4,
                        preserve_candidates=True)["events"]
    events = [refine(event, rows) for event in raw]
    assert all(event is not None for event in events)
    by_start = {event["start_time"]: event for event in events}
    narrow, wide = (by_start[rows[index]["ts"]] for index in (80, 40))
    assert narrow["points"] == wide["points"] and narrow["id"] == wide["id"]
    narrow["confidence"], wide["confidence"] = 1., math.nextafter(1., 0.)
    for candidates in (events, list(reversed(events))):
        selected = _canonical_boundary_windows(candidates)
        assert len(selected) == 3
        chosen = next(event for event in selected if event["id"] == wide["id"])
        assert chosen is wide
        assert chosen["confidence"] == math.nextafter(1., 0.)  # Not rounded up.
    # Even an identity-sharing event with one different later anchor is not the
    # same fitted geometry: leave it for normal phase/anchor/score selection.
    changed = copy.deepcopy(narrow)
    changed["points"][-1]["price"] = math.nextafter(changed["points"][-1]["price"], math.inf)
    assert len(_canonical_boundary_windows([wide, changed])) == 2


def test_wave_same_candle_high_low_has_no_invented_intrabar_order():
    rows=channel_bars()
    point={"time":rows[-5]["ts"],"price":rows[-5]["high"],"label":"0"}
    raw={"pattern":"elliott_impulse_bull","phase":"FORMING","points":[point,{**point,"label":"1"}],"lines":[]}
    diagnostics={}
    assert refine(raw,rows,diagnostics=diagnostics) is None
    assert diagnostics["rejected"]=={"AMBIGUOUS_PIVOT_ORDER":1}


def test_current_pagination_counts_all_records_not_first_96(monkeypatch):
    from gatescope.adapters import annual_structure_study as annual
    monkeypatch.setattr(annual,"record_reference",lambda *args,**kwargs:{"sample_count":0,"rate":None})
    records={str(i):{"id":str(i),"phase":"FORMING" if i%2 else "INVALIDATED","observed_time":i,
        "first_observed_time":i,"latest":{"name":"波浪" if i%2 else "双顶","group":"结构","experimental":bool(i%2)}} for i in range(125)}
    class Result:
        def __init__(self,row):self.row=row
        def fetchone(self):return self.row
    class Pool:
        @contextmanager
        def public_transaction(self):yield self
        def execute(self,query,args):
            if "SELECT requested_at" in query:return Result((datetime.now(timezone.utc),"READY",8760,8760,1000))
            if "checkpoint_json" in query:return Result(({"last_ts":1000,"records":records},None))
            return Result(None)
    repo=PatternStudyRepository(Pool()); instrument=UUID(int=1)
    a=repo.current(instrument,"1h"); b=repo.current(instrument,"1h",offset=96)
    assert a["total"]==125 and a["tracked_total"]==125 and len(a["items"])==96
    assert len(b["items"])==29 and a["has_more"] and not b["has_more"]
    assert sum(a["phase_counts"].values())==a["tracked_total"]
    wave=repo.current(instrument,"1h",family="wave",query="波浪",limit=12)
    assert wave["total"]==62 and len(wave["items"])==12
    assert repo.current(instrument,"1h",phase="INVALIDATED",family="wave")["total"]==0
