from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image
import pytest

from gatescope.application.paper_decision_chart import paper_decision_chart_spec, render_paper_decision_chart
from gatescope.application.paper_execution import PaperExecutionError


def frozen_order():
    start = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)
    candles = [{"open_time": (start+timedelta(minutes=5*i)).isoformat(),
        "close_time": (start+timedelta(minutes=5*(i+1))).isoformat(), "open": "100", "high": "102", "low": "98",
        "close": "101", "volume": "10", "is_final": True} for i in range(30)]
    event = {"name": "原始双底", "points": [{"time": int(start.timestamp()), "price": 98, "label": "A"},
        {"time": int(start.timestamp())+600, "price": 98, "label": "B"}], "lines": []}
    return {"id": "order", "decision_snapshot": {"id": "snapshot", "evidence_digest": "a"*64,
        "evidence_json": {"symbol": "SOL_USDT", "timeframe": "5m", "observed_at": (start+timedelta(hours=3)).isoformat(),
            "server_evidence": {"candles": candles, "pattern_events": [{"record": {"latest": event}}], "gaps": []}}}}


def test_png_uses_frozen_times_geometry_and_is_repeatable():
    order = frozen_order()
    chart = paper_decision_chart_spec(order)
    assert len(chart["bars"]) == 30 and chart["layout"] == "PATTERN"
    assert chart["event"] == order["decision_snapshot"]["evidence_json"]["server_evidence"]["pattern_events"][0]["record"]["latest"]
    first, metadata = render_paper_decision_chart(order)
    assert first == render_paper_decision_chart(order)[0]
    assert Image.open(BytesIO(first)).size == (1400, 960)
    assert metadata["decision_snapshot_id"] == "snapshot" and metadata["evidence_digest"] == "a"*64


def test_missing_evidence_is_not_replaced_with_current_candles_or_shapes():
    with pytest.raises(PaperExecutionError, match="不使用当前行情"):
        paper_decision_chart_spec({"id": "manual"})
    order = frozen_order()
    source = order["decision_snapshot"]["evidence_json"]["server_evidence"]
    source["pattern_events"] = []
    chart = paper_decision_chart_spec(order)
    assert chart["event"] is None and "不事后识别或补画" in chart["summary"]
    source["candles"] = []
    with pytest.raises(PaperExecutionError, match="不补造历史图"):
        paper_decision_chart_spec(order)


def test_truncated_anatomy_and_gaps_are_disclosed_without_moving_anchors():
    order = frozen_order()
    source = order["decision_snapshot"]["evidence_json"]["server_evidence"]
    anchor = source["pattern_events"][0]["record"]["latest"]["points"][0]
    anchor["time"] -= 5000
    source["gaps"] = [{"after": "earlier", "before": "later"}]
    chart = paper_decision_chart_spec(order)
    assert "锚点早于冻结窗口" in chart["summary"] and "存在缺口" in chart["summary"]
    assert chart["event"]["points"][0]["time"] == anchor["time"]


def test_out_of_range_pattern_or_unclosed_candle_is_rejected():
    order = frozen_order()
    with pytest.raises(PaperExecutionError):
        paper_decision_chart_spec(order, pattern_index=3)
    order["decision_snapshot"]["evidence_json"]["server_evidence"]["candles"][-1]["is_final"] = False
    with pytest.raises(ValueError, match="availability"):
        paper_decision_chart_spec(order)
